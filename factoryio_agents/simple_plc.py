"""Straight-line PLC continuation over the existing binding/compiler/lifecycle tools."""
from copy import deepcopy
from pathlib import Path

from .binding_tools import bool_bindings, compile_bound_program
from .contracts import ContractError, canonical_hash, read_json, require, write_json
from .coordination import attach_component_semantics
from .plc import check_assignment_targets, export_program
from .plc_scaffold import assemble_program, io_scaffold
from .runtime import run_hook
from .simple_prompts import PLC_PROMPT, RUNTIME_PROMPT, RUNTIME_SCHEMA
from .simple_runtime import TransferRun, transfer_signals


def start_service_with_uac(config,directory,trace):
    import os,subprocess,time
    script=Path(__file__).resolve().parents[1]/'tools/start-codesys-runtime.ps1'
    output=(directory/'runtime-start-elevated.json').resolve()
    quote=lambda value:"'"+str(value).replace("'","''")+"'"
    args=['-NoProfile','-NonInteractive','-File','"'+str(script)+'"','-OutputPath','"'+str(output)+'"']
    command="Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden -Wait -ArgumentList @("+','.join(quote(a) for a in args)+')'
    env=dict(os.environ)
    for name in config.get('online_credentials',{}).values():env.pop(name,None)
    trace('tool_requested',phase='deployment_preflight',tool='windows_uac',action='start_local_codesys_services')
    with (directory/'service-uac.stdout.log').open('w') as stdout,(directory/'service-uac.stderr.log').open('w') as stderr:
        process=subprocess.Popen(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],env=env,stdout=stdout,stderr=stderr)
        deadline=time.monotonic()+60
        while not output.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.2)
    require(output.is_file(),'Windows administrator confirmation or service startup did not complete; inspect service-uac.stderr.log')
    report=read_json(output);require(report.get('service_running') is True,'elevated Runtime startup was not confirmed')
    trace('tool_completed',phase='deployment_preflight',tool='windows_uac',action='start_local_codesys_services',receipt=str(output))


def assemble_logic(fragment, scaffold, contract):
    require(isinstance(fragment,dict) and set(fragment)=={'declaration','implementation','unresolved','notes'},
            'PLC output needs exactly declaration, implementation, unresolved, notes; tools own I/O')
    program=assemble_program({'schema_version':1,'handoff_id':None,'program_name':scaffold['program_name'],
        **fragment},scaffold,contract)
    check_assignment_targets(program)
    require(not program['unresolved'],'PLC has unresolved requirements: '+str(program['unresolved']))
    return program


def runtime_review(value,object_ids,evidence_ids):
    require(isinstance(value,dict) and value.get('verdict') in ('pass','repair','uncertain'),'invalid runtime verdict')
    require(isinstance(value.get('summary'),str) and isinstance(value.get('findings'),list),'runtime summary/findings missing')
    for finding in value['findings']:
        require(finding.get('target') in ('plc','scene','tools','unknown') and finding.get('status') in ('issue','uncertain'),'runtime finding target/status missing')
        require(isinstance(finding.get('object_ids'),list) and set(finding['object_ids'])<=object_ids,'runtime finding object unknown')
        refs=finding.get('evidence_ids');require(isinstance(refs,list) and refs and set(refs)<=evidence_ids,'runtime finding needs current evidence')
        require(all(isinstance(finding.get(k),str) and finding[k].strip() for k in ('observed','reason','suggestion')),'runtime finding needs explanation')
    require(value['verdict']!='pass' or not value['findings'],'runtime pass cannot contain findings')
    return value


class NativePLCTools:
    def __init__(self, workflow):self.w=workflow;self.active_run=None

    def prepare(self,design,rendered,directory):
        w=self.w;contract=rendered['io_contract'];contract['protocol']=w.config['protocol']
        require(w.plc_mode=='compile' or w.config['factoryio'].get('runtime_control') is True,'runtime_control must be enabled for PLC execution')
        w.state['phase']='device_binding';w.trace('tool_requested',tool='codesys_binding',action='derive_verified_bool_channels')
        effective=bool_bindings(w.config['codesys'],contract)
        contract.update(bindings=effective['bindings'],transport_confirmed=effective['transport_confirmed'],software_load_confirmed=True)
        receipt=read_json(directory/'load-receipt.json')
        attach_component_semantics(rendered['manifest'],contract,receipt['app_version'])
        from .geometry_tools import attach_button_semantics
        attach_button_semantics(contract,rendered['manifest'],receipt)
        scaffold=io_scaffold(effective,contract);signals=transfer_signals(design,scaffold)
        write_json(directory/'io-contract.json',contract);write_json(directory/'manifest.json',rendered['manifest'])
        write_json(directory/'io-scaffold.json',scaffold);write_json(directory/'runtime-signals.json',signals)
        w.trace('tool_completed',tool='codesys_binding',action='derive_verified_bool_channels',binding_count=len(effective['bindings']))
        if effective.get('ensure_local_services') and w.plc_mode=='run':
            from .codesys_services import service_control
            w.state['phase']='deployment_preflight'
            w.trace('tool_requested',phase='deployment_preflight',tool='codesys_services',action='preflight')
            services=[service_control(kind,start=True) for kind in ('gateway','runtime')]
            write_json(directory/'service-preflight.json',{'services':services})
            if w.start_local_services and any(s.get('windows_error')==5 for s in services):
                start_service_with_uac(effective,directory,w.trace)
                services=[service_control(kind,start=False) for kind in ('gateway','runtime')]
                write_json(directory/'service-after-elevation.json',{'services':services})
            require(all(s.get('status')=='ok' and s.get('service_running') is True for s in services),'CODESYS local services unavailable; inspect service-preflight.json')
            w.trace('tool_completed',phase='deployment_preflight',tool='codesys_services',action='preflight',ready=True)
        return effective,scaffold,signals

    def compile(self,effective,program,contract,plc_dir):
        w=self.w;action='compile_only' if w.plc_mode=='compile' else 'compile_and_deploy'
        w.state['phase']='compile';w.trace('tool_requested',tool='codesys',action=action)
        if w.plc_mode=='compile':
            result=compile_bound_program(effective,plc_dir,program,contract,False)
            write_json(plc_dir/'codesys-result.json',result)
            w.trace('tool_completed',tool='codesys',action=action,compiled=result.get('compiled'),started=False,result=str(plc_dir/'codesys-result.json'))
            return result
        receipt_path=plc_dir/'driver-disconnect-receipt.json'
        run_hook(w.config['factoryio'].get('prepare_deployment_command'),{'output':str(receipt_path.resolve())},20)
        receipt=read_json(receipt_path)
        require(receipt.get('ok') is True and receipt.get('data',{}).get('modbus',{}).get('socket_connected') is False,
                'Factory I/O disconnect not confirmed before deployment')
        require(effective.get('deploy') is True,'enable CODESYS deployment for --with-plc')
        result=compile_bound_program(effective,plc_dir,program,contract,True)
        write_json(plc_dir/'codesys-result.json',result)
        w.trace('tool_completed',tool='codesys',action='compile_and_deploy',compiled=result.get('compiled'),started=result.get('started'),result=str(plc_dir/'codesys-result.json'))
        return result

    def measure(self,effective,plc_dir,signals,rendered,scaffold):
        self.active_run=TransferRun(self.w.config['factoryio'],effective,plc_dir,signals,rendered['manifest'],rendered['io_contract'],self.w.trace)
        result=self.active_run.run(scaffold,seconds=self.w.runtime_seconds,keep_running=self.w.keep_running,reset_mode=self.w.reset_mode)
        if not result['left_running'] and not result.get('cleanup_error'):self.active_run=None
        return result,read_json(plc_dir/'runtime/evidence.json')

    def stop(self):
        if self.active_run is not None:
            run=self.active_run;self.active_run=None
            return run.stop()


class SimplePLCStage:
    def __init__(self,workflow,tools=None):self.w=workflow;self.tools=tools or NativePLCTools(workflow)

    def run(self,requirement,design,rendered,directory):
        try:return self._run(requirement,design,rendered,directory)
        finally:
            if self.w.state.get('status')!='passed':self.tools.stop()

    def _run(self,requirement,design,rendered,directory):
        w=self.w;effective,scaffold,signals=self.tools.prepare(design,rendered,directory)
        from .memory import plc_io_design_hash
        feedback=None;previous=None;contract=rendered['io_contract'];ids={o['id'] for o in design['objects']}
        reused=None
        if getattr(w,'resume_source',None):
            old=read_json(w.resume_source/'run.json')
            if old.get('plc_program_sha256') and old.get('plc_dir'):
                old_dir=Path(old['plc_dir']).resolve();require(old_dir.is_relative_to(w.resume_source),'saved PLC is outside source run')
                candidate=read_json(old_dir/'program.json')
                require(canonical_hash(candidate)==old['plc_program_sha256'],'saved PLC program changed')
                if plc_io_design_hash(read_json(old_dir.parent/'io-contract.json'))==plc_io_design_hash(contract):
                    reused=candidate;w.trace('artifact_reused',artifact='plc_logic',source_run=str(w.resume_source))
        base={'phase':'plc_generation','execution_scope':'Continue this confirmed layout through PLC control and live process validation.',
            'requirement':requirement,'scene_goal':design.get('scene_goal'),'control_intent':design.get('control_intent',[]),
            'objects':[{k:o[k] for k in ('id','prefab','function')} for o in design['objects']],
            'io_scaffold':scaffold,'signal_roles':{role:signal['st_reference'] for role,signal in signals.items()}}
        for attempt in range(w.max_plc_repairs+1):
            payload={**base,'feedback':feedback,'previous_program':previous}
            if attempt==0 and reused is not None:
                program=reused;fragment=None
            else:
                fragment=w.call('plc',PLC_PROMPT,payload)
                try:program=assemble_logic(fragment,scaffold,contract)
                except (ContractError,ValueError) as error:
                    w.trace('plc_fragment_rejected',active_role='plc',error=str(error))
                    fragment=w.call('plc',PLC_PROMPT,{**payload,'rejected_fragment':fragment,'format_error':str(error)})
                    program=assemble_logic(fragment,scaffold,contract)
            require(previous is None or canonical_hash(program)!=canonical_hash(previous),'PLC repair returned unchanged code')
            previous=program;plc_dir=directory/f'plc-{attempt:03d}';export_program(program,contract,plc_dir)
            if fragment is not None:
                write_json(w.training.directory/'derived'/('plc-assembly-'+canonical_hash(program)+'.json'),{
                    'producer_role':'plc','assembly':'model_logic_with_tool_io','candidate_id':w.training.last_candidate_id,
                    'output_sha256':canonical_hash(fragment),'artifact_sha256':canonical_hash(program),'scaffold_sha256':canonical_hash(scaffold)})
            versions={'scene':rendered['manifest']['scene_sha256'],'io':canonical_hash(contract),'plc':canonical_hash(program)}
            write_json(plc_dir/'versions.json',versions)
            w.state.update(plc_dir=str(plc_dir),plc_revision=attempt,plc_program_sha256=versions['plc']);write_json(w.output/'run.json',w.state)
            result=self.tools.compile(effective,program,contract,plc_dir)
            if result.get('compiled') is not True:
                bridge=result.get('bridge_result',{})
                if bridge.get('build_completed') and bridge.get('messages_verified') and (result.get('error_count') or 0)>0 and attempt<w.max_plc_repairs:
                    feedback={'phase':'compile','messages':bridge.get('messages'),'diagnostics':result.get('diagnostics')};continue
                return w.finish('needs_attention',target='tools',phase='compile',diagnostics=result)
            if w.plc_mode=='compile':
                require(all(result.get(k) is True for k in ('bindings_verified','task_program_verified','transport_verified')),
                        'offline compile did not confirm bindings, task program or transport')
                return w.finish('plc_compiled',phase='compile',compiled=True,plc_executed=False,live_pipeline_passed=False)
            require(all(result.get(k) is True for k in ('started','bindings_verified','task_program_verified','transport_verified')),
                    'PLC compile passed but deployed application, bindings or transport were not confirmed')
            w.state['plc_executed']=True
            acceptance,evidence=self.tools.measure(effective,plc_dir,signals,rendered,scaffold)
            if acceptance.get('cleanup_error'):
                return w.finish('needs_attention',phase='runtime_control',target='tools',acceptance=acceptance,error=acceptance['cleanup_error'])
            images=[Path(r['path']) for r in evidence['records'] if r['kind']=='image']
            require(images,'current runtime screenshots missing')
            samples=[r for r in evidence['records'] if r['kind']=='transfer_sample'];records=[]
            previous_values=None
            for row in samples:
                if row['values']!=previous_values or row.get('forced_signals') or row.get('item_count',0)>1:
                    records.append({k:row[k] for k in ('id','kind','timestamp','elapsed_seconds','scope','values','item_count','forced_signals','connection_ok')})
                previous_values=row['values']
            prompt_records=records[:60]+[{k:r[k] for k in ('id','kind','timestamp')} for r in evidence['records'] if r['kind'] in ('image','operator_action')]
            prompt_records.append({'id':'runtime-acceptance','kind':'acceptance_result','result':acceptance})
            prompt_records.append({'id':'current-plc-deployment','kind':'plc_diagnostics','result':{
                k:result.get(k) for k in ('compiled','started','bindings_verified','task_program_verified','transport_verified',
                    'error_count','warning_count','project_sha256')}})
            from .llm import ModelClient
            if isinstance(w.raw_model,ModelClient):w.raw_model.config['response_schemas']['vision']=RUNTIME_SCHEMA
            review=runtime_review(w.call('vision',RUNTIME_PROMPT,{'phase':'runtime','requirement':requirement,
                'execution_scope':base['execution_scope'],
                'control_intent':design.get('control_intent',[]),'objects':base['objects'],'acceptance':acceptance,
                'evidence':{'records':prompt_records}},images),ids,{r['id'] for r in prompt_records})
            write_json(plc_dir/'runtime-feedback.json',review)
            if acceptance['passed'] and review['verdict']=='pass':
                return w.finish('passed',phase='runtime',live_pipeline_passed=True,acceptance=acceptance,
                    runtime_visual_verdict='pass',left_running=acceptance['left_running'])
            self.tools.stop()
            targets={f['target'] for f in review['findings']}
            if review['verdict']=='uncertain' or targets-{'plc'} or attempt==w.max_plc_repairs:
                return w.finish('needs_attention',phase='runtime',target=next(iter(targets),'plc'),acceptance=acceptance,feedback=review)
            feedback={'phase':'runtime','acceptance':acceptance,'visual_feedback':review,'observed_transitions':records[:60]}
        return w.finish('needs_attention',phase='plc_generation',error='PLC repair limit reached')
