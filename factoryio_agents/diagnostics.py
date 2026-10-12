"""Evidence-based run attribution and failure classification, without guessing root causes."""
from pathlib import Path

from .contracts import canonical_hash, file_hash, read_json


def summarize_run(output: Path, state: dict) -> dict:
    candidates = [read_json(p) for p in (output / 'training-data/candidates').glob('*.json')]
    live = [r for r in candidates if r.get('source_kind') == 'live_model' and
            r.get('output_origin') == 'provider_content' and r.get('status') == 'response_received' and
            r.get('model_metadata', {}).get('http_status') == 200]
    roles = {}
    for role in ('planner', 'scene', 'vision', 'plc'):
        rows = [r for r in candidates if r['role'] == role]
        verified = [r for r in live if r['role'] == role]
        responded = [r for r in rows if r.get('model_metadata', {}).get('http_status') == 200 and
                     r.get('output_origin') == 'provider_content']
        roles[role] = {'calls': len(rows), 'provider_replies': len(verified),
            'provider_responses': len(responded), 'invalid_output_responses': len(responded) - len(verified),
            'models': sorted({r.get('model_metadata', {}).get('returned_model') for r in responded
                              if r.get('model_metadata', {}).get('returned_model')})}
    audit = []
    producers = [(output / 'task.json', 'planner')]
    producers += [(output/'layout-proposal.json','scene'),(output/'layout-review.json','planner'),
                  (output/'approved-layout.json','planner')]
    producers += [(p, 'scene') for p in output.glob('scene-*/scene-plan.json')]
    producers += [(p, 'plc') for p in output.rglob('program.json')]
    producers += [(p, 'vision') for p in output.glob('scene-*/scene-feedback.json')]
    producers += [(p, 'vision') for p in output.glob('scene-*/plc-*/runtime-feedback.json')]
    producers += [(p, 'vision') for p in output.glob('scene-*/plc-*/restart-feedback.json')]
    producers += [(p, 'supervisor') for p in output.glob('scene-*/plc-*/runtime-investigation.json')]
    for path, role in producers:
        if not path.is_file(): continue
        value = read_json(path)
        matching = [r for r in candidates if r['role'] == role and r.get('structured_output') == value]
        assembly_mode = None
        if path==output/'approved-layout.json':
            from .layout_design import audit_layout_approval
            matching=audit_layout_approval(output,value,candidates)
            if matching: assembly_mode='planner_approved_layout'
        if path == output / 'task.json' and not matching:
            proof = output / 'training-data/derived/task-assembly.json'
            if proof.is_file():
                receipt = read_json(proof)
                by_id = {r['candidate_id']:r for r in candidates}
                parts = {s['stage']:by_id.get(s['candidate_id']) for s in receipt.get('sources', [])}
                if len(receipt.get('sources',[])) == 5 and set(parts) == {'spec','execution','measurement_signals','measurement_probes','measurement_checks'} and all(
                        r and r.get('role') == 'planner' and isinstance(r.get('structured_output'),dict) for r in parts.values()):
                    values = {k:r['structured_output'] for k,r in parts.items()}
                    composed = {**values['spec'],'execution_contract':values['execution']['execution_contract'],
                        'acceptance':[{'kind':'signal_contract','signals':values['measurement_signals']['signals'],
                            'probes':values['measurement_probes']['probes'],'checks':values['measurement_checks']['checks']}],
                        'verification_tasks':values['measurement_checks']['verification_tasks']}
                    if composed == value and receipt.get('artifact_sha256') == canonical_hash(value) and all(
                            canonical_hash(by_id[s['candidate_id']]['structured_output']) == s.get('output_sha256') for s in receipt['sources']):
                        matching = list(parts.values()); assembly_mode = 'five_model_fragments'
                if len(receipt.get('sources', [])) == 3 and set(parts) == {'spec','execution','acceptance'} and all(
                    r and r.get('role') == 'planner' and isinstance(r.get('structured_output'), dict) for r in parts.values()):
                    spec, execution, acceptance = (parts[k]['structured_output'] for k in ('spec','execution','acceptance'))
                    rules = acceptance.get('acceptance')
                    adaptations = ['single_acceptance_rule_to_array'] if isinstance(rules,dict) and rules.get('kind') in (
                        'weight_routing','event_sequence','signal_contract') else []
                    if adaptations: rules = [rules]
                    composed = {**spec, 'execution_contract':execution.get('execution_contract'),
                                'acceptance':rules, 'verification_tasks':acceptance.get('verification_tasks')}
                    if composed == value and receipt.get('format_adaptations', []) == adaptations and receipt.get('artifact_sha256') == canonical_hash(value) and all(
                        canonical_hash(by_id[s['candidate_id']]['structured_output']) == s.get('output_sha256')
                        for s in receipt['sources']):
                        matching = list(parts.values()); assembly_mode = 'three_model_fragments'
        if role=='vision' and value.get('phase')=='scene' and not matching:
            proof=output/'training-data/derived'/('layout-review-'+canonical_hash(value)+'.json')
            if proof.is_file():
                from .vision_layout import layout_feedback, visual_layout_view
                receipt=read_json(proof)
                source=next((r for r in candidates if r['candidate_id']==receipt.get('candidate_id') and r['role']=='vision'),None)
                if source and canonical_hash(source.get('structured_output'))==receipt.get('output_sha256'):
                    try:
                        payload=source['payload']
                        context=receipt.get('request_context')
                        if context:
                            if (visual_layout_view(context['layout'])!=payload['layout_review'] or
                                    context['versions']!=source['versions'] or
                                    (context.get('handoff') or {}).get('id')!=source.get('handoff_id') or
                                    [r for r in context['evidence']['records'] if r['kind']=='image']!=payload['evidence']['records']):
                                raise ValueError('layout audit context differs from the recorded request')
                            composed=layout_feedback(source['structured_output'],context['layout'],context['evidence'],
                                context['versions'],context.get('handoff'))
                        else:
                            composed=layout_feedback(source['structured_output'],payload['layout_review'],payload['evidence'],
                                payload['versions'],payload.get('handoff'))
                        if composed==value and receipt.get('artifact_sha256')==canonical_hash(value):
                            matching=[source]; assembly_mode='layout_checks_with_tool_metadata'
                    except (ValueError,KeyError,TypeError): pass
        if role == 'plc' and not matching:
            proof = output / 'training-data/derived' / ('plc-assembly-'+canonical_hash(value)+'.json')
            if proof.is_file():
                from .plc_scaffold import io_scaffold, assemble_program
                receipt = read_json(proof)
                source = next((r for r in candidates if r['candidate_id'] == receipt.get('candidate_id') and r['role'] == 'plc'), None)
                if source and canonical_hash(source.get('structured_output')) == receipt.get('output_sha256'):
                    try:
                        payload = source['payload']
                        scaffold = io_scaffold(payload['binding_config'],payload['io_contract'])
                        composed = assemble_program(source['structured_output'],scaffold,payload['io_contract'])
                        if composed == value and receipt.get('artifact_sha256') == canonical_hash(value) and receipt.get('scaffold_sha256') == canonical_hash(scaffold):
                            matching = [source]; assembly_mode = 'model_logic_with_tool_io'
                    except (ValueError, KeyError, TypeError): pass
        audit.append({'path': str(path.resolve()), 'sha256': file_hash(path), 'producer_role': role,
                      'matches_recorded_candidate': bool(matching),
                      'matches_live_provider_candidate': bool(matching) and all(r in live for r in matching),
                      'assembly_mode':assembly_mode,
                      'candidate_ids': [r['candidate_id'] for r in matching]})
    all_roles = all(r['provider_replies'] for r in roles.values())
    status = state.get('status', 'failed')
    error = state.get('error', '')
    diagnostics = state.get('diagnostics') or {}
    category, evidence_status = 'unknown', 'inconclusive'
    if status == 'cancelled':
        category, evidence_status = 'cancelled', 'observed'
    elif state.get('failure_kind') == 'execution_limit':
        category, evidence_status = 'execution_limit', 'observed'
    elif state.get('failure_kind') == 'model_output':
        category, evidence_status = 'model_output_budget', 'observed'
    elif state.get('failure_kind') == 'model_service' or ('model ' in error and ('HTTP ' in error or 'request failed' in error)):
        category, evidence_status = 'model_service', 'observed'
    elif state.get('failure_kind') == 'tool_configuration' or state.get('phase') == 'device_binding':
        category, evidence_status = 'tool_configuration', 'observed'
    elif 'unresolved requirements' in error:
        category, evidence_status = 'requirements_unresolved', 'observed'
    elif (diagnostics.get('error_count', 0) and diagnostics.get('bridge_result', {}).get('messages_verified') and
          diagnostics.get('bridge_result', {}).get('build_completed') and diagnostics.get('source_unchanged')):
        category, evidence_status = 'plc_compile', 'observed'
    elif state.get('phase') == 'deployment' and diagnostics.get('binding_report',{}).get('unresolved'):
        category,evidence_status='agent_output_contract','observed'
    elif state.get('phase') in ('compile','deployment') and diagnostics:
        category, evidence_status = 'codesys_tool_or_environment', 'observed'
    elif state.get('phase') in ('scene_load', 'connection', 'runtime_control'):
        category, evidence_status = 'software_or_adapter', 'observed'
    elif state.get('acceptance', {}).get('passed') is False:
        category, evidence_status = 'physical_acceptance_or_missing_evidence', 'observed'
    elif error and state.get('active_role') in ('planner', 'scene', 'plc', 'vision'):
        category, evidence_status = 'agent_output_contract', 'observed'
    elif status in ('passed', 'awaiting_scene_review'):
        category, evidence_status = None, 'observed'
    scene_integrity = []
    for path in output.glob('scene-*/manifest.json'):
        manifest = read_json(path)
        scene = path.parent / 'scene.factoryio'
        ok = scene.is_file() and file_hash(scene) == manifest['scene_sha256']
        scene_integrity.append({'scene': str(scene.resolve()), 'matches_generated_hash': ok})
    current_scene = Path(state.get('scene_dir') or output / '_missing_scene').resolve()
    current_plc = Path(state.get('plc_dir') or output / '_missing_plc').resolve()
    def current_record(directory, name):
        path = directory / name
        return read_json(path) if path.is_relative_to(output.resolve()) and path.is_file() else {}
    acceptance = current_record(current_plc, 'acceptance.json')
    scene_review = current_record(current_scene, 'scene-feedback.json')
    runtime_review = current_record(current_plc, 'runtime-feedback.json')
    verification_path = output / 'verification.json'
    verification = read_json(verification_path) if verification_path.is_file() else None
    assigned_complete = verification is None or (verification.get('all_completed') is True and
        all(row.get('status') == 'passed' and row.get('evidence') and all(
            Path(e['path']).is_file() and file_hash(Path(e['path'])) == e['sha256'] for e in row['evidence'])
            for row in verification.get('checks', [])))
    checks = {'all_roles_replied': all_roles,
        'assigned_phase_verification_completed': assigned_complete,
        'all_required_artifact_roles_present': {'planner', 'scene', 'plc', 'vision'} <= {a['producer_role'] for a in audit},
        'artifacts_match_live_provider_replies': bool(audit) and all(a['matches_live_provider_candidate'] for a in audit),
        'scene_files_match_generated_hashes': bool(scene_integrity) and all(r['matches_generated_hash'] for r in scene_integrity),
        'deterministic_acceptance_passed': acceptance.get('passed') is True and acceptance == state.get('acceptance'),
        'scene_visual_review_passed': scene_review.get('verdict') == 'pass',
        'runtime_visual_review_passed': runtime_review.get('verdict') == 'pass' and runtime_review.get('versions') == state.get('versions')}
    passed = status == 'passed' and all(checks.values())
    if status == 'passed' and not passed:
        category, evidence_status = 'attribution_or_acceptance_missing', 'observed'
    attempt_id = state.get('training_collection', {}).get('run_id')
    attempt_calls = {role:sum(r.get('run_id') == attempt_id for r in candidates if r['role'] == role)
                     for role in roles} if attempt_id else None
    return {'schema_version': 1, 'workflow_status': status, 'live_pipeline_passed': passed,
        'roles': roles, 'all_four_roles_have_provider_replies': all_roles, 'artifact_authorship': audit,
        'roles_this_attempt':attempt_calls, 'resume':state.get('resume'),
        'completion_checks': checks,
        'scene_integrity': scene_integrity,
        'manual_override_detected': any(not a['matches_recorded_candidate'] for a in audit) or
                                    any(not r['matches_generated_hash'] for r in scene_integrity),
        'failure': None if passed else {'category': category, 'evidence_status': evidence_status,
            'phase': state.get('phase'), 'active_role': state.get('failed_role', state.get('active_role')),
            'target': state.get('target'), 'observed_error': error or None,
            'diagnostics': diagnostics, 'root_cause_confirmed': category == 'plc_compile'},
        'model_call_errors': [{'candidate_id': r['candidate_id'], 'role': r['role'], 'error': r['error']}
                              for r in candidates if r.get('error')],
        'training': {'review_status': 'pending', 'automatic_gold_promotion': False},
        'evidence_directory': str(output.resolve())}
