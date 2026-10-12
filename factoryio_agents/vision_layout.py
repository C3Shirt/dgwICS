"""Layout input, lightweight checks and workflow routing; no scene generation here.

approved_layout is a separate upstream artifact:
  {scene_goal, objects:[{id,name,type,function,expected_position}],
   approval:{reviewer:'planner',verdict:'approved',task_sha256:layout_task_hash(task)}}.
The scene-generation/planner workflow produces that artifact before scene realization. This receiver
never promotes generated coordinates into an approved layout requirement.
"""
from copy import deepcopy

from .contracts import canonical_hash, nonempty, require


def layout_task_hash(task):
    return canonical_hash({k:v for k,v in task.items() if k!='handoff_id'})


def visual_layout_view(layout):
    """Only the intended goal/functions/relative relationships belong in the visual request."""
    objects=[]
    for obj in layout['objects']:
        row={k:deepcopy(obj[k]) for k in ('id','name','type','function') if k in obj}
        position=obj.get('expected_position')
        row['expected_position']=({k:deepcopy(position[k]) for k in ('relative_to','relation','requirements') if k in position}
                                  if isinstance(position,dict) else None)
        objects.append(row)
    return {'scene_goal':layout['scene_goal'],'objects':objects}


def prepare_layout_input(task, manifest, evidence, approved_layout=None):
    actual=manifest['objects']; ids={o['id'] for o in actual}
    require(len(ids)==len(actual),'actual layout object IDs are duplicated')
    if approved_layout is not None:
        require(isinstance(approved_layout,dict),'approved layout must be an object')
        approval=approved_layout.get('approval',{})
        require(approval.get('reviewer')=='planner' and approval.get('verdict')=='approved',
                'layout reference is not planner-approved')
        require(approval.get('task_sha256')==layout_task_hash(task),'approved layout belongs to another task')
        if 'layout_policy_sha256' in approval:
            from .layout_design import validate_approved_layout
            validate_approved_layout(task,approved_layout)
        if 'layout_reference_sha256' in manifest:
            require(manifest['layout_reference_sha256']==canonical_hash(approved_layout),
                    'actual scene belongs to another approved layout reference')
        goal=nonempty(approved_layout.get('scene_goal'),'approved scene goal')
        expected=deepcopy(approved_layout.get('objects'))
        require(isinstance(expected,list) and all(isinstance(o,dict) for o in expected) and
                {o.get('id') for o in expected}==ids and len(expected)==len(ids),
                'approved layout object IDs do not match actual scene')
        for obj in expected:
            for field in ('id','name','type','function'): nonempty(obj.get(field),'approved object '+field)
            position=obj.get('expected_position')
            require(isinstance(position,dict) and bool(position),'approved expected_position is missing')
            relative=position.get('relative_to',[])
            relative=[relative] if isinstance(relative,str) else relative
            require(isinstance(relative,list) and all(ref in ids and ref!=obj['id'] for ref in relative),
                    'expected position references an unknown or identical object')
        reference={'approved':True,'sha256':canonical_hash(approved_layout), 'task_sha256':layout_task_hash(task)}
    else:
        goal=task.get('requirement','')
        desired={o['id']:o for o in task.get('objects',[])}
        expected=[{'id':o['id'],'name':o['id'],'type':o.get('prefab','unknown'),
            'function':desired.get(o['id'],{}).get('purpose','Function not supplied by planning'),
            'expected_position':None} for o in actual]
        reference={'approved':False,'sha256':None,
            'reason':'Planner-approved expected relative positions have not been supplied.'}
    records=[]
    for record in evidence.get('records',[]):
        kind=record.get('kind')
        if kind=='image': records.append({'id':record['id'],'kind':'image'})
        elif kind=='scene_geometry':
            # Tool outcome stays program-side; bounds and measurements never enter the visual prompt.
            records.append({'id':record['id'],'kind':kind,'result':{k:deepcopy(record['result'][k]) for k in
                ('passed','spatial_checks_applied','reason','scope') if k in record['result']}})
    return {'scene_goal':goal,'objects':expected,'reference':reference}, {'records':records}


def layout_review_schema(payload):
    ids=[o['id'] for o in payload['layout_review']['objects']]
    images=[r['id'] for r in payload['evidence']['records'] if r['kind']=='image']
    def texts(values=None, minimum=0):
        item={'type':'string'} if not values else {'type':'string','enum':values}
        return {'type':'array','items':item,'minItems':minimum}
    fields={'object_ids':texts(ids,1),'related_object_ids':texts(ids),
        'status':{'type':'string','enum':['ok','issue','uncertain']},
        **{k:{'type':'string','minLength':1} for k in ('observed','expected','reason','suggestion')},
        'evidence_images':texts(images,1)}
    return {'type':'object','properties':{'checks':{'type':'array','minItems':1 if ids else 0,
        'items':{'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}}},
        'required':['checks'],'additionalProperties':False}


def validate_layout_checks(value, layout, image_ids):
    require(isinstance(value,dict) and isinstance(value.get('checks'),list),'layout result must contain a checks array')
    checks=deepcopy(value['checks']); ids={o['id'] for o in layout['objects']}; covered=set()
    for check in checks:
        require(isinstance(check,dict),'layout check must be an object')
        affected=check.get('object_ids'); related=check.get('related_object_ids')
        require(isinstance(affected,list) and bool(affected) and all(isinstance(i,str) and i in ids for i in affected),
                'layout check needs actual object IDs')
        require(isinstance(related,list) and all(isinstance(i,str) and i in ids for i in related),
                'layout check references unknown related objects')
        require(check.get('status') in ('ok','issue','uncertain'),'invalid layout check status')
        for field in ('observed','expected','reason','suggestion'): nonempty(check.get(field),'layout check '+field)
        refs=check.get('evidence_images')
        require(isinstance(refs,list) and bool(refs) and all(isinstance(i,str) and i in image_ids for i in refs),
                'layout check must cite actual images')
        covered.update(affected)
    require(covered==ids,'layout checks omit one or more objects')
    return checks


def layout_feedback(value, layout, evidence, versions, handoff=None):
    images={r['id'] for r in evidence['records'] if r['kind']=='image'}
    model_checks=validate_layout_checks(value,layout,images)
    checks=deepcopy(model_checks)
    if not layout['reference']['approved']:
        for check in checks:
            if check['status']=='ok':
                check.update(status='uncertain',model_status='ok',source='framework',
                    reason='The model reported ok, but planner-approved expected positions are missing; this conclusion is unverified.',
                    suggestion='Obtain the planner-approved relative-position reference and recheck this object.')
    findings=[]
    for check in checks:
        if check['status']=='ok': continue
        category,target=('geometry','scene') if check['status']=='issue' else ('unknown','investigate')
        findings.append({**deepcopy(check),'category':category,'target':target,'evidence_ids':check['evidence_images']})
    blockers=[]
    if not layout['reference']['approved']:
        blockers.append({'object_ids':[o['id'] for o in layout['objects']], 'related_object_ids':[],
            'status':'uncertain','observed':'No approved layout reference is available.',
            'expected':'A scene proposal reviewed and approved by the planning agent.',
            'reason':layout['reference']['reason'],'evidence_images':[],
            'suggestion':'Supply the planner-approved object functions and expected relative positions.',
            'category':'requirements','target':'planner','evidence_ids':[],'source':'framework'})
    if not layout['objects']:
        blockers.append({'object_ids':[],'related_object_ids':[],'status':'uncertain',
            'observed':'The actual scene object inventory is empty.','expected':'Objects required by the scene goal.',
            'reason':'There are no scene objects to compare.','evidence_images':[],
            'suggestion':'Provide a generated scene inventory.','category':'requirements','target':'planner',
            'evidence_ids':[],'source':'framework'})
    for record in evidence['records']:
        if record['kind']=='scene_geometry' and (not record['result'].get('spatial_checks_applied') or not record['result'].get('passed')):
            blockers.append({'object_ids':[o['id'] for o in layout['objects']],'related_object_ids':[],
                'status':'uncertain','observed':'Native geometry verification has not passed.',
                'expected':'Applicable native connection checks.',
                'reason':record['result'].get('reason','Native geometry is failed or unchecked.'),'evidence_images':[],
                'suggestion':'Resolve the native geometry measurement/checker result.',
                'category':'communication','target':'tools','evidence_ids':[record['id']],'source':'framework'})
    findings.extend(blockers)
    uncertain=bool(blockers) or any(c['status']=='uncertain' for c in checks)
    issues=any(c['status']=='issue' for c in checks)
    return {'handoff_id':(handoff or {}).get('id'), 'versions':deepcopy(versions),'phase':'scene',
        'verdict':'inconclusive' if uncertain else 'repair' if issues else 'pass',
        'overall_status':'uncertain' if uncertain else 'issue' if issues else 'ok',
        'checks':checks,'model_checks':model_checks,'findings':findings,'framework_checks':blockers,
        'layout_reference':deepcopy(layout['reference'])}


def record_layout_feedback(client, feedback, layout=None, evidence=None):
    """Preserve the raw model checks and a reproducible program-derived receipt."""
    from .training import TrainingRecorder
    from .contracts import read_json
    recorder=getattr(client,'recorder',None)
    if not isinstance(recorder,TrainingRecorder) or not recorder.last_candidate_id: return
    candidate=read_json(recorder.directory/'candidates'/(recorder.last_candidate_id+'.json'))
    receipt={'candidate_id':candidate['candidate_id'],
        'output_sha256':canonical_hash(candidate['structured_output']),
        'artifact_sha256':canonical_hash(feedback),'assembly_mode':'layout_checks_with_tool_metadata'}
    if layout is not None and evidence is not None:
        receipt['request_context']={'layout':deepcopy(layout),'evidence':deepcopy(evidence),
            'versions':deepcopy(feedback['versions']),
            'handoff':{'id':feedback['handoff_id']} if feedback.get('handoff_id') else None}
    recorder._write(recorder.directory/'derived'/('layout-review-'+canonical_hash(feedback)+'.json'),receipt)
