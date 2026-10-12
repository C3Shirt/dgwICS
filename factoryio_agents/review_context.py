"""Role-scoped views constructed only after original handoff hashes are checked."""
from copy import deepcopy

from .contracts import canonical_hash


def layout_model_payload(payload):
    """Final wire allowlist: images plus intended design, no actual geometry or repair history."""
    from .vision_layout import visual_layout_view
    result={'phase':'scene','review_task':deepcopy(payload['review_task']),
        'layout_review':visual_layout_view(payload['layout_review']),
        'evidence':{'records':[{'id':r['id'],'kind':'image'} for r in payload['evidence']['records'] if r['kind']=='image']}}
    if payload.get('format_feedback'):
        # A model-invented coordinate in an invalid reply must not become evidence on retry.
        result['format_feedback']={k:payload['format_feedback'][k] for k in ('error','instruction')
                                   if k in payload['format_feedback']}
    return result


def project_review_payload(payload, role):
    if (role=='scene' and payload.get('scene_stage')=='layout_proposal') or (
            role=='planner' and payload.get('planning_stage')=='layout_review'):
        result=deepcopy(payload)
        result['task']={k:v for k,v in result['task'].items() if k in
                        ('task_id','requirement','scenario_type','objects','process','control_rules','assumptions','unresolved')}
        return result
    if role not in ('vision', 'supervisor') or payload.get('phase') not in ('scene', 'runtime'):
        return payload
    if role=='vision' and payload.get('phase')=='scene' and 'layout_review' in payload:
        # Original artifacts were checked before projection. Static layout review
        # needs its goal, object/reference data and images, not PLC or signal tables.
        return {k:deepcopy(v) for k,v in payload.items() if k in
            ('review_task','layout_review','evidence','versions','phase','format_feedback')}
    result = deepcopy(payload)
    projections = {}
    def select(field, keys):
        value = result.get(field)
        if not isinstance(value, dict): return
        omitted = sorted(set(value) - set(keys))
        if omitted:
            result[field] = {k:v for k,v in value.items() if k in keys}
            projections[field] = {'original_sha256':canonical_hash(value), 'omitted_fields':omitted}
    task_fields = ('schema_version','task_id','requirement','scenario_type','objects','process',
                   'control_rules','unresolved','protocol','execution_contract')
    if payload['phase'] == 'runtime': task_fields += ('acceptance',)
    select('task', task_fields)
    select('plc', ('program_name','declaration','implementation'))
    contract = result.get('io_contract', {})
    if contract:
        original = deepcopy(contract)
        # Remove aliases only: kind/data_type and use_forced_value/forced_value retain their meaning.
        contract['tags']=[{k:v for k,v in t.items() if k not in ('index','type','direction','forced')}
                          for t in contract.get('tags',[])]
        contract.pop('supported_limits', None)
        contract.pop('channel_counts', None)
        contract.pop('channel_offsets', None)
        for item in contract.get('component_semantics', []):
            item.pop('evidence', None); item.pop('bridge_response_sha256', None)
        # Keep complete signal polarity, static force/fault configuration and wire bindings.
        projections['io_contract'] = {'original_sha256':canonical_hash(original),
            'omitted_fields':['tags.index','tags.type','tags.direction','tags.forced (aliases; use_forced_value/forced_value retained)',
                'supported_limits','channel_counts','channel_offsets','component_semantics.evidence','component_semantics.bridge_response_sha256']}
    # Manifest tags duplicate the exact GUID/object table in io_contract.
    for obj in result.get('manifest', {}).get('objects', []): obj.pop('tags', None)
    result['context_projection'] = {'schema_version':1, 'role':role, 'phase':payload['phase'],
        'artifacts':projections, 'manifest_omitted':['objects.tags'],
        'authority':'original artifacts hash-checked before projection; immutable full sources in handoff.inputs',
        'acceptance_and_versions_modified':False}
    return result


def compact_progress_evidence(state, report, resume_plan):
    """Next-action selection does not need candidate lists, full histories or raw diagnostics."""
    state_fields = ('status','phase','active_role','target','versions','error','failure_kind',
                    'scene_revision','plc_revision','scene_dir','plc_dir','reason')
    evidence = [{'id':'run_state','kind':'workflow_state', 'value':{
        **{k:deepcopy(v) for k,v in state.items() if k in state_fields},
        'history_count':len(state.get('history', [])), 'original_sha256':canonical_hash(state)}}]
    if report:
        evidence.append({'id':'result','kind':'audited_result','value':{
            **{k:deepcopy(report[k]) for k in ('workflow_status','live_pipeline_passed','checks','failure',
                'manual_override_detected','framework_changed_during_run') if k in report},
            'original_sha256':canonical_hash(report)}})
    if resume_plan:
        evidence.append({'id':'resume_plan','kind':'validated_recovery','value':{
            **{k:deepcopy(resume_plan[k]) for k in ('reusable','restart_from','invalidation_reasons',
                'source_failure','live_steps_reexecuted','latest_runtime_review') if k in resume_plan},
            'original_sha256':canonical_hash(resume_plan)}})
    return evidence
