"""PLC generation and task-provided operation plans over existing software adapters."""
from copy import deepcopy
from pathlib import Path

from .binding_tools import verified_bindings, compile_bound_program
from .contracts import ContractError, canonical_hash, read_json, require, write_json
from .coordination import attach_component_semantics
from .plc import export_program
from .plc_scaffold import assemble_program, io_scaffold
from .runtime import run_hook
from .simple_prompts import PLC_PROMPT, RUNTIME_PROMPT, RUNTIME_SCHEMA
from .simple_runtime import RuntimeRun, runtime_signals, validate_runtime_plan, summarize_evidence


def start_service_with_uac(config,directory,trace):
    import base64,os,subprocess,time
    script=Path(__file__).resolve().parents[1]/'tools/start-codesys-runtime.ps1'
    output=(directory/'runtime-start-elevated.json').resolve()
    launch_receipt=(directory/'service-uac-launch.json').resolve()
    elevated_log=(directory/'service-uac.elevated.log').resolve()
    quote=lambda value:"'"+str(value).replace("'","''")+"'"
    helper=("$ErrorActionPreference='Stop'; try { & "+quote(script)+" -OutputPath "+quote(output)+
        " *> "+quote(elevated_log)+"; if (-not (Test-Path -LiteralPath "+quote(output)+")) { throw 'Elevated helper exited without a receipt' } } catch { "+
        "@{status='error';service_running=$false;error=$_.Exception.Message} | ConvertTo-Json | Set-Content -LiteralPath "+quote(output)+" -Encoding UTF8; exit 2 }")
    encoded=base64.b64encode(helper.encode('utf-16-le')).decode('ascii')
    # Process-only policy for this local helper; never Set-ExecutionPolicy or registry changes.
    args=['-NoProfile','-NonInteractive','-ExecutionPolicy','RemoteSigned','-EncodedCommand',encoded]
    command=("$ErrorActionPreference='Stop'; try { $taskElevatedProcess=Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ArgumentList @("+
        ','.join(quote(a) for a in args)+
        "); @{status='completed';process_id=$taskElevatedProcess.Id;exit_code=$taskElevatedProcess.ExitCode} | ConvertTo-Json | Set-Content -LiteralPath "+quote(launch_receipt)+
        " -Encoding UTF8; exit $taskElevatedProcess.ExitCode } catch { @{status='error';error=$_.Exception.Message} | ConvertTo-Json | Set-Content -LiteralPath "+quote(launch_receipt)+" -Encoding UTF8; exit 2 }")
    env=dict(os.environ)
    for name in config.get('online_credentials',{}).values():env.pop(name,None)
    trace('tool_requested',phase='deployment_preflight',tool='windows_uac',action='start_local_codesys_services')
    with (directory/'service-uac.stdout.log').open('w') as stdout,(directory/'service-uac.stderr.log').open('w') as stderr:
        process=subprocess.Popen(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],env=env,stdout=stdout,stderr=stderr)
        write_json(directory/'service-uac-process.json',{'launcher_pid':process.pid,'receipt':str(output),'launch_receipt':str(launch_receipt)})
        deadline=time.monotonic()+60
        while not output.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.2)
    if not output.is_file():
        launch=read_json(launch_receipt) if launch_receipt.is_file() else {}
        require(False,'Runtime service startup has no receipt; launcher exit code '+str(process.poll())+
            '; '+str(launch.get('error','inspect service-uac-launch.json and service-uac.elevated.log')))
    report=read_json(output);require(report.get('service_running') is True,'elevated Runtime startup failed: '+str(report.get('error','service not Running')))
    trace('tool_completed',phase='deployment_preflight',tool='windows_uac',action='start_local_codesys_services',receipt=str(output))


def assemble_logic(fragment, scaffold, contract):
    fields={'declaration','implementation','unresolved','notes'}
    require(isinstance(fragment,dict) and fields<=set(fragment)<=fields|{'runtime_plan'},
            'PLC output needs declaration, implementation, unresolved, notes and optional runtime_plan; tools own I/O')
    program=assemble_program({'schema_version':1,'handoff_id':None,'program_name':scaffold['program_name'],
        **{k:fragment[k] for k in fields}},scaffold,contract)
    require(not program['unresolved'],'PLC has unresolved requirements: '+str(program['unresolved']))
    return program


def runtime_review(value,object_ids,evidence_ids,checks):
    require(isinstance(value,dict) and value.get('verdict') in ('pass','repair','uncertain'),'invalid runtime verdict')
    require(isinstance(value.get('summary'),str) and isinstance(value.get('findings'),list),'runtime summary/findings missing')
    for finding in value['findings']:
        require(finding.get('target') in ('plc','scene','tools','unknown') and finding.get('status') in ('issue','uncertain'),'runtime finding target/status missing')
        require(isinstance(finding.get('object_ids'),list) and set(finding['object_ids'])<=object_ids,'runtime finding object unknown')
        refs=finding.get('evidence_ids');require(isinstance(refs,list) and refs and set(refs)<=evidence_ids,'runtime finding needs current evidence')
        require(all(isinstance(finding.get(k),str) and finding[k].strip() for k in ('observed','reason','suggestion')),'runtime finding needs explanation')
    require(value['verdict']!='pass' or not value['findings'],'runtime pass cannot contain findings')
    require(type(value.get('coverage_complete')) is bool,'runtime review needs requirement coverage')
    results=value.get('check_results');require(isinstance(results,list),'runtime review needs check_results')
    required={c['id'] for c in checks};seen=set()
    for result in results:
        require(isinstance(result,dict) and result.get('check_id') in required and result['check_id'] not in seen,'unknown or duplicate runtime check result')
        seen.add(result['check_id'])
        require(result.get('status') in ('pass','issue','uncertain'),'runtime check status missing')
        require(isinstance(result.get('reason'),str) and result['reason'].strip(),'runtime check reason missing')
        refs=result.get('evidence_ids');require(isinstance(refs,list) and set(refs)<=evidence_ids,'runtime check references unknown evidence')
        if result['status']=='pass':require(refs,'passing runtime check needs actual evidence')
    require(seen==required,'runtime review omitted requested checks')
    require(value['verdict']!='pass' or (value['coverage_complete'] and all(r['status']=='pass' for r in results)),
            'runtime pass needs all checks and original-requirement coverage')
    return value


class NativePLCTools:
    def __init__(self, workflow):self.w=workflow;self.active_run=None

    def prepare(self,design,rendered,directory):
        w=self.w;contract=rendered['io_contract'];contract['protocol']=w.config['protocol']
        require(w.plc_mode=='compile' or w.config['factoryio'].get('runtime_control') is True,'runtime_control must be enabled for PLC execution')
        w.state['phase']='device_binding';w.trace('tool_requested',tool='codesys_binding',action='derive_verified_channels')
        effective=verified_bindings(w.config['codesys'],contract)
        contract.update(bindings=effective['bindings'],transport_confirmed=effective['transport_confirmed'],software_load_confirmed=True)
        receipt=read_json(directory/'load-receipt.json')
        attach_component_semantics(rendered['manifest'],contract,receipt['app_version'])
        from .geometry_tools import attach_button_semantics
        attach_button_semantics(contract,rendered['manifest'],receipt)
        scaffold=io_scaffold(effective,contract);signals=runtime_signals(design,scaffold)
        native={o['key']:o.get('component_geometry',{}).get('button') for o in receipt.get('native_geometry',{}).get('objects',[])}
        scaffold['operator_buttons']={o['id']:{'object_key':o['key'],'native_button':native[o['key']]}
            for o in rendered['manifest']['objects'] if native.get(o['key']) and native[o['key']].get('alternate_mode') is False}
        write_json(directory/'io-contract.json',contract);write_json(directory/'manifest.json',rendered['manifest'])
        write_json(directory/'io-scaffold.json',scaffold);write_json(directory/'runtime-signals.json',signals)
        w.trace('tool_completed',tool='codesys_binding',action='derive_verified_channels',binding_count=len(effective['bindings']))
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

    def measure(self,effective,plc_dir,signals,rendered,scaffold,plan):
        self.active_run=RuntimeRun(self.w.config['factoryio'],effective,plc_dir,signals,rendered['manifest'],rendered['io_contract'],self.w.trace)
        result=self.active_run.run(scaffold,plan,seconds=self.w.runtime_seconds,keep_running=self.w.keep_running,reset_mode=self.w.reset_mode)
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
        feedback=None;previous=None;previous_plan=None;contract=rendered['io_contract'];ids={o['id'] for o in design['objects']}
        reused=None;reused_plan=None
        if getattr(w,'resume_source',None):
            old=read_json(w.resume_source/'run.json')
            if old.get('plc_program_sha256') and old.get('plc_dir'):
                old_dir=Path(old['plc_dir']).resolve();require(old_dir.is_relative_to(w.resume_source),'saved PLC is outside source run')
                candidate=read_json(old_dir/'program.json')
                require(canonical_hash(candidate)==old['plc_program_sha256'],'saved PLC program changed')
                if plc_io_design_hash(read_json(old_dir.parent/'io-contract.json'))==plc_io_design_hash(contract):
                    plan_path=old_dir/'runtime-plan.json'
                    if w.plc_mode=='compile' or plan_path.is_file():
                        reused=candidate;reused_plan=read_json(plan_path) if plan_path.is_file() else None
                        if reused_plan is not None:
                            require(canonical_hash(reused_plan)==read_json(old_dir/'versions.json').get('runtime_plan'),
                                    'saved runtime operation plan changed')
                        w.trace('artifact_reused',artifact='plc_logic',source_run=str(w.resume_source))
                    else:feedback={'phase':'runtime_planning','reason':'Archived code has no task-specific operation plan. Preserve its control intent.',
                                   'archived_program':candidate}
        base={'phase':'plc_generation','execution_scope':'Continue this confirmed layout through PLC control and live process validation.',
            'requirement':requirement,'scene_goal':design.get('scene_goal'),'control_intent':design.get('control_intent',[]),
            'deferred_tasks':design.get('deferred_tasks',[]),
            'objects':[{k:o[k] for k in ('id','prefab','function')} for o in design['objects']],
            'io_scaffold':scaffold,'runtime_signals':signals,'plc_mode':w.plc_mode,
            'execution_options':{'keep_running':getattr(w,'keep_running',False),'reset_mode':getattr(w,'reset_mode','warm')},
            'runtime_capabilities':{'actions':['observe','press_button','start_both','stop_both','reset_both'],
                'operator_buttons':scaffold.get('operator_buttons',{}),'default_observation_seconds':getattr(w,'runtime_seconds',30)}}
        from .component_kb import knowledge_base
        base['component_reference']=knowledge_base().references([o['prefab'] for o in design['objects']],'plc')
        for attempt in range(w.max_plc_repairs+1):
            payload={**base,'feedback':feedback,'previous_program':previous,'previous_runtime_plan':previous_plan}
            if attempt==0 and reused is not None:
                program=reused;fragment=None;plan=reused_plan
            else:
                fragment=w.call('plc',PLC_PROMPT,payload)
                try:
                    program=assemble_logic(fragment,scaffold,contract);plan=fragment.get('runtime_plan')
                    if w.plc_mode=='run':plan=validate_runtime_plan(plan,scaffold.get('operator_buttons',{}),w.runtime_seconds)
                except (ContractError,ValueError) as error:
                    w.trace('plc_fragment_rejected',active_role='plc',error=str(error))
                    fragment=w.call('plc',PLC_PROMPT,{**payload,'rejected_fragment':fragment,'format_error':str(error)})
                    program=assemble_logic(fragment,scaffold,contract);plan=fragment.get('runtime_plan')
                    if w.plc_mode=='run':plan=validate_runtime_plan(plan,scaffold.get('operator_buttons',{}),w.runtime_seconds)
            if w.plc_mode=='run':plan=validate_runtime_plan(plan,scaffold.get('operator_buttons',{}),w.runtime_seconds)
            require(previous is None or canonical_hash([program,plan])!=canonical_hash([previous,previous_plan]),'PLC repair returned unchanged code and operation plan')
            previous,previous_plan=program,plan;plc_dir=directory/f'plc-{attempt:03d}';export_program(program,contract,plc_dir)
            if plan is not None:write_json(plc_dir/'runtime-plan.json',plan)
            if fragment is not None:
                write_json(w.training.directory/'derived'/('plc-assembly-'+canonical_hash(program)+'.json'),{
                    'producer_role':'plc','assembly':'model_logic_with_tool_io','candidate_id':w.training.last_candidate_id,
                    'output_sha256':canonical_hash(fragment),'artifact_sha256':canonical_hash(program),'scaffold_sha256':canonical_hash(scaffold)})
            versions={'scene':rendered['manifest']['scene_sha256'],'io':canonical_hash(contract),'plc':canonical_hash(program),
                      'runtime_plan':canonical_hash(plan) if plan is not None else None}
            write_json(plc_dir/'versions.json',versions)
            w.state.update(plc_dir=str(plc_dir),plc_revision=attempt,plc_program_sha256=versions['plc'],
                           runtime_plan_sha256=versions['runtime_plan']);write_json(w.output/'run.json',w.state)
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
            acceptance,evidence=self.tools.measure(effective,plc_dir,signals,rendered,scaffold,plan)
            if acceptance.get('cleanup_error'):
                return w.finish('needs_attention',phase='runtime_control',target='tools',acceptance=acceptance,error=acceptance['cleanup_error'])
            images=[Path(r['path']) for r in evidence['records'] if r['kind']=='image']
            require(images,'current runtime screenshots missing')
            records=summarize_evidence(evidence['records'])
            prompt_records=[{k:v for k,v in r.items() if k not in ('path','receipt_path')} for r in records]
            prompt_records.append({'id':'runtime-acceptance','kind':'acceptance_result','result':acceptance})
            prompt_records.append({'id':'current-plc-deployment','kind':'plc_diagnostics','result':{
                k:result.get(k) for k in ('compiled','started','bindings_verified','task_program_verified','transport_verified',
                    'error_count','warning_count','project_sha256')}})
            from .llm import ModelClient
            if isinstance(w.raw_model,ModelClient):w.raw_model.config['response_schemas']['vision']=RUNTIME_SCHEMA
            review=runtime_review(w.call('vision',RUNTIME_PROMPT,{'phase':'runtime','requirement':requirement,
                'execution_scope':base['execution_scope'],
                'execution_options':base['execution_options'],
                'control_intent':design.get('control_intent',[]),'objects':base['objects'],'acceptance':acceptance,
                'deferred_tasks':base['deferred_tasks'],
                'runtime_plan':plan,'signals':signals,
                'evidence':{'records':prompt_records}},images),ids,{r['id'] for r in prompt_records},plan['checks'])
            write_json(plc_dir/'runtime-feedback.json',review)
            acceptance.update(passed=acceptance['infrastructure']['passed'] and review['verdict']=='pass',
                              process_verdict=review['verdict'],check_results=review['check_results'],
                              coverage_complete=review['coverage_complete'],acceptance_basis='task_based_runtime_visual_review')
            write_json(plc_dir/'acceptance.json',acceptance)
            if acceptance['passed']:
                return w.finish('passed',phase='runtime',live_pipeline_passed=True,acceptance=acceptance,
                    runtime_visual_verdict='pass',left_running=acceptance['left_running'])
            self.tools.stop()
            acceptance['left_running']=False;write_json(plc_dir/'acceptance.json',acceptance)
            if not acceptance['infrastructure']['passed']:
                return w.finish('needs_attention',phase='runtime',target='tools',acceptance=acceptance,feedback=review)
            targets={f['target'] for f in review['findings']}
            if review['verdict']=='uncertain' or targets-{'plc'} or attempt==w.max_plc_repairs:
                return w.finish('needs_attention',phase='runtime',target=next(iter(targets),'plc'),acceptance=acceptance,feedback=review)
            feedback={'phase':'runtime','acceptance':acceptance,'visual_feedback':review,'observed_transitions':records}
        return w.finish('needs_attention',phase='plc_generation',error='PLC repair limit reached')
