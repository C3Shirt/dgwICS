"""Issue #4 scene-only loop over existing model, XML, load and capture adapters."""
from copy import deepcopy
from pathlib import Path

from .contracts import ContractError, canonical_hash, read_json, require, write_json
from .coordination import component_context
from .component_kb import knowledge_base
from .scene_spec import REQUIREMENT_SHAPE, DESIGN_SHAPE, PLACEMENTS, hard_static, validate_requirement_spec, validate_requirement_bindings, validate_scene_design, validate_plan
from .scene_verifier import default_registry, compile_check_plan, evaluate_plan, fuse_results, repair_ticket
from .scene_visual import VISUAL_SCHEMA, VIEWS, image_context, validate_visual
from .simple_prompts import REQUIREMENT_PROMPT, DESIGN_PROMPT, LAYOUT_PROMPT, PATCH_PROMPT
from .scene_context import PromptContextBuilder

def catalog_context(catalog):
    rows = []
    for prefab in sorted({t['prefab'] for t in catalog['templates']}):
        templates = [t for t in catalog['templates'] if t['prefab'] == prefab]
        metric = catalog.get('native_geometry', {}).get('prefabs', {}).get(prefab, {})
        card = knowledge_base().lookup(prefab)
        rows.append({'prefab': prefab, 'function': card['function'] if card else 'No official functional description indexed.',
            'measured_capabilities': sorted(metric.get('component_geometry_local', {})),
            'bounds_local_metres': metric.get('bounds_local_metres'),
            'templates': [{'template_id': t['template_id'], 'operating_mode': component_context(t['configuration'])['operating_mode'],
                'settings': [{'path': s['path'], 'attributes': list(s['attributes'])} for s in component_context(t['configuration'])['settings']]} for t in templates]})
    return rows


def checked_call(w, role, prompt, payload, validator, images=None):
    from .llm import ModelClient,ModelServiceError
    value=None;parse_error=None
    try:value = w.call(role, prompt, payload, images)
    except ModelServiceError:raise
    except ContractError as error:
        if role!='vision' or not str(error).startswith(('vision model response','vision model did not return')):raise
        parse_error=error
    try:
        if parse_error is not None:raise parse_error
        return validator(value)
    except (ContractError, ValueError, KeyError, TypeError) as error:
        if 'could not justify a correction' in str(error) or 'Unsupported physical requirements' in str(error):raise
        w.trace('model_output_rejected', active_role=role, error=str(error))
        retry={**payload,'rejected_output':value,'format_error':str(error)}
        saved=None;saved_modes=None
        if parse_error is not None and isinstance(w.raw_model,ModelClient):
            retry['rejected_output']=w.raw_model.exchange_metadata().get('raw_content')
            saved=w.raw_model.config.get('response_schemas',{}).pop('vision',None)
            saved_modes={k:deepcopy(w.raw_model.config[k]) for k in ('schema_roles','json_mode') if k in w.raw_model.config}
            w.raw_model.config['schema_roles']=[r for r in w.raw_model.config.get('schema_roles',[]) if r!='vision']
            w.raw_model.config['json_mode']=True
            retry['response_schema']=saved or VISUAL_SCHEMA
            retry['format_recovery']='Return a JSON object. Server schema enforcement returned malformed content; local schema validation remains mandatory.'
            w.trace('response_format_fallback',active_role=role,response_format='json_object',bounded_retry=1)
        try:
            value = w.call(role, prompt, retry, images)
            return validator(value)
        finally:
            if saved is not None:w.raw_model.config['response_schemas']['vision']=saved
            if saved_modes is not None:
                for key in ('schema_roles','json_mode'):
                    if key in saved_modes:w.raw_model.config[key]=saved_modes[key]
                    else:w.raw_model.config.pop(key,None)


def apply_ticket_patch(plan, design, patch, ticket, spec, catalog):
    from .simple import apply_local_patch
    require(isinstance(patch, dict) and not set(patch) - {'reason', 'changes', 'add_objects', 'remove_object_ids', 'unresolved','relation_updates','remove_relation_ids','relation_impacts'}, 'unsupported patch field')
    require(not patch.get('unresolved'), 'Repair agent could not justify a correction: ' + str(patch.get('unresolved')))
    operations = set(ticket['allowed_operations']); allowed = set(ticket['affected_object_ids'])
    require(not patch.get('add_objects') or 'add' in operations, 'ticket does not allow additions')
    require(not patch.get('remove_object_ids') or 'remove' in operations, 'ticket does not allow removals')
    for change in patch.get('changes', []):
        require(('replace' if 'replace_with' in change else 'change') in operations, 'ticket disallows this patch operation')
    relevant=[r for r in spec['requirements'] if r['requirement_id'] in ticket['requirement_ids']]
    justified_roles = {r[k] for r in relevant
                       for k in ('subject_role', 'target_role') if r[k]}
    for obj in patch.get('add_objects', []):
        justified=bool(set(obj.get('roles', [])) & justified_roles)
        for r in relevant:
            if r['predicate'] not in ('count','exists') or r['subject_role'] is not None:continue
            args=r['arguments'];prefab=obj.get('prefab')
            matches_prefab='prefab' not in args or args['prefab']==prefab
            capabilities=catalog.get('native_geometry',{}).get('prefabs',{}).get(prefab,{}).get('component_geometry_local',{})
            matches_capability='capability' not in args or args['capability'] in capabilities
            justified=justified or matches_prefab and matches_capability
        require(justified,'addition has no affected requirement role or matching count selector')
    before = {o['id']: deepcopy(o) for o in plan['objects']}; candidate_design = deepcopy(design)
    normalized=deepcopy(patch)
    for change in normalized.get('changes',[]):
        for setting in change.get('settings',[]):
            if type(setting.get('value')) is bool:setting['value']='True' if setting['value'] else 'False'
            elif type(setting.get('value')) in (int,float):setting['value']=str(setting['value'])
    changed = apply_local_patch(plan, {k:v for k,v in normalized.items() if k not in ('relation_updates','remove_relation_ids','relation_impacts')}, allowed, catalog, design=candidate_design)
    after = {o['id']: o for o in changed['objects']}
    for identity in ticket['frozen_object_ids']: require(after.get(identity) == before[identity], 'patch modified a frozen object')
    for obj in candidate_design['objects']:
        row = after[obj['id']]
        obj.update(placement={'kind': 'absolute', 'position': deepcopy(row['position']), 'yaw_deg': row['yaw_deg']},
                   settings=deepcopy(row.get('settings', [])), template_id=row['template_id'])
    candidate_design['relations'] = [r for r in candidate_design['relations'] if r['subject_id'] in after and r['target_id'] in after]
    relation_map={r['id']:r for r in candidate_design['relations']}
    new_ids=set(after)-set(before);relation_scope=allowed|new_ids
    for relation_id in patch.get('remove_relation_ids',[]):
        require(relation_id in relation_map and {relation_map[relation_id]['subject_id'],relation_map[relation_id]['target_id']} & relation_scope,'cannot remove an unrelated relation')
        del relation_map[relation_id]
    for row in patch.get('relation_updates',[]):
        require(isinstance(row,dict) and {row.get('subject_id'),row.get('target_id')} & relation_scope and
                set(row.get('requirement_ids',[]))<=set(ticket['requirement_ids']),'relation update lacks affected scope/requirements')
        if row.get('id') in relation_map:require({relation_map[row['id']]['subject_id'],relation_map[row['id']]['target_id']} & relation_scope,'cannot overwrite an unrelated relation')
        relation_map[row['id']]=deepcopy(row)
    candidate_design['relations']=list(relation_map.values())
    for impact in patch.get('relation_impacts',[]):
        require(isinstance(impact,dict) and set(impact)=={'relation_id','expected_effect'} and impact['relation_id'] in relation_map and
                isinstance(impact['expected_effect'],str),'invalid reported relation impact')
    validate_scene_design(candidate_design, spec, catalog); validate_plan(changed, catalog)
    from .scene import validate_component_settings
    validate_component_settings(changed['objects'],catalog)
    require(canonical_hash(changed) != canonical_hash(plan), 'patch made no layout change')
    return changed, candidate_design


def run_layout(w, requirement, catalog, resume_source=None):
    from .simple import design_to_plan, route_scene_design
    from .llm import ModelClient
    registry = default_registry(); geometry_enabled = w.scene_verification == 'relations'
    if isinstance(w.raw_model, ModelClient): w.raw_model.config['response_schemas']['vision'] = VISUAL_SCHEMA
    components = catalog_context(catalog)
    complete_resume=bool(resume_source and (Path(resume_source)/'design.json').is_file() and (Path(resume_source)/'scene-plan.json').is_file())
    if resume_source and not complete_resume:
        source=Path(resume_source).resolve();old=read_json(source/'run.json')
        require(old.get('framework')=='simple' and read_json(source/'requirement.json')['requirement']==requirement,'resume requirement/framework differs')
        require(old.get('template_sources')==w.state['template_sources'] and old.get('protocol_sha256')==w.state['protocol_sha256'],'resume source/protocol changed')
        require(old.get('execution_options',{}).get('scene_verification') in ('relations','relations_visual'),'legacy scene needs its legacy continuation path')
        if (source/'requirement-spec.json').is_file():
            recovered=read_json(source/'requirement-spec.json')
            require(canonical_hash(recovered)==old.get('requirements_sha256'),'saved requirements changed')
            if w.requirement_spec is not None:require(canonical_hash(w.requirement_spec)==canonical_hash(recovered),'resume cannot replace independent requirements')
            try:
                validate_requirement_bindings(recovered,catalog,registry)
            except ContractError as error:
                provenance=read_json(source/'requirements-source.json')
                require(w.requirement_spec is None and provenance.get('source')=='independent_model_extraction',
                        'Supplied requirements have invalid adapter bindings: '+str(error))
                write_json(w.output/'rejected-requirement-spec.json',recovered)
                write_json(w.output/'requirements-recovery.json',{'source_run':str(source),'rejected_sha256':canonical_hash(recovered),'reason':str(error),
                           'action':'Re-extract original text; no design or loaded scene is reused from this invalid contract.'})
                w.trace('saved_contract_rejected',artifact='independent_requirements',source_run=str(source),error=str(error))
            else:
                w.requirement_spec=recovered
                w.trace('artifact_reused',artifact='independent_requirements',source_run=str(source))
        w.state['source_run']=str(source)
        w.trace('resume_from_saved_phase',source_run=str(source),saved_phase=old.get('phase'))
    if complete_resume:
        source = Path(resume_source).resolve(); old = read_json(source / 'run.json')
        require(old.get('framework') == 'simple' and read_json(source / 'requirement.json')['requirement'] == requirement, 'resume requirement/framework differs')
        require(old.get('template_sources') == w.state['template_sources'] and old.get('protocol_sha256') == w.state['protocol_sha256'], 'resume source/protocol changed')
        require((source / 'requirement-spec.json').is_file(), 'This legacy run has no independent RequirementSpec; use --scene-verification legacy to resume it.')
        spec = validate_requirement_bindings(validate_requirement_spec(read_json(source / 'requirement-spec.json'), requirement, trusted=True),catalog,registry)
        require(canonical_hash(spec) == old.get('requirements_sha256'), 'saved independent requirements changed')
        if w.requirement_spec is not None: require(canonical_hash(w.requirement_spec) == canonical_hash(spec), 'resume cannot replace requirements')
        design = read_json(source / 'design.json'); plan = read_json(source / 'scene-plan.json')
        require(canonical_hash(design) == old['design_sha256'] and canonical_hash(plan) == old['plan_sha256'], 'resume artifact changed')
        validate_scene_design(design, spec, catalog); validate_plan(plan, catalog)
        w.state['source_run'] = str(source)
        if old.get('experiment_fault'): w.state['experiment_fault'] = old['experiment_fault']
        w.trace('artifact_reused', artifact='requirements_design_layout', source_run=str(source))
    else:
        if w.requirement_spec is not None:
            spec = validate_requirement_bindings(validate_requirement_spec(w.requirement_spec, requirement, trusted=True),catalog,registry)
            write_json(w.output / 'requirements-source.json', {'source': 'supplied_oracle', 'sha256': canonical_hash(w.requirement_spec)})
        else:
            request = {'phase': 'scene_requirements', 'original_requirement': requirement, 'output_contract': REQUIREMENT_SHAPE,
                'checker_capabilities': registry.describe(), 'available_components': [{'prefab': c['prefab'], 'measured_capabilities': c['measured_capabilities']} for c in components]}
            spec = checked_call(w, 'scene', REQUIREMENT_PROMPT, request,
                                lambda v: validate_requirement_bindings(validate_requirement_spec(v, requirement),catalog,registry))
            write_json(w.output / 'requirements-source.json', {'source': 'independent_model_extraction', 'coverage_basis': 'quoted source-text coverage; semantic fidelity still requires evaluation'})
        write_json(w.output / 'requirement-spec.json', spec)
        w.state['requirements_sha256'] = canonical_hash(spec)
        require(hard_static(spec), 'No mandatory static requirement is available; runtime requirements are deferred.')
        builder=PromptContextBuilder(catalog,spec,PLACEMENTS)
        request = builder.design(requirement,components)
        require(not request['candidate_coverage']['catalog_expansion_required'],'Candidate retrieval needs expansion: '+str(request['candidate_coverage']['missing_capabilities']))
        def build_design(value):
            value = route_scene_design(value)
            require(not value.get('unresolved'), 'Unsupported physical requirements: ' + str(value.get('unresolved')))
            validate_scene_design(value, spec, catalog)
            plan = design_to_plan(value, catalog, on_center=lambda r: write_json(w.output / 'centering.json', r))
            validate_plan(plan, catalog)
            return value, plan
        design, plan = checked_call(w, 'scene', DESIGN_PROMPT, request, build_design)
    write_json(w.output / 'requirement-spec.json', spec); w.state['requirements_sha256'] = canonical_hash(spec)
    builder=PromptContextBuilder(catalog,spec,PLACEMENTS)
    w.state['deferred_requirement_ids'] = [r['requirement_id'] for r in spec['requirements'] if r['phase'] == 'runtime']
    w.state['deferred_tasks'] = design.get('deferred_tasks', [])
    write_json(w.output / 'deferred-tasks.json', {'status': 'pending', 'requirements': [r for r in spec['requirements'] if r['phase'] == 'runtime'], 'tasks': w.state['deferred_tasks']})
    if w.inject_fault:
        from .scene_experiments import inject_fault
        write_json(w.output/'original-design.json',design);write_json(w.output/'original-scene-plan.json',plan)
        plan, fault = inject_fault(plan, design, catalog, w.inject_fault)
        if w.inject_fault=='missing_object':
            identity=fault['object_id'];design=deepcopy(design)
            design['objects']=[o for o in design['objects'] if o['id']!=identity]
            design['relations']=[r for r in design['relations'] if identity not in (r['subject_id'],r['target_id'])]
        w.state['experiment_fault'] = fault; w.trace('experiment_fault_injected', **fault)
    seen_plans = set(); previous_failures = None
    for attempt in range(w.max_repairs + 1):
        fingerprint = canonical_hash(plan)
        if fingerprint in seen_plans: return w.finish('needs_attention', target='scene', error='Repair cycle returned to an already evaluated layout.')
        seen_plans.add(fingerprint)
        w.state.update(scene_revision=attempt, design_sha256=canonical_hash(design), plan_sha256=fingerprint, phase='scene_checks')
        directory = w.output / ('scene-' + str(attempt).zfill(3)); directory.mkdir(parents=True, exist_ok=True)
        w.state['scene_dir']=str(directory)
        checks = compile_check_plan(spec, design, registry)
        for root in (w.output, directory):
            write_json(root / 'design.json', design); write_json(root / 'scene-plan.json', plan)
            write_json(root / 'requirement-spec.json', spec); write_json(root / 'check-plan.json', checks)
        write_json(w.output / 'run.json', w.state)
        w.trace('tool_requested', phase='scene_checks', tool='scene_verifier', action='prebuild_checks')
        projected = evaluate_plan(checks, design, plan, catalog, registry, geometry_enabled=geometry_enabled)
        write_json(directory / 'prebuild-checks.json', projected)
        w.trace('tool_completed', phase='scene_checks', tool='scene_verifier', action='prebuild_checks', check_count=len(projected))
        rendered = w.tools.render(plan, catalog, directory)
        receipt = read_json(directory / 'load-receipt.json'); receipt['manifest_objects'] = rendered['manifest']['objects']
        native = evaluate_plan(checks, design, plan, catalog, registry, receipt=receipt, geometry_enabled=geometry_enabled)
        write_json(directory / 'native-checks.json', native)
        visual = None
        for view_attempt in range(w.max_view_retries + 1):
            images = image_context(rendered, directory); write_json(directory / 'image-index.json', images)
            payload = builder.visual(requirement,design,checks,images,plan,receipt,spatial_context=geometry_enabled)
            write_json(directory/('vision-input-'+str(view_attempt)+'.json'),payload)
            write_json(directory/('vision-context-audit-'+str(view_attempt)+'.json'),
                {'context_manifest':builder.manifest('visual',{'scene_overview':'independent requirements and official functions',
                    'objects':'native identity/pose, declared roles, sourced knowledge','expected_relations':'compiled original constraints',
                    'images':'validated actual image attachments'},[o['id'] for o in design['objects']]),
                 'image_manifest':images,'load_request_id':receipt['request_id'],'requirements_sha256':canonical_hash(spec)})
            visual = checked_call(w, 'vision', LAYOUT_PROMPT, payload,
                lambda v: validate_visual(v, checks, spec, design, images), rendered['images'])
            visual['inspection_mode']=payload['inspection_mode']
            write_json(directory / ('visual-review-' + str(view_attempt) + '.json'), visual['raw_output'])
            write_json(directory / ('visual-review-normalized-' + str(view_attempt) + '.json'), visual)
            requested = {i['requested_view'] for i in visual['issues'] if i['status']=='unobservable' and i['requested_view']}
            if not requested or view_attempt == w.max_view_retries: break
            rendered = w.tools.capture_views(rendered, directory, sorted(requested), view_attempt + 1)
        verification = fuse_results(checks, native, visual, registration_passed=rendered['geometry'].get('passed') is True)
        write_json(directory / 'verification.json', verification)
        failures = [{'check_id': r['check_id'], 'status': r['status'], 'object_ids': r['object_ids']} for r in verification['checks'] if r['status'] != 'satisfied']
        write_json(directory / 'repair-progress.json', {'previous_failures': previous_failures, 'current_failures': failures,
            'unchanged_failure_set': previous_failures == failures, 'evaluated_layout_sha256': fingerprint})
        previous_failures = failures
        feedback = {'verdict': 'pass' if verification['status'] == 'satisfied' else 'repair' if verification['status'] == 'violated' else 'uncertain',
            'summary': visual['global_review']['summary'], 'inspection_complete':visual['inspection_complete'],
            'issues':visual['issues'], 'verification_file':str(directory/'verification.json')}
        write_json(directory / 'scene-feedback.json', feedback)
        if verification['status'] == 'satisfied':
            return w.finish('scene_verified', scene_dir=str(directory), visual_verdict='pass',
                native_registration_verified=True, native_geometry_verified=all(r['status']=='satisfied' for r in native),
                native_verification_scope=verification['scope'], static_requirements_covered=True,
                verification_file=str(directory / 'verification.json'), geometry_mode=w.scene_verification)
        blockers = [r for r in verification['checks'] if r['status'] in ('unsupported', 'unknown', 'unobservable')]
        ticket = repair_ticket(checks, verification, visual, design)
        if ticket is None or attempt == w.max_repairs:
            return w.finish('needs_attention', scene_dir=str(directory), target='tools' if any(r['status'] == 'unsupported' for r in blockers) else 'vision' if blockers else 'scene',
                feedback=feedback, verification_file=str(directory / 'verification.json'))
        write_json(directory / 'repair-ticket.json', ticket)
        request = builder.repair(design,plan,ticket,verification,visual,components,rendered['geometry'].get('measurements',[]),images)
        def patch_layout(value):
            write_json(directory / 'local-patch.json', value)
            return apply_ticket_patch(plan, design, value, ticket, spec, catalog)
        plan, design = checked_call(w, 'scene', PATCH_PROMPT, request, patch_layout)
        require(canonical_hash(spec) == w.state['requirements_sha256'], 'repair changed independent requirements')
