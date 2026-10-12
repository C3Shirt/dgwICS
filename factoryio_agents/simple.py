"""Scene-first loop: one design, native tools, one image review, bounded local repair.

There is no Planner, proposal approval, signal DSL or Supervisor in this path.
PLC execution is deliberately a separate next stage, not a scene acceptance claim.
"""
from copy import deepcopy
from pathlib import Path
import json
import re
import time
import uuid

from .contracts import ContractError, canonical_hash, file_hash, read_json, require, write_json
from .geometry_tools import center_scene_layout, loaded_geometry, resolve_relative_layout
from .scene import build_catalog, build_scene, validate_component_settings
from .runtime import collect_evidence, run_hook, utc_now
from .simple_prompts import DESIGN_PROMPT, LAYOUT_PROMPT, PATCH_PROMPT
from .coordination import component_context


def design_to_plan(design,catalog,on_center=None):
    require(isinstance(design,dict) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}',str(design.get('scene_id',''))),'invalid scene_id')
    objects=design.get('objects');require(isinstance(objects,list) and 1<=len(objects)<=100,'objects must contain 1..100 components')
    ids=[o.get('id') for o in objects];require(len(set(ids))==len(ids) and all(isinstance(i,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,60}',i) for i in ids),'invalid or duplicate object ID')
    templates={}
    for obj in objects:
        require(isinstance(obj.get('function'),str) and bool(obj['function'].strip()),'each object needs its function')
        choices=[t for t in catalog['templates'] if t['prefab']==obj.get('prefab')]
        require(bool(choices),'prefab not in current catalog: '+str(obj.get('prefab')))
        templates[obj['id']]=choices[0]
    plan={'scene_id':design['scene_id'],'base_source_id':templates[ids[0]]['source_id'],'objects':[]}
    constraints=[]
    for obj in objects:
        p=obj.get('placement');require(isinstance(p,dict),'each object needs a placement')
        constraints.append({'id':obj['id'],'template_id':templates[obj['id']]['template_id'],'placement':p})
    if all(o['placement'].get('kind')=='absolute' for o in constraints):
        rows=[{'id':o['id'],'template_id':o['template_id'],'position':o['placement']['position'],'yaw_deg':o['placement']['yaw_deg']} for o in constraints]
    else:
        relative=[o for o in constraints if o['placement'].get('kind')!='absolute']
        solved={o['id']:o for o in resolve_relative_layout({**plan,'objects':relative},catalog,verify=False)['objects']}
        rows=[solved[o['id']] if o['id'] in solved else {'id':o['id'],'template_id':o['template_id'],
            'position':o['placement']['position'],'yaw_deg':o['placement']['yaw_deg']} for o in constraints]
    for obj,row in zip(objects,rows):
        row=deepcopy(row);settings=deepcopy(obj.get('settings',[]));template=templates[obj['id']]
        if obj['prefab']=='Emitter':
            material=obj.get('material','BoxS');available=False
            for node in component_context(template.get('configuration',{})).get('settings',[]):
                path=node['path'];attrs=node['attributes']
                if path.startswith(('Emitter/Parts[','Emitter/Bases[')):
                    for key in attrs:
                        if key==material:available=True
                        settings.append({'path':path,'attribute':key,'value':'True' if key==material else 'False'})
            require(available,'selected emitter template cannot supply material '+material)
            settings.extend({'path':'Emitter','attribute':k,'value':v} for k,v in
                [('UpTo','1'),('RandomPartPosition','False'),('RandomPartOrientation','False')])
        # Tool defaults win for single-item emission; explicit other settings retain their order.
        for setting in settings:
            if type(setting.get('value')) is bool:setting['value']='True' if setting['value'] else 'False'
            elif type(setting.get('value')) in (int,float):setting['value']=str(setting['value'])
        merged={(s['path'],s['attribute']):s for s in settings}
        row['settings']=list(merged.values());row['tag_names']={str(t['index']):obj['id']+' '+str(t['index']) for t in template['tags']}
        plan['objects'].append(row)
    plan,centering=center_scene_layout(plan,catalog)
    validate_component_settings(plan['objects'],catalog)
    if centering and on_center:on_center(centering)
    return plan


def apply_local_patch(plan,patch,allowed,catalog):
    require(isinstance(patch,dict) and isinstance(patch.get('changes'),list) and patch['changes'],'repair needs nonempty changes')
    result=deepcopy(plan);by_id={o['id']:o for o in result['objects']};changed=set()
    template={t['template_id']:t for t in catalog['templates']}
    for change in patch['changes']:
        identity=change.get('object_id')
        require(identity in allowed and identity in by_id and identity not in changed,'repair touched an unaffected, unknown or duplicate object')
        require(not set(change)-{'object_id','position','yaw_deg','placement','settings'},'unsupported repair field')
        obj=by_id[identity];changed.add(identity)
        if 'placement' in change:
            anchors=[o for o in result['objects'] if 'conveyor' in catalog['native_geometry']['prefabs'][template[o['template_id']]['prefab']]['component_geometry_local']]
            require(len(anchors)==1 and anchors[0]['id']!=identity,'relative patch needs one unaffected conveyor anchor')
            anchor=anchors[0]
            constraints=[{'id':anchor['id'],'template_id':anchor['template_id'],'placement':{'kind':'anchor','position':anchor['position'],'yaw_deg':anchor['yaw_deg']}},
                {'id':identity,'template_id':obj['template_id'],'placement':change['placement']}]
            resolved=resolve_relative_layout({'objects':constraints},catalog,verify=False)['objects'][1]
            obj.update(position=resolved['position'],yaw_deg=resolved['yaw_deg'])
        for key in ('position','yaw_deg'):
            if key in change:obj[key]=deepcopy(change[key])
        if 'settings' in change:
            settings={(s['path'],s['attribute']):s for s in obj.get('settings',[])}
            settings.update({(s['path'],s['attribute']):s for s in change['settings']});obj['settings']=list(settings.values())
        require(isinstance(obj['position'],list) and len(obj['position'])==3 and all(type(v) is int for v in obj['position']),'repair positions must be integer XYZ')
        require(type(obj['yaw_deg']) in (int,float) and obj['yaw_deg']%90==0,'repair rotation must be cardinal')
    result.pop('centering',None)  # A local repair does not translate unaffected objects again.
    require(result!=plan,'repair did not change the layout')
    return result


def validate_review(value,object_ids,image_ids):
    require(isinstance(value,dict) and value.get('verdict') in ('pass','repair','uncertain'),'invalid visual verdict')
    require(isinstance(value.get('summary'),str) and isinstance(value.get('findings'),list),'visual summary/findings missing')
    value=deepcopy(value)
    for finding in value['findings']:
        require(isinstance(finding,dict) and finding.get('status') in ('issue','uncertain'),'invalid finding status')
        ids=finding.get('object_ids');require(isinstance(ids,list) and set(ids)<=object_ids,'finding references an unknown object')
        images=finding.get('image_ids');require(isinstance(images,list) and bool(images) and set(images)<=image_ids,'finding needs current image evidence')
        require(all(isinstance(finding.get(k),str) and bool(finding[k].strip()) for k in ('observed','reason','suggestion')),'finding needs observation, reason and suggestion')
        if not ids and finding['status']=='issue':
            finding['status']='uncertain';value['verdict']='uncertain'
            value['normalization_note']='Missing object identity prevents a safe local patch; original provider reply remains in training-data.'
    require(value['verdict']!='pass' or not value['findings'],'visual pass cannot contain unresolved findings')
    require(value['verdict']!='repair' or any(f['status']=='issue' and f['object_ids'] for f in value['findings']),'repair needs an object-level issue')
    return value


class NativeSceneTools:
    def __init__(self,config,trace,controller_owned=False):self.config,self.trace,self.controller_owned=config,trace,controller_owned

    def catalog(self,output):
        catalog=build_catalog([Path(p) for p in self.config['scene_sources']]);path=output/'native-prefab-geometry.json'
        self.trace('tool_requested',phase='native_geometry',tool='factoryio_bridge',action='read_prefab_geometry')
        run_hook(self.config['factoryio'].get('geometry_command'),{'output':str(path.resolve())})
        receipt=read_json(path);require(receipt.get('ok') is True and receipt.get('action')=='prefab_geometry','native geometry not confirmed')
        require(isinstance(receipt['data'].get('world_bounds'),dict),'update the Factory I/O bridge to measure factory boundaries')
        catalog['native_geometry']=receipt['data'];write_json(output/'catalog.json',catalog)
        self.trace('tool_completed',phase='native_geometry',tool='factoryio_bridge',action='read_prefab_geometry',receipt=str(path))
        return catalog

    def render(self,plan,catalog,directory):
        directory.mkdir(parents=True,exist_ok=True);scene=directory/'scene.factoryio'
        execution=None
        if self.controller_owned:
            templates={t['template_id']:t['prefab'] for t in catalog['templates']}
            emitters=[o for o in plan['objects'] if templates[o['template_id']]=='Emitter']
            require(len(emitters)==1,'PLC continuation currently needs one single-item emitter')
            execution={'tracking_mode':'single_item','max_in_flight':1,
                'feed':{'control':'plc','items_per_enable':1,'object_id':emitters[0]['id']}}
        built=build_scene(plan,catalog,scene,self.config['protocol'],auto_bool_mappings=True,execution_contract=execution)
        if built['manifest'].get('process_constraint_application'):
            write_json(directory/'controller-ownership.json',built['manifest']['process_constraint_application'])
        for name in ('manifest','io_contract'):
            write_json(directory/(name.replace('_','-')+'.json'),built[name])
        request={'schema_version':1,'request_id':uuid.uuid4().hex,'scene_path':str(scene.resolve()),'scene_sha256':file_hash(scene),
            'versions':{'scene':file_hash(scene),'io':canonical_hash(built['io_contract'])}}
        request_path=directory/'load-request.json';receipt_path=directory/'load-receipt.json';write_json(request_path,request)
        self.trace('tool_requested',phase='scene_load',tool='factoryio_bridge',action='load',scene_sha256=request['scene_sha256'])
        run_hook(self.config['factoryio'].get('load_command'),{'scene':str(scene.resolve()),'output':str(directory.resolve()),
            'request':str(request_path.resolve()),'confirmation':str(receipt_path.resolve())})
        receipt=read_json(receipt_path)
        require(receipt.get('request_id')==request['request_id'] and receipt.get('software_load_confirmed') is True and
            receipt.get('scene_sha256')==request['scene_sha256']==file_hash(scene) and
            Path(receipt.get('scene_path','')).resolve()==scene.resolve(),'loader did not confirm this scene')
        self.trace('tool_completed',phase='scene_load',tool='factoryio_bridge',action='load',receipt=str(receipt_path))
        geometry=loaded_geometry(receipt,built['manifest']);write_json(directory/'geometry-validation.json',geometry)
        rows=geometry.get('measurements',[])
        conveyors=[o for o in rows if 'conveyor' in o.get('component_geometry',{})]
        if len(conveyors)==1:
            direction=conveyors[0]['component_geometry']['conveyor']['direction']
            for obj in rows:
                ray=obj.get('component_geometry',{}).get('sensor')
                if ray and obj.get('prefab') in ('DiffusePhotoelectricSensor','RetroreflectivePhotoelectricSensor') and abs(sum(a*b for a,b in zip(direction,ray['direction'])))>.5:
                    geometry['passed']=False;geometry['issues'].append({'object_id':obj['id'],
                        'error':'presence sensor beam is parallel to conveyor flow; a crossing beam is required'})
            write_json(directory/'geometry-validation.json',geometry)
        self.trace('tool_requested',phase='scene_capture',tool='factoryio_bridge',action='capture_views')
        evidence=collect_evidence(self.config['factoryio'],directory/'visual',request['versions'],built['io_contract'],seconds=0)
        require(evidence.get('reconciliation',{}).get('passed') is True,'loaded tags differ from generated I/O')
        images=[Path(r['path']) for r in evidence['records'] if r['kind']=='image'];require(images,'current screenshots missing')
        for image in images:
            frame=read_json(image.with_name(image.stem+'.bridge.json'))['bridge_response']['data']
            require(Path(frame.get('scene_path','')).resolve()==scene.resolve(),'screenshot belongs to a different scene')
        self.trace('tool_completed',phase='scene_capture',tool='factoryio_bridge',action='capture_views',image_count=len(images))
        return {**built,'geometry':geometry,'evidence':evidence,'images':images}


class SimpleWorkflow:
    def __init__(self,config,output,client=None,tools=None,progress=None,interaction=None,max_repairs=2,inject_fault=None,
                 with_plc=False,keep_running=False,max_plc_repairs=2,runtime_seconds=30,reset_mode='warm',plc_tools=None,start_from='scene',start_local_services=False,plc_mode='run'):
        from .llm import ModelClient
        from .training import TrainingRecorder,RecordingClient,Redactor,configured_secret_values
        from .progress import ModelObserver,ObservedClient
        self.config,self.output=deepcopy(config),Path(output).resolve();self.progress,self.interaction=progress,interaction
        require(type(with_plc) is bool and type(keep_running) is bool and (with_plc or not keep_running),'keep_running needs with_plc')
        require(type(max_plc_repairs) is int and 0<=max_plc_repairs<=5,'max_plc_repairs must be 0..5')
        require(reset_mode in ('warm','cold'),'reset_mode must be warm or cold')
        self.with_plc,self.keep_running,self.max_plc_repairs=with_plc,keep_running,max_plc_repairs
        self.runtime_seconds,self.reset_mode,self.plc_tools=runtime_seconds,reset_mode,plc_tools
        require(start_from in ('scene','plc') and (with_plc or start_from=='scene'),'start_from plc needs with_plc')
        self.start_from=start_from
        self.start_local_services=start_local_services
        require(plc_mode in ('compile','run'),'plc_mode must be compile or run')
        self.plc_mode=plc_mode
        require(type(max_repairs) is int and 0<=max_repairs<=5,'max_repairs must be 0..5');self.max_repairs=max_repairs
        require(inject_fault in (None,'emitter_gap','sensor_parallel'),'unknown experiment fault');self.inject_fault=inject_fault
        from .simple_prompts import LAYOUT_SCHEMA
        llm=deepcopy(config.get('llm',{}));llm['schema_roles']=[]
        llm.setdefault('response_schemas',{})['vision']=LAYOUT_SCHEMA
        raw=client if client is not None else ModelClient(llm)
        self.raw_model=raw
        secrets=configured_secret_values(config)+(raw.secret_values() if isinstance(raw,ModelClient) else [])
        self.redactor=Redactor(secrets)
        self.training=TrainingRecorder(self.output/'training-data',source_kind='injected_client' if client is not None else 'live_model',
            lineage_group='simple-scene',secrets=secrets)
        observer=ModelObserver(self.output,callback=progress,secrets=secrets)
        observer_client=ObservedClient(raw,observer,self.training)
        self.client=RecordingClient(observer_client,self.training)
        if interaction is not None:
            from .interactive import ManualClient,model_info
            self.client=ManualClient(self.client,interaction,info=lambda role,payload:model_info(raw,role,payload))
        self.tools=tools or NativeSceneTools(config,self.trace,controller_owned=with_plc)
        self.state={'schema_version':1,'framework':'simple','status':'created','phase':'scene_design','model_calls':0,
            'scene_revision':0,'plc_executed':False,'live_pipeline_passed':False,'started_at':utc_now()}
        self.state['execution_scope']='scene_and_plc' if with_plc else 'scene_only'
        self.state['started_from']=start_from
        self.state['execution_options']={'with_plc':with_plc,'keep_running':keep_running,'max_plc_repairs':max_plc_repairs,
            'runtime_seconds':runtime_seconds,'reset_mode':reset_mode,'start_local_services':start_local_services,'plc_mode':plc_mode}

    def trace(self,event,**details):
        self.output.mkdir(parents=True,exist_ok=True)
        record=self.redactor.clean({'timestamp':utc_now(),'event':event,'active_role':details.get('active_role','tools'),
            'phase':details.get('phase',self.state['phase']),**details})
        with (self.output/'trace.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(record,ensure_ascii=False)+'\n')
        if self.progress:self.progress(record)
        if self.interaction is not None and event=='tool_requested':self.interaction.before_tool(record)

    def finish(self,status,**details):
        candidates=[read_json(p) for p in (self.output/'training-data/candidates').glob('*.json')]
        usages=[r.get('model_metadata',{}).get('usage') or {} for r in candidates]
        self.state.update(model_calls=len(candidates),tokens={key:sum(u.get(key,0) or 0 for u in usages)
            for key in ('prompt_tokens','completion_tokens','total_tokens')})
        self.state['usage_missing_calls']=sum(type(u.get('total_tokens')) is not int for u in usages)
        self.state.update(status=status,**self.redactor.clean(details),elapsed_seconds=round(time.monotonic()-self.started,3))
        write_json(self.output/'run.json',self.state);self.trace('workflow_transition',**self.state)
        return self.state

    def call(self,role,prompt,payload,images=None):
        self.state['phase']=payload['phase'];self.state['model_calls']+=1
        write_json(self.output/'run.json',self.state)
        return self.client.complete(role,prompt,payload,images) if images else self.client.complete(role,prompt,payload)

    def run(self,requirement,resume_source=None):
        from .component_kb import knowledge_base
        self.started=time.monotonic();self.output.mkdir(parents=True,exist_ok=True)
        require(not (self.output/'requirement.json').exists(),'choose a fresh run directory')
        write_json(self.output/'requirement.json',{'requirement':requirement})
        try:
            require(self.start_from!='plc' or resume_source is not None,'PLC continuation needs a saved scene run')
            catalog=self.tools.catalog(self.output)
            self.state['template_sources']={s['id']:s['sha256'] for s in catalog['sources']}
            self.state['protocol_sha256']=canonical_hash(self.config['protocol'])
            if resume_source:
                source=Path(resume_source).resolve();old=read_json(source/'run.json')
                require(old.get('framework')=='simple','resume needs a simple-framework run')
                require(read_json(source/'requirement.json')['requirement']==requirement,'resume requirement differs')
                require(old.get('template_sources')==self.state['template_sources'] and
                        old.get('protocol_sha256')==self.state['protocol_sha256'],'resume template or protocol configuration changed')
                design=read_json(source/'design.json');plan=read_json(source/'scene-plan.json')
                require(canonical_hash(design)==old['design_sha256'] and canonical_hash(plan)==old['plan_sha256'],'resume artifact changed')
                self.state['source_run']=str(source)
                self.resume_source=source
                if self.start_from=='plc':
                    require(old.get('visual_verdict')=='pass' and old.get('native_geometry_verified') is True,'source layout was not verified')
                    previous_scene=Path(old['scene_dir']).resolve();require(previous_scene.is_relative_to(source),'source scene outside saved run')
                    require(read_json(previous_scene/'scene-feedback.json')['verdict']=='pass' and
                        read_json(previous_scene/'geometry-validation.json')['passed'] is True and
                        canonical_hash(read_json(previous_scene/'scene-plan.json'))==old['plan_sha256'],'saved layout evidence no longer matches')
                    self.trace('artifact_reused',artifact='layout_review',source_run=str(source),scope='archived layout only; current native and runtime evidence required')
                if old.get('experiment_fault'): self.state['experiment_fault']=old['experiment_fault']
                self.trace('artifact_reused',artifact='scene_design',source_run=str(source))
            else:
                candidates=[]
                for prefab in sorted({t['prefab'] for t in catalog['templates']}):
                    metric=catalog.get('native_geometry',{}).get('prefabs',{}).get(prefab,{})
                    candidate={'prefab':prefab,'relative_conveyor_supported':'conveyor' in metric.get('component_geometry_local',{})}
                    if prefab in ('DiffusePhotoelectricSensor','Remover'):
                        t=next(t for t in catalog['templates'] if t['prefab']==prefab)
                        candidate['settings']=[{'path':s['path'],'attributes':list(s['attributes'])} for s in component_context(t['configuration'])['settings']]
                    candidates.append(candidate)
                request={'phase':'scene_design','scene_stage':'design','requirement':requirement,
                    'available_components':candidates,'component_reference':knowledge_base().references([c['prefab'] for c in candidates],'planner')}
                design=self.call('scene',DESIGN_PROMPT,request)
                require(not design.get('unresolved'),'unsupported requirement: '+str(design.get('unresolved')))
                write_json(self.output/'design.json',design)
                try:plan=design_to_plan(design,catalog,on_center=lambda receipt:write_json(self.output/'centering.json',receipt))
                except (ContractError,ValueError) as error:
                    self.trace('design_rejected',error=str(error))
                    design=self.call('scene',DESIGN_PROMPT,{**request,'previous_design':design,'format_error':str(error)})
                    require(not design.get('unresolved'),'unsupported requirement: '+str(design.get('unresolved')))
                    plan=design_to_plan(design,catalog,on_center=lambda receipt:write_json(self.output/'centering.json',receipt))
            if self.inject_fault:
                templates={t['template_id']:t['prefab'] for t in catalog['templates']}
                target=next((o for o in plan['objects'] if templates[o['template_id']]==('Emitter' if self.inject_fault=='emitter_gap' else 'DiffusePhotoelectricSensor')),None)
                require(target is not None,'experiment fault has no compatible object')
                if self.inject_fault=='emitter_gap':
                    conveyor=next(o for o in plan['objects'] if 'conveyor' in templates[o['template_id']].lower())
                    # Translate across the actual cardinal flow, not a hard-coded screen direction.
                    axis=0 if conveyor['yaw_deg']%180==0 else 2
                    target['position'][axis]+=24
                else:target['yaw_deg']=(target['yaw_deg']+90)%360
                self.state['experiment_fault']={'kind':self.inject_fault,'object_id':target['id']}
                self.trace('experiment_fault_injected',**self.state['experiment_fault'])
            write_json(self.output/'design.json',design)
            self.state['design_sha256']=canonical_hash(design)
            for attempt in range(self.max_repairs+1):
                self.state.update(scene_revision=attempt,plan_sha256=canonical_hash(plan),phase='scene_load')
                write_json(self.output/'scene-plan.json',plan);write_json(self.output/'run.json',self.state)
                directory=self.output/f'scene-{attempt:03d}';write_json(directory/'scene-plan.json',plan)
                rendered=self.tools.render(plan,catalog,directory)
                if self.start_from=='plc':
                    require(rendered['geometry'].get('passed') is True and rendered['geometry'].get('spatial_checks_applied') is True,
                            'current native geometry failed; PLC continuation cannot change an approved layout')
                    self.state.update(scene_dir=str(directory),visual_verdict='pass',layout_visual_source='archived',
                        native_geometry_verified=True,phase='plc_generation')
                    # Keep the archived visual result explicitly separate from the current runtime review.
                    archived=read_json(previous_scene/'scene-feedback.json')
                    write_json(directory/'scene-feedback.json',{**archived,'source_run':str(source),'evidence_scope':'archived unchanged layout'})
                    from .simple_plc import SimplePLCStage
                    return SimplePLCStage(self,self.plc_tools).run(requirement,design,rendered,directory)
                images=rendered['images'];records=[r for r in rendered['evidence']['records'] if r['kind']=='image']
                image_ids={r['id'] for r in records};ids={o['id'] for o in design['objects']}
                payload={'phase':'scene','requirement':requirement,
                    'objects':[{k:o[k] for k in ('id','prefab','function')} for o in design['objects']],
                    'component_reference':knowledge_base().references([o['prefab'] for o in design['objects']],'vision'),
                    'evidence':{'records':[{'id':r['id'],'kind':'image'} for r in records]}}
                review=validate_review(self.call('vision',LAYOUT_PROMPT,payload,images),ids,image_ids)
                write_json(directory/'scene-feedback.json',review);geometry=rendered['geometry']
                if review['verdict']=='pass' and geometry.get('passed') is True and geometry.get('spatial_checks_applied') is True:
                    if self.with_plc:
                        self.state.update(scene_dir=str(directory),visual_verdict='pass',native_geometry_verified=True,phase='plc_generation')
                        self.trace('workflow_transition',status='scene_verified',scene_dir=str(directory))
                        from .simple_plc import SimplePLCStage
                        return SimplePLCStage(self,self.plc_tools).run(requirement,design,rendered,directory)
                    return self.finish('scene_verified',scene_dir=str(directory),visual_verdict='pass',native_geometry_verified=True)
                if not geometry.get('spatial_checks_applied'):
                    return self.finish('needs_attention',target='tools',error='Native geometry measurement is missing; image review is diagnostic only.',scene_dir=str(directory))
                if review['verdict']=='uncertain':return self.finish('needs_attention',target='vision',feedback=review,scene_dir=str(directory))
                allowed={i for f in review['findings'] for i in f['object_ids']}|{i['object_id'] for i in geometry.get('issues',[]) if i.get('object_id') in ids}
                if attempt==self.max_repairs or not allowed:return self.finish('needs_attention',target='scene',feedback=review,diagnostics=geometry,scene_dir=str(directory))
                patch_request={'phase':'scene_repair','scene_stage':'local_repair','requirement':requirement,
                    'current_layout':plan,'allowed_object_ids':sorted(allowed),'visual_feedback':review,'native_issues':geometry.get('issues',[]),
                    'native_geometry':{**{k:catalog['native_geometry'][k] for k in ('coordinate_system','world_bounds')},
                        'prefabs':{o['prefab']:catalog['native_geometry']['prefabs'][o['prefab']] for o in design['objects']
                                   if o['id'] in allowed or 'conveyor' in o['prefab'].lower()}},'objects':payload['objects']}
                patch=self.call('scene',PATCH_PROMPT,patch_request)
                write_json(directory/'local-patch.json',patch)
                try: repaired=apply_local_patch(plan,patch,allowed,catalog)
                except (ContractError,ValueError) as error:
                    self.trace('patch_rejected',error=str(error),active_role='scene')
                    patch=self.call('scene',PATCH_PROMPT,{**patch_request,'rejected_patch':patch,'format_error':str(error)})
                    write_json(directory/'corrected-patch.json',patch)
                    repaired=apply_local_patch(plan,patch,allowed,catalog)
                plan=repaired
        except KeyboardInterrupt:return self.finish('cancelled',error='Interrupted; use --resume to retain the design.')
        except Exception as error:return self.finish('needs_attention',error=str(error),error_type=type(error).__name__)

    def resume(self,source):
        source=Path(source).resolve()
        return self.run(read_json(source/'requirement.json')['requirement'],resume_source=source)
