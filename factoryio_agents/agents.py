"""Four model roles with versioned prompts and explicit orchestrator handoffs."""
from copy import deepcopy
from pathlib import Path

from .contracts import ContractError, canonical_hash, file_hash, require, validate_feedback, validate_task
from .coordination import component_context, model_review_evidence, validate_diagnoses, validate_execution_contract
from .llm import ModelServiceError, ModelOutputError
from .execution import ExecutionLimitError
from .scene import SceneError, validate_component_settings
from .prompts import PROMPTS, VISION_PROMPTS, component_knowledge, policy_manifest

PLANNER_PROMPT, SCENE_PROMPT, PLC_PROMPT = (
    PROMPTS[r] for r in ("planner", "scene", "plc"))


def catalog_for_model(catalog: dict) -> dict:
    result = {"sources": [{k: s[k] for k in ("id", "path", "object_count")} for s in catalog["sources"]],
        "templates": [{**{k: t[k] for k in
            ("template_id", "source_id", "prefab", "position", "rotation", "tags")},
            "component_context": component_context(t.get("configuration", {}))} for t in catalog["templates"]]}
    if catalog.get('native_geometry'):
        native=catalog['native_geometry'];prefabs={t['prefab'] for t in catalog['templates']}
        result['native_geometry']={**native,'prefabs':{k:v for k,v in native['prefabs'].items() if k in prefabs}}
    return result


def catalog_for_planner(catalog):
    """Planner needs available capabilities; exact XML/template placement belongs to Agent2."""
    prefabs = {}
    for template in catalog['templates']:
        context = component_context(template.get('configuration', {}))
        item = prefabs.setdefault(template['prefab'], {'prefab':template['prefab'], 'variants':[]})
        capabilities = {'operating_mode':context['operating_mode'], 'signals':[
            {'index':t['index'], 'kind':t['kind'], 'data_type':t['data_type']} for t in template['tags']],
            'editable_components':[{ 'component':s['path'], 'attributes':list(s['attributes']) }
                                   for s in context['settings'] if s['path'].split('/')[-1].split('[')[0]
                                   in ('Emitter', 'Parts', 'Bases', 'Sensor', 'Encoder')]}
        if capabilities not in item['variants']: item['variants'].append(capabilities)
    measured=[k for k,v in catalog.get('native_geometry',{}).get('prefabs',{}).items()
              if 'conveyor' in v.get('component_geometry_local',{})]
    return {'prefabs':list(prefabs.values()), 'view':'capability_summary',
            'layout_capabilities':{'relative_conveyor_solver_prefabs':measured,
                'other_prefabs':'absolute-coordinate layout and fresh visual/physical verification'},
            'device_io_capacity':catalog.get('device_io_capacity'),
            'exact_templates':'selected by scene author from immutable full catalog'}


def validate_planning_capacity(task, catalog):
    capacity = catalog.get('device_io_capacity')
    if not capacity or task.get('unresolved'): return
    counts = {'input':capacity.get('system_bool_inputs_reserved',0),'output':0}
    for obj in task['objects']:
        variants = [t for t in catalog['templates'] if t['prefab'] == obj['prefab'] and
                    all(s['data_type'] in capacity['data_types'] for s in t['tags'])]
        require(bool(variants), 'no inspected compatible I/O variant for ' + obj['prefab'])
        for kind in counts:
            counts[kind] += min(sum(t['kind'] == kind for t in v['tags']) for v in variants)
    require(all(counts[k] <= capacity['counts'][k] for k in counts),
            'minimum scene I/O exceeds inspected device capacity: '+str(counts)+' versus '+str(capacity['counts']))


def rejected_reply(client):
    from .training import TrainingRecorder
    recorder = getattr(client, 'recorder', None)
    if isinstance(recorder, TrainingRecorder) and recorder.last_candidate_id:
        from .contracts import read_json
        record = read_json(recorder.directory / 'candidates' / (recorder.last_candidate_id + '.json'))
        text = record.get('response_text')
        return text[:20000] if isinstance(text, str) else None
    return None


def complete_role(client, role, prompt, payload, handoff=None, images=None, metadata_from_request=False):
    request_task_id=(payload.get('task') or {}).get('task_id')
    layout_design=(role=='scene' and payload.get('scene_stage')=='layout_proposal') or (
        role=='planner' and payload.get('planning_stage')=='layout_review')
    if metadata_from_request:
        require((role=='vision' and payload.get('phase')=='scene') or layout_design,
                'program-owned metadata is limited to layout design/review')
    if handoff is not None:
        require(canonical_hash({k: v for k, v in handoff.items() if k != "id"}) == handoff.get("id"),
                "handoff content differs from its ID")
        require(handoff.get("recipient") == role, "handoff has wrong recipient")
        require(handoff.get("policy") == policy_manifest(), "handoff uses another prompt policy")
        for field, version_key in (("task", "task"), ("io_contract", "io"), ("plc", "plc")):
            if payload.get(field) is not None and handoff["versions"].get(version_key) is not None:
                require(canonical_hash(payload[field]) == handoff["versions"][version_key],
                        "handoff payload belongs to another artifact version")
        for input_ in handoff["inputs"]:
            require(file_hash(Path(input_["path"])) == input_["sha256"], "handoff input changed before model call")
    # Upstream artifact correlation IDs are not part of the next role's design task.
    # Validate originals above, then give a labeled view with one unambiguous current handoff.
    payload = deepcopy(payload)
    omitted = []
    for field in ('task','task_spec','plc','previous_plan','previous_program','previous_task','approved_layout','review_feedback',
                  'layout_proposal','previous_proposal'):
        if isinstance(payload.get(field), dict) and 'handoff_id' in payload[field]:
            payload[field].pop('handoff_id'); omitted.append(field+'.handoff_id')
    for field in ('stage_rejection','format_feedback'):
        previous = (payload.get(field) or {}).get('previous_response')
        if isinstance(previous, dict) and 'handoff_id' in previous:
            previous.pop('handoff_id'); omitted.append(field+'.previous_response.handoff_id')
    if omitted:
        payload['input_projection'] = {'omitted_upstream_metadata':omitted,
                                       'original_artifacts':'hash-verified handoff.inputs', 'semantic_fields_changed':False}
    from .review_context import project_review_payload
    payload = project_review_payload(payload, role)
    payload = {**payload, "handoff": handoff, "policy": policy_manifest()}
    if not metadata_from_request: payload['component_knowledge'] = component_knowledge()
    from contextlib import nullcontext
    from .training import TrainingRecorder
    recorder=getattr(client,'recorder',None)
    model_payload=payload
    capture=nullcontext()
    if role=='vision' and payload.get('phase')=='scene':
        from .review_context import layout_model_payload
        model_payload=layout_model_payload(payload)
        if isinstance(recorder,TrainingRecorder):
            capture=recorder.capture_context({'handoff':handoff,'versions':payload['versions'],
                'policy':payload['policy'],'task_id':request_task_id})
    with capture:
        value = client.complete(role, prompt, model_payload, images) if images else client.complete(role, prompt, model_payload)
    require(isinstance(value, dict), "role response must be object")
    if metadata_from_request and not layout_design:
        metadata={'versions':payload['versions'],'phase':payload['phase']}
        if handoff is not None: metadata['handoff_id']=handoff['id']
        for key,expected in metadata.items():
            require(key not in value or value[key]==expected,'layout response has conflicting '+key)
        value={**value,**deepcopy(metadata)}
    if handoff is not None:
        # A synchronous design call already belongs to this hash-verified request.
        # Keep its raw business JSON intact; an explicitly conflicting ID still fails.
        require(value.get('handoff_id') in (None,handoff['id']) if metadata_from_request and layout_design else
                value.get('handoff_id')==handoff['id'],'role response belongs to another handoff')
        for input_ in handoff["inputs"]:
            require(file_hash(Path(input_["path"])) == input_["sha256"], "handoff input changed during model call")
    return value


class PlannerAgent:
    def __init__(self, client, staged=False, staged_measurements=False):
        self.client, self.staged, self.staged_measurements = client, staged, staged_measurements

    def review_layout(self, task, proposal, catalog, handoff=None):
        from .layout_design import call_layout_stage, validate_layout_proposal, validate_layout_review, layout_policy_hash
        from .vision_layout import layout_task_hash
        from .layout_prompts import LAYOUT_REVIEW_PROMPT
        validate_layout_proposal(proposal,task)
        scoped={**catalog,'templates':[t for t in catalog['templates'] if t['prefab'] in {o['prefab'] for o in task['objects']}]}
        payload={'task':task,'layout_proposal':proposal,'catalog':catalog_for_planner(scoped),
                 'planning_stage':'layout_review','layout_policy_sha256':layout_policy_hash(),
                 'task_sha256':layout_task_hash(task)}
        return call_layout_stage(self.client,'planner',LAYOUT_REVIEW_PROMPT,payload,
                                 lambda value:validate_layout_review(value,proposal),handoff)

    def plan(self, requirement: str, catalog: dict, protocol: dict, handoff=None) -> dict:
        if self.staged: return self._staged_plan(requirement, catalog, protocol, handoff)
        from .verification import VERIFIERS
        payload = {"requirement": requirement, "protocol": protocol, "catalog": catalog_for_planner(catalog),
                   'available_verifiers': VERIFIERS}
        for attempt in range(3):
            value = None
            try:
                value = complete_role(self.client, "planner", PLANNER_PROMPT, payload, handoff)
                task = validate_task(value)
                require(task["protocol"] == protocol, "planner changed configured protocol")
                prefabs = {t["prefab"] for t in catalog["templates"]}
                require(all(o["prefab"] in prefabs for o in task["objects"]), "planner selected unavailable prefab")
                validate_planning_capacity(task,catalog)
                validate_execution_contract(task)
                if task['unresolved'] and attempt < 2:
                    feedback = {'phase': 'planning_relevance', 'unresolved': task['unresolved'],
                        'instruction': 'Recheck these blockers against the original requirement. Do not invent optional features '
                        'and ask the user to specify them. Ordinary template design choices belong in assumptions, '
                        'downstream checks in verification_tasks. Preserve actual missing critical requirements/capabilities '
                        'and never mark unobserved verification as passed. Return a complete reconsidered task.'}
                    recorder = getattr(self.client, 'recorder', None)
                    if recorder is not None:
                        recorder.event('planner_blockers_review', {'attempt': attempt, 'feedback': feedback,
                                                                  'artifact_sha256': canonical_hash(value)})
                    payload = {**payload, 'previous_task': value, 'feedback': feedback}
                    continue
                return task
            except (ModelServiceError, ModelOutputError, ExecutionLimitError):
                raise
            except ContractError as error:
                if attempt == 2:
                    raise
                feedback = {"phase": "planner_contract", "error": str(error),
                            'rejected_reply': rejected_reply(self.client) if value is None else None,
                            "instruction": "Return a complete valid JSON task. Escape newlines/quotes inside strings. "
                            "protocol must exactly equal the supplied JSON object; preserve original requirements."}
                recorder = getattr(self.client, 'recorder', None)
                if recorder is not None:
                    recorder.event('planner_contract_rejected', {'attempt': attempt, 'feedback': feedback,
                                                                  'artifact_sha256': canonical_hash(value)})
                payload = {**payload, "previous_task": value, "feedback": feedback}

    def _staged_plan(self, requirement, catalog, protocol, handoff):
        from .verification import VERIFIERS
        from .contracts import write_json, acceptance_array
        spec_fields = {'schema_version','handoff_id','task_id','requirement','scenario_type','objects','process',
                       'control_rules','assumptions','unresolved','protocol'}
        base = {'requirement':requirement, 'protocol':protocol, 'catalog':catalog_for_planner(catalog)}
        sources = []
        recorder = getattr(self.client, 'recorder', None)
        def call_stage(stage, payload, validate):
            from .memory import planner_stage_cache
            cache_inputs = {**base, **payload, 'planning_stage': stage}
            cached = planner_stage_cache(recorder, stage, cache_inputs, validate)
            if cached:
                value, ref = cached
                sources.append({k: ref[k] for k in ('stage', 'candidate_id', 'output_sha256')})
                return value
            for attempt in range(3):
                value = None
                try:
                    value = complete_role(self.client, 'planner', PLANNER_PROMPT,
                                          {**base, **payload, 'planning_stage':stage}, handoff)
                    validate(value)
                    if recorder is not None:
                        recorder.event('planner_substage_accepted', {'stage':stage, 'artifact_sha256':canonical_hash(value)})
                        sources.append({'stage':stage, 'candidate_id':recorder.last_candidate_id,
                                        'output_sha256':canonical_hash(value)})
                        planner_stage_cache(recorder, stage, cache_inputs, validate, value)
                    return value
                except (ModelServiceError, ModelOutputError, ExecutionLimitError): raise
                except ContractError as error:
                    if attempt == 2: raise
                    if recorder is not None: recorder.event('planner_substage_rejected', {'stage':stage, 'error':str(error)})
                    payload = {**payload, 'stage_rejection':{'error':str(error), 'previous_response':value,
                        'rejected_reply':rejected_reply(self.client) if value is None else None,
                        'instruction':'Correct only this planning subtask. Preserve original requirements and supplied spec/contract. Return valid JSON.'}}
        def validate_spec(value):
            require(set(value) == spec_fields, 'spec stage must return only the specified task fields')
            validate_task(value, specification_only=True)
            require(value['protocol'] == protocol, 'planner changed configured protocol')
            require(value['requirement'] == requirement, 'copy the original requirement exactly in spec')
            require(all(o['prefab'] in {t['prefab'] for t in catalog['templates']} for o in value['objects']),
                    'planner selected unavailable prefab')
            validate_planning_capacity(value,catalog)
        spec = call_stage('spec', {}, validate_spec)
        def validate_execution(value):
            require(set(value) == {'handoff_id','execution_contract'}, 'execution stage returns only execution_contract and handoff_id')
            validate_execution_contract({**spec,'execution_contract':value['execution_contract']})
        execution = call_stage('execution', {'task_spec':spec}, validate_execution)
        if self.staged_measurements:
            from .signal_validation import validate_signal_contract
            from .signal_validation import BUTTON_PREFABS, native_probe_constraints
            context = {'task_spec':spec,'execution_contract':execution['execution_contract'],
                       'available_native_buttons':[o['id'] for o in spec['objects'] if o['prefab'] in BUTTON_PREFABS]}
            def validate_signals(value):
                require(set(value) == {'handoff_id','signals'}, 'measurement_signals returns only signals and handoff_id')
                validate_signal_contract({'signals':value['signals']},spec,signals_only=True)
            signals = call_stage('measurement_signals',context,validate_signals)
            def validate_probes(value):
                require(set(value) == {'handoff_id','probes'}, 'measurement_probes returns only probes and handoff_id')
                validate_signal_contract({'signals':signals['signals'],'probes':value['probes']},spec,probes_only=True)
            probes = call_stage('measurement_probes',{**context,'approved_signals':signals['signals'],
                'native_probe_constraints':native_probe_constraints()},validate_probes)
            def compose(value):
                return {**spec,'execution_contract':execution['execution_contract'],
                    'acceptance':[{'kind':'signal_contract','signals':signals['signals'],
                                   'probes':probes['probes'],'checks':value['checks']}],
                    'verification_tasks':value['verification_tasks']}
            def validate_checks(value):
                require(set(value) == {'handoff_id','checks','verification_tasks'},
                        'measurement_checks returns only checks, verification_tasks and handoff_id')
                validate_task(compose(value))
            checks = call_stage('measurement_checks',{**context,'approved_signals':signals['signals'],
                'approved_probes':probes['probes'],'available_verifiers':VERIFIERS},validate_checks)
            task = validate_task(compose(checks))
            from .training import TrainingRecorder
            if isinstance(recorder,TrainingRecorder):
                write_json(recorder.directory/'derived/task-assembly.json',{'schema_version':1,'producer_role':'planner',
                    'assembly':'task_from_five_model_subtasks','artifact_sha256':canonical_hash(task),
                    'sources':sources,'format_adaptations':[]})
            return task
        def validate_acceptance(value):
            require(set(value) == {'handoff_id','acceptance','verification_tasks'},
                    'acceptance stage returns only acceptance, verification_tasks and handoff_id')
            validate_task({**spec, 'execution_contract':execution['execution_contract'],
                           'acceptance':acceptance_array(value['acceptance']), 'verification_tasks':value['verification_tasks']})
        acceptance = call_stage('acceptance', {'task_spec':spec,'execution_contract':execution['execution_contract'],
                                             'available_verifiers':VERIFIERS}, validate_acceptance)
        task = validate_task({**spec,'execution_contract':execution['execution_contract'],
                              'acceptance':acceptance_array(acceptance['acceptance']),'verification_tasks':acceptance['verification_tasks']})
        from .training import TrainingRecorder
        if isinstance(recorder, TrainingRecorder):
            receipt = {'schema_version':1, 'producer_role':'planner','assembly':'task_from_three_model_subtasks',
                       'artifact_sha256':canonical_hash(task),'sources':sources,
                       'format_adaptations':['single_acceptance_rule_to_array'] if isinstance(acceptance['acceptance'],dict) else []}
            write_json(recorder.directory/'derived/task-assembly.json',receipt)
            recorder.event('planner_artifact_assembled',receipt)
        return task


class SceneAgent:
    def __init__(self, client, staged=False, auto_bool_mappings=False, process_constraints=False, tool_layout=False):
        self.client, self.staged, self.auto_bool_mappings = client, staged, auto_bool_mappings
        self.process_constraints = process_constraints
        self.tool_layout = tool_layout

    def propose_layout(self, task, catalog, previous=None, feedback=None, handoff=None):
        from .layout_design import call_layout_stage, validate_layout_proposal, layout_policy_hash
        from .layout_prompts import LAYOUT_PROPOSAL_PROMPT
        scoped={**catalog,'templates':[t for t in catalog['templates'] if t['prefab'] in {o['prefab'] for o in task['objects']}]}
        payload={'task':task,'catalog':catalog_for_planner(scoped),'previous_proposal':previous,
                 'review_feedback':feedback,'scene_stage':'layout_proposal','layout_policy_sha256':layout_policy_hash()}
        return call_layout_stage(self.client,'scene',LAYOUT_PROPOSAL_PROMPT,payload,
                                 lambda value:validate_layout_proposal(value,task),handoff)

    def plan(self, task: dict, catalog: dict, previous=None, feedback=None, handoff=None, layout_reference=None) -> dict:
        if layout_reference is not None:
            from .layout_design import validate_approved_layout
            validate_approved_layout(task,layout_reference)
        required_templates = {obj['id']: [t['template_id'] for t in catalog['templates'] if t['prefab'] == obj['prefab']]
                              for obj in task['objects']}
        relevant = {t for ids in required_templates.values() for t in ids}
        scoped = {**catalog, 'templates': [t for t in catalog['templates'] if t['template_id'] in relevant]}
        payload = {"task": task, "catalog": catalog_for_model(scoped), "previous_plan": previous,
                   "feedback": feedback, 'required_templates': required_templates,
                   'mapping_policy':'automatic_bool_contiguous' if self.auto_bool_mappings else 'model_explicit',
                    'process_constraint_policy':'plc_single_item' if self.process_constraints else 'model_settings'}
        if layout_reference is not None: payload['layout_reference']=layout_reference
        if self.staged:
            geometry_catalog = {**payload['catalog'], 'templates': [{k: t[k] for k in
                ('template_id', 'source_id', 'prefab', 'position', 'rotation')} for t in payload['catalog']['templates']]}
            from .geometry_tools import relative_layout_capability
            capability=relative_layout_capability(task,catalog)
            stage='layout_constraints' if self.tool_layout and capability['supported'] else 'layout'
            geometry_payload = {**payload, 'catalog': geometry_catalog, 'scene_stage':stage,
                                'layout_tool_capability':capability}
            if self.tool_layout and not capability['supported']:
                recorder=getattr(self.client,'recorder',None)
                if recorder is not None:recorder.event('layout_solver_fallback',capability)
            diagnostics = (feedback or {}).get('diagnostics', {})
            settings_repair = previous is not None and (feedback or {}).get('phase') == 'scene_contract' and (
                diagnostics.get('scope') == 'process_scene_invariants' or 'setting' in diagnostics.get('error', '').lower())
            if settings_repair:
                layout = {k:previous[k] for k in ('scene_id', 'base_source_id')}
                layout['objects'] = [{k:o[k] for k in ('id','template_id','position','yaw_deg')} for o in previous['objects']]
                recorder = getattr(self.client, 'recorder', None)
                if recorder is not None: recorder.event('scene_layout_reused_for_configuration_repair',
                                                       {'layout_sha256':canonical_hash(layout)})
            else:
                layout = self._stage(geometry_payload, handoff, task, required_templates, None, catalog)
            chosen = {o['template_id'] for o in layout['objects']}
            component_catalog = {**payload['catalog'], 'templates': [t for t in payload['catalog']['templates']
                                  if t['template_id'] in chosen]}
            value = self._stage({**payload, 'catalog': component_catalog, 'scene_stage': 'configuration_io',
                                 'approved_layout': layout}, handoff, task, required_templates, layout, catalog)
        else:
            value = complete_role(self.client, "scene", SCENE_PROMPT, payload, handoff)
        require(value.get("scene_id") == task["task_id"], "scene_id must match task_id")
        require(isinstance(value.get("objects"), list) and bool(value["objects"]), "empty scene plan")
        return value

    def _stage(self, payload, handoff, task, required_templates, layout, catalog=None):
        from .memory import planner_stage_cache
        recorder = getattr(self.client, 'recorder', None)
        cache_inputs = deepcopy(payload)
        # Receipt locations vary between attempts; the measured geometry itself remains in the key.
        native = cache_inputs.get('catalog', {}).get('native_geometry', {})
        for field in ('receipt_path','receipt_sha256'): native.pop(field, None)
        cached = planner_stage_cache(recorder, payload['scene_stage'], cache_inputs, lambda value: None, role='scene')
        for attempt in range(3):
            value = None
            try:
                value = cached[0] if attempt == 0 and cached else complete_role(self.client, 'scene', SCENE_PROMPT, payload, handoff)
                candidate_id = cached[1]['candidate_id'] if attempt == 0 and cached else getattr(recorder,'last_candidate_id',None)
                require(value.get('scene_id') == task['task_id'], 'scene_id must match task_id')
                require(value.get('base_source_id') in {s['id'] for s in payload['catalog']['sources']}, 'unknown base source')
                objects = value.get('objects')
                require(isinstance(objects, list) and all(isinstance(o, dict) for o in objects), 'objects must be an array of objects')
                require(len(objects) == len(required_templates) and {o.get('id') for o in objects} == set(required_templates),
                        'stage must cover each required object exactly once')
                for obj in objects:
                    require(obj.get('template_id') in required_templates[obj['id']], 'template must match required prefab')
                    if payload['scene_stage']=='layout_constraints':continue
                    pos = obj.get('position')
                    require(isinstance(pos, list) and len(pos) == 3 and all(type(v) is int for v in pos),
                            'layout positions must be explicit integer XYZ')
                    yaw = obj.get('yaw_deg')
                    require(type(yaw) in (int, float) and yaw % 90 == 0, 'layout yaw must be a multiple of 90')
                if payload['scene_stage']=='layout_constraints':
                    from .geometry_tools import resolve_relative_layout
                    resolved=resolve_relative_layout(value,catalog)
                    recorder=getattr(self.client,'recorder',None)
                    if recorder is not None:
                        receipt={'authority':'native_prefab_geometry','model_constraints_sha256':canonical_hash(value),
                                 'resolved_layout_sha256':canonical_hash(resolved),'native_geometry_sha256':canonical_hash(catalog['native_geometry']),
                                 'model_candidate_id':candidate_id,'resolved_layout':resolved}
                        from .contracts import write_json
                        if getattr(recorder,'directory',None):write_json(recorder.directory/'derived'/('layout-'+canonical_hash(resolved)+'.json'),receipt)
                        recorder.event('relative_layout_resolved',receipt)
                        if not (attempt == 0 and cached):
                            planner_stage_cache(recorder,payload['scene_stage'],cache_inputs,lambda value:None,value,role='scene')
                    return resolved
                if layout is None:
                    require(not value.get('mappings') and all(not (set(o) - {'id', 'template_id', 'position', 'yaw_deg'}) for o in objects),
                            'layout stage only selects templates and geometry; settings and mappings belong in configuration_io')
                    from .geometry_tools import validate_layout_geometry, resolve_transport_clearance
                    resolved, changes=resolve_transport_clearance(value,catalog or {})
                    validate_layout_geometry(resolved,catalog or {})
                    if changes:
                        if recorder is not None:
                            from .contracts import write_json
                            receipt={'authority':'conservative_native_voxel_clearance','model_layout_sha256':canonical_hash(value),
                                'resolved_layout_sha256':canonical_hash(resolved),'model_candidate_id':candidate_id,
                                'native_geometry_sha256':canonical_hash(catalog['native_geometry']),
                                'changes':changes,'flow_direction_verified':False,'physical_acceptance_passed':False}
                            write_json(recorder.directory/'derived'/('clearance-'+canonical_hash(resolved)+'.json'),receipt)
                            recorder.event('transport_clearance_resolved',receipt)
                            if not (attempt==0 and cached):
                                planner_stage_cache(recorder,payload['scene_stage'],cache_inputs,lambda value:None,value,role='scene')
                        return resolved
                else:
                    def geometry(plan):
                        return {o['id']: {k:o.get(k) for k in ('template_id', 'position', 'yaw_deg')} for o in plan['objects']}
                    require(value['base_source_id'] == layout['base_source_id'] and geometry(value) == geometry(layout),
                            'configuration_io changed approved layout; preserve its templates and geometry exactly')
                    if self.auto_bool_mappings:
                        require('mappings' not in value, 'automatic BOOL mapping is enabled; omit model-authored mappings')
                    validate_component_settings(objects, catalog)
                recorder = getattr(self.client, 'recorder', None)
                if recorder is not None:
                    recorder.event('scene_substage_accepted', {'stage': payload['scene_stage'], 'attempt': attempt,
                                                              'artifact_sha256': canonical_hash(value)})
                    if not (attempt == 0 and cached):
                        planner_stage_cache(recorder,payload['scene_stage'],cache_inputs,lambda value:None,value,role='scene')
                return value
            except (ModelServiceError, ModelOutputError, ExecutionLimitError):
                raise
            except (ContractError, SceneError) as error:
                if attempt == 2: raise
                recorder = getattr(self.client, 'recorder', None)
                if recorder is not None:
                    recorder.event('scene_substage_rejected', {'stage': payload['scene_stage'], 'attempt': attempt, 'error': str(error)})
                payload = {**payload, 'stage_rejection': {'error': str(error), 'previous_response': value,
                           'instruction': 'Return a complete response for this stage only; preserve approved layout in configuration_io.'}}


class VisionAgent:
    def __init__(self, client): self.client = client

    def review(self, task: dict, manifest: dict, io_contract: dict, evidence: dict,
               versions: dict, phase: str, plc: dict | None = None, handoff=None, approved_layout=None) -> dict:
        require(phase in VISION_PROMPTS, "review phase must be scene or runtime")
        prompt = VISION_PROMPTS[phase]
        require(evidence.get("versions") == versions, "review evidence is stale")
        require(versions.get("agent_policy") in (None, canonical_hash(policy_manifest())),
                "review uses another agent policy version")
        records = evidence.get("records", [])
        image_records = [r for r in records if r.get("kind") == "image"]
        require(bool(image_records), "Agent3 requires current image evidence")
        images = [Path(r["path"]) for r in image_records]
        for record, path in zip(image_records, images):
            require(path.is_file() and file_hash(path) == record["sha256"], "review image changed after capture")
        prompt_evidence = model_review_evidence(evidence, max_samples=24, io_contract=io_contract, compact_status=True)
        review_manifest = manifest_for_review(manifest)
        payload = {"task": task, "manifest": review_manifest, "io_contract": io_contract,
            "evidence": prompt_evidence, "versions": versions, "phase": phase, "plc": plc,
            "review_task": {"phase": phase, "prompt_id": "vision_layout" if phase == "scene" else "vision_runtime",
                "objective": "Check the current scene layout against the requested process." if phase == "scene" else
                             "Check observed execution against the requested process and acceptance checks."},
            'review_process_ids':['scene_setup']+[p['id'] for p in task['process']]}
        if phase=='scene':
            from .vision_layout import prepare_layout_input, layout_feedback, record_layout_feedback
            layout,layout_evidence=prepare_layout_input(task,manifest,evidence,approved_layout)
            payload.update(layout_review=layout,evidence=layout_evidence)
        for attempt in range(3):
            value = None
            try:
                value = complete_role(self.client, "vision", prompt, payload, handoff, images,
                                      metadata_from_request=phase=='scene')
                if phase=='scene':
                    feedback=layout_feedback(value,layout,layout_evidence,versions,handoff)
                    record_layout_feedback(self.client,feedback,layout,layout_evidence)
                    return feedback
                require(value.get("phase") == phase, "model returned wrong review phase")
                feedback = validate_feedback(value, versions, manifest, {r["id"] for r in prompt_evidence["records"]})
                validate_diagnoses(feedback, task, io_contract, prompt_evidence["records"])
                return feedback
            except (ModelServiceError, ModelOutputError, ExecutionLimitError):
                raise
            except ContractError as error:
                # Inconclusive causal evidence needs investigation, not a prompt that pressures a pass.
                if 'investigation' in str(error) or attempt == 2: raise
                recorder = getattr(self.client, 'recorder', None)
                if recorder is not None:
                    recorder.event('vision_contract_rejected', {'attempt': attempt, 'error': str(error)})
                payload = {**payload, 'format_feedback': {'error': str(error), 'previous_response': value,
                    'instruction': ('Return one JSON object with checks covering every supplied object. Each check needs '
                        'object_ids, related_object_ids, status, observed, expected, reason, evidence_images and suggestion. '
                        'Explain issue and uncertain; preserve uncertainty.' if phase=='scene' else
                        'Return one complete JSON object with handoff_id, versions, phase, verdict and findings. '
                                   'Never return [] alone. Re-evaluate the same evidence; do not invent a pass or diagnosis.')}}


def manifest_for_review(manifest):
    """Keep geometry/current signals while avoiding repeated XML and inactive operating modes.

    Complete artifacts and hashes stay in handoff inputs; this is a labeled review view, not a rewrite.
    """
    objects = []
    for obj in manifest['objects']:
        context = component_context(obj.get('configuration', {}))
        context['settings'] = [s for s in context['settings'] if s['path'].split('/')[-1].split('[')[0]
                               in ('Emitter', 'Sensor', 'Encoder', 'Parts', 'Bases')]
        objects.append({**{k:obj[k] for k in ('id', 'key', 'prefab', 'position', 'rotation', 'tags') if k in obj},
                        'component_context': context})
    return {**{k:manifest[k] for k in ('schema_version', 'scene_id', 'scene_path', 'scene_sha256') if k in manifest},
            'objects': objects, 'review_view': {'omitted': ['raw XML configuration', 'inactive operating modes',
                                                          'catalog origins'], 'full_source': 'handoff.inputs'}}


class PLCAgent:
    def __init__(self, client, tool_io=False): self.client, self.tool_io = client, tool_io

    def program(self, task: dict, io_contract: dict, feedback=None, previous=None,
                binding_config=None, handoff=None) -> dict:
        from .plc import validate_program, check_assignment_targets
        from .plc_scaffold import io_scaffold, assemble_program
        from .training import TrainingRecorder
        from .contracts import write_json
        scaffold = io_scaffold(binding_config, io_contract) if self.tool_io else None
        payload = {"task":task, "io_contract":io_contract, "feedback":feedback,
                   "previous_program":previous, "binding_config":binding_config or {}}
        if scaffold is not None: payload.update(plc_stage='logic', io_scaffold=scaffold)
        recorder = getattr(self.client, 'recorder', None)
        for attempt in range(3):
            value = None
            try:
                value = complete_role(self.client, 'plc', PLC_PROMPT, payload, handoff)
                program = assemble_program(value, scaffold, io_contract) if scaffold else value
                report=validate_program(program, io_contract)
                check_assignment_targets(program)
                if self.tool_io and report['unresolved'] and attempt<2:
                    if recorder is not None:recorder.event('plc_unresolved_review',{'attempt':attempt,'unresolved':report['unresolved']})
                    payload={**payload,'stage_rejection':{'previous_response':value,'error':report['unresolved'],
                        'instruction':'Recheck blockers against exact native evidence and scaffold. Verified normalized inputs are authoritative; '
                        'do not override them with typical device behavior. Optional unmapped system features are not requested requirements. '
                        'Keep actual unsupported requirements unresolved. Never remove pending physical verification to claim success.'}}
                    continue
                if scaffold is not None and isinstance(recorder, TrainingRecorder):
                    receipt = {'schema_version':1, 'producer_role':'plc', 'assembly':'model_logic_with_tool_io',
                               'artifact_sha256':canonical_hash(program), 'candidate_id':recorder.last_candidate_id,
                               'output_sha256':canonical_hash(value), 'scaffold_sha256':canonical_hash(scaffold)}
                    write_json(recorder.directory/'derived'/('plc-assembly-'+canonical_hash(program)+'.json'), receipt)
                    recorder.event('plc_artifact_assembled',receipt)
                return program
            except (ModelServiceError, ModelOutputError, ExecutionLimitError): raise
            except ContractError as error:
                if attempt == 2: raise
                if recorder is not None: recorder.event('plc_contract_rejected', {'attempt':attempt,'error':str(error)})
                payload = {**payload, 'stage_rejection':{'error':str(error),'previous_response':value,
                    'rejected_reply':rejected_reply(self.client) if value is None else None,
                    'instruction':'Repair this exact contract/linkage failure. Preserve control requirements. Return the complete requested fragment.'}}
