"""Proposal/review contracts, bounded role subtasks, and reproducible approval provenance."""
from copy import deepcopy

from .contracts import ContractError, canonical_hash, nonempty, read_json, require, write_json
from .vision_layout import layout_task_hash


def semantic(value):
    return {k:v for k,v in value.items() if k!='handoff_id'}


def proposal_hash(proposal):
    return canonical_hash(semantic(proposal))


def layout_policy_hash():
    from .prompts import policy_manifest
    hashes=policy_manifest()['prompt_sha256']
    return canonical_hash({k:hashes[k] for k in ('scene_layout_proposal','planner_layout_review')})


def validate_layout_proposal(value, task):
    require(isinstance(value,dict),'layout proposal must be an object')
    require(set(semantic(value))=={'scene_goal','objects'},'layout proposal returns only scene_goal and objects')
    nonempty(value.get('scene_goal'),'layout scene_goal')
    expected={o['id']:o['prefab'] for o in task['objects']}
    objects=value.get('objects')
    require(isinstance(objects,list) and all(isinstance(o,dict) for o in objects), 'layout objects must be an array')
    require(len(objects)==len(expected) and {o.get('id') for o in objects}==set(expected),
            'layout proposal must contain every task object exactly once')
    for obj in objects:
        require(set(obj)=={'id','name','type','function','expected_position'}, 'layout proposal object has unexpected fields')
        for key in ('id','name','type','function'): nonempty(obj.get(key),'layout object '+key)
        require(obj['type']==expected[obj['id']], 'layout proposal substituted the requested object type')
        position=obj['expected_position']
        require(isinstance(position,dict) and set(position)=={'relative_to','relation','requirements'},
                'expected_position needs relative_to, relation and requirements; no generated coordinates')
        refs=position['relative_to']
        require(isinstance(refs,list) and all(isinstance(i,str) and i in expected and i!=obj['id'] for i in refs)
                and len(set(refs))==len(refs),'expected_position references an unknown, duplicated or identical object')
        nonempty(position['relation'],'expected_position relation')
        requirements=position['requirements']
        require(isinstance(requirements,list) and bool(requirements), 'expected_position requirements are missing')
        for requirement in requirements: nonempty(requirement,'expected_position requirement')
    return value


def validate_layout_review(value, proposal):
    require(isinstance(value,dict) and set(semantic(value))=={'verdict','summary','findings'},
            'layout review returns only verdict, summary and findings')
    require(value.get('verdict') in ('approved','revise','uncertain'),'invalid layout review verdict')
    nonempty(value.get('summary'),'layout review summary')
    findings=value.get('findings'); ids={o['id'] for o in proposal['objects']}
    require(isinstance(findings,list),'layout review findings must be an array')
    require((not findings) if value['verdict']=='approved' else bool(findings),
            'approved review must have no findings; revise/uncertain need specific findings')
    for finding in findings:
        require(isinstance(finding,dict) and set(finding)=={'object_ids','reason','suggestion'},
                'layout review finding needs object_ids, reason and suggestion')
        refs=finding['object_ids']
        require(isinstance(refs,list) and bool(refs) and all(isinstance(i,str) and i in ids for i in refs),
                'layout review finding must identify actual objects')
        for key in ('reason','suggestion'): nonempty(finding.get(key),'layout finding '+key)
    return value


def compose_approved_layout(task, proposal, review, *, policy_sha256=None):
    validate_layout_proposal(proposal,task); validate_layout_review(review,proposal)
    require(review['verdict']=='approved','planner has not approved the layout proposal')
    return {**deepcopy(semantic(proposal)), 'approval':{'reviewer':'planner','verdict':'approved',
        'task_sha256':layout_task_hash(task),'proposal_sha256':proposal_hash(proposal),
        'review_sha256':canonical_hash(semantic(review)),
        'layout_policy_sha256':policy_sha256 or layout_policy_hash()}}


def validate_approved_layout(task, approved, proposal=None, review=None):
    require(isinstance(approved,dict) and set(approved)=={'scene_goal','objects','approval'},'invalid approved layout artifact')
    validate_layout_proposal({k:approved[k] for k in ('scene_goal','objects')},task)
    approval=approved['approval']
    require(isinstance(approval,dict) and approval.get('reviewer')=='planner' and approval.get('verdict')=='approved',
            'layout reference is not planner-approved')
    require(approval.get('task_sha256')==layout_task_hash(task),'approved layout belongs to another task')
    require(approval.get('layout_policy_sha256')==layout_policy_hash(),'layout approval policy changed')
    require(approval.get('proposal_sha256')==proposal_hash({k:approved[k] for k in ('scene_goal','objects')}),
            'approved layout proposal changed')
    require(isinstance(approval.get('review_sha256'),str) and len(approval['review_sha256'])==64,'layout review hash missing')
    if proposal is not None or review is not None:
        require(proposal is not None and review is not None,'both proposal and planner review are required')
        require(approved==compose_approved_layout(task,proposal,review),'approved layout differs from its proposal/review')
    return approved


def call_layout_stage(client, role, prompt, payload, validate, handoff=None):
    from .agents import complete_role, rejected_reply
    from .memory import planner_stage_cache
    from .llm import ModelServiceError, ModelOutputError
    from .execution import ExecutionLimitError
    recorder=getattr(client,'recorder',None)
    stage=payload.get('scene_stage') or payload['planning_stage']
    cache_inputs=deepcopy(payload)
    for key in ('task','layout_proposal','previous_proposal','review_feedback'):
        if isinstance(cache_inputs.get(key),dict): cache_inputs[key]=semantic(cache_inputs[key])
    cached=planner_stage_cache(recorder,stage,cache_inputs,validate,role=role)
    if cached: return cached[0]
    for attempt in range(3):
        value=None
        try:
            value=complete_role(client,role,prompt,payload,handoff,metadata_from_request=True)
            validate(value)
            if recorder is not None:
                recorder.event('layout_subtask_accepted',{'stage':stage,'artifact_sha256':canonical_hash(value)})
                planner_stage_cache(recorder,stage,cache_inputs,validate,value,role=role)
            return value
        except (ModelServiceError,ModelOutputError,ExecutionLimitError): raise
        except ContractError as error:
            if attempt==2: raise
            feedback={'error':str(error),'previous_response':value,
                'rejected_reply':rejected_reply(client) if value is None else None,
                'instruction':'Correct this output contract and return the complete subtask JSON.'}
            if recorder is not None: recorder.event('layout_substage_rejected',{'stage':stage,'feedback':feedback})
            payload={**payload,'format_feedback':feedback}


def record_layout_approval(client, task, proposal, review, approved):
    from .training import TrainingRecorder
    recorder=getattr(client,'recorder',None)
    if not isinstance(recorder,TrainingRecorder): return
    candidates=[read_json(p) for p in (recorder.directory/'candidates').glob('*.json')]
    sources=[]
    for role,value in (('scene',proposal),('planner',review)):
        def belongs(record):
            payload=record.get('payload',{}); view=payload.get('task',{})
            if not {'requirement','objects'}<=set(view) or not all(task.get(k)==v for k,v in view.items()): return False
            if payload.get('layout_policy_sha256')!=approved['approval']['layout_policy_sha256']: return False
            if role=='planner':
                return (payload.get('task_sha256')==layout_task_hash(task) and
                        isinstance(payload.get('layout_proposal'),dict) and
                        proposal_hash(payload['layout_proposal'])==proposal_hash(proposal))
            return True
        matches=[r for r in candidates if r['role']==role and r.get('structured_output')==value and belongs(r)]
        require(bool(matches),'layout approval has no recorded '+role+' source')
        record=matches[-1]
        sources.append({'role':role,'candidate_id':record['candidate_id'],'output_sha256':canonical_hash(value)})
    receipt={'sources':sources,'task_sha256':layout_task_hash(task),'artifact_sha256':canonical_hash(approved)}
    write_json(recorder.directory/'derived'/('layout-approval-'+canonical_hash(approved)+'.json'),receipt)
    recorder.event('layout_approval_assembled',receipt)


def audit_layout_approval(output, approved, candidates):
    """Reconstruct from immutable role replies and the review request, not a model approval claim."""
    proof=output/'training-data/derived'/('layout-approval-'+canonical_hash(approved)+'.json')
    if not proof.is_file(): return []
    try:
        receipt=read_json(proof); by_id={r['candidate_id']:r for r in candidates}
        sources=receipt['sources']
        require(len(sources)==2 and {s['role'] for s in sources}=={'scene','planner'},'invalid layout source roles')
        records={s['role']:by_id[s['candidate_id']] for s in sources}
        for source in sources:
            record=records[source['role']]
            require(record['role']==source['role'] and record.get('status')=='response_received'
                    and canonical_hash(record['structured_output'])==source['output_sha256'],'layout source changed')
        proposal=records['scene']['structured_output']; review=records['planner']['structured_output']
        payload=records['planner']['payload']; task=read_json(output/'task.json')
        require(payload['task_sha256']==layout_task_hash(task)==receipt['task_sha256'],'layout review task changed')
        require(all(task.get(k)==v for k,v in payload['task'].items()),'layout review task view changed')
        require(proposal_hash(payload['layout_proposal'])==proposal_hash(proposal),'planner reviewed another proposal')
        policy=payload['layout_policy_sha256']
        require(compose_approved_layout(task,proposal,review,policy_sha256=policy)==approved
                and receipt['artifact_sha256']==canonical_hash(approved),'assembled layout changed')
        return list(records.values())
    except (ValueError,KeyError,TypeError): return []
