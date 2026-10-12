"""Persistent task checkpoints and scoped failure observations, without caching live success."""
from copy import deepcopy
import json
from pathlib import Path
import shutil

from .contracts import canonical_hash, file_hash, read_json, require, write_json
from .prompts import policy_manifest
from .training import Redactor, now


DESIGN_FLAGS = ('staged_task_design', 'staged_measurement_design', 'staged_scene_design',
                'auto_bool_mappings', 'apply_process_constraints', 'tool_scene_layout', 'tool_plc_io')


def plc_io_design_hash(contract):
    """Binding, polarity and knowledge changes invalidate logic; fresh receipt locations do not."""
    value = deepcopy(contract)
    for row in value.get('component_semantics', []):
        row.pop('evidence', None)
        row.pop('bridge_response_sha256', None)
    return canonical_hash(value)


def design_context(config):
    """Ignore model/credential/timeouts, but fingerprint actual design inputs and device layout."""
    codesys = {k: v for k, v in config.get('codesys', {}).items() if k not in
               ('online_credentials', 'protected_credentials_file', 'deploy', 'timeout_seconds',
                'command', 'online_probe', 'ensure_local_services')}
    files = {}
    for key in ('binding_seed_config', 'binding_seed_io', 'template_project'):
        if codesys.get(key):
            path = Path(codesys[key])
            files[key] = {'path': str(path.resolve()), 'sha256': file_hash(path)}
    # The seed may name a different template from the public configuration.
    if codesys.get('binding_seed_config'):
        seed = read_json(Path(codesys['binding_seed_config']))['codesys']
        template = Path(seed['template_project'])
        files['seed_template'] = {'path': str(template.resolve()), 'sha256': file_hash(template)}
    return {'protocol': config.get('protocol'), 'flags': {k: config.get(k, False) for k in DESIGN_FLAGS},
            'codesys': codesys, 'device_files': files,
            'scene_sources': [{'path': str(Path(p).resolve()), 'sha256': file_hash(Path(p))}
                              for p in config.get('scene_sources', [])]}


def checkpoint(output, stage, paths, config, versions):
    path = output / 'memory/checkpoints.json'
    value = read_json(path) if path.is_file() else {'schema_version': 1, 'stages': {}}
    value['stages'][stage] = {'recorded_at': now(), 'versions': deepcopy(versions),
        'policy': policy_manifest(), 'design_context': design_context(config),
        'files': [{'path': p.relative_to(output).as_posix(), 'sha256': file_hash(p)} for p in paths]}
    write_json(path, value)


def _owned_path(source, value):
    path = Path(value).resolve()
    require(path.is_relative_to(source), 'resume artifact is outside its source run')
    return path


def latest_runtime_review(plc):
    """Explicit execution order wins over timestamps: handoff follows full acceptance.

    A partial new handoff must never fall back to the old passing review.
    """
    plc = Path(plc)
    handoff = any((plc / 'live-handoff' / name).is_file() for name in ('receipt.json','evidence.json'))
    return {'stage':'live_handoff' if handoff else 'runtime',
        'evidence':plc / ('live-handoff/evidence.json' if handoff else 'runtime/evidence.json'),
        'acceptance':plc / ('live-handoff/acceptance.json' if handoff else 'acceptance.json'),
        'feedback':plc / ('restart-feedback.json' if handoff else 'runtime-feedback.json')}


def _supported_repair(feedback, target):
    return feedback.get('verdict') == 'repair' and any(
        f.get('target') == target for f in feedback.get('findings', []))


def inspect_resume(source, config, restart_from='auto', *, revalidate=False):
    """Read-only inspection; legacy runs require the same authorship audit as new checkpoints."""
    from .diagnostics import summarize_run
    source = Path(source).resolve()
    require((source / 'run.json').is_file(), 'resume source has no run.json')
    require(not (source / '.writer.lock').exists(), 'resume source is still owned by a writer')
    state = read_json(source / 'run.json')
    current_plc = _owned_path(source, state['plc_dir']) if state.get('plc_dir') else None
    latest = latest_runtime_review(current_plc) if current_plc else None
    runtime_feedback = read_json(latest['feedback']) if latest and latest['feedback'].is_file() else {}
    investigation = read_json(current_plc/'runtime-investigation.json') if current_plc and (
        current_plc/'runtime-investigation.json').is_file() else {}
    requirement = read_json(source / 'requirement.json')['requirement']
    report = summarize_run(source, state)
    require(revalidate or not report['live_pipeline_passed'],
            'source already passed; use --resume with --revalidate to repeat live validation')
    require(not report['manual_override_detected'], 'resume source has modified or unowned artifacts')
    execution = read_json(source / 'execution.json') if (source / 'execution.json').is_file() else {}
    if (source / 'result.json').is_file():
        require(not read_json(source / 'result.json').get('framework_changed_during_run'),
                'source framework changed during execution; inspect it before resuming')
    old_config = read_json(source / 'config.snapshot.json')
    old_policy = state.get('policy', {})
    old_context = design_context(old_config)
    index_path = source / 'memory/checkpoints.json'
    index = read_json(index_path).get('stages', {}) if index_path.is_file() else {}
    if index:
        # Checkpoints retain historical input hashes, rather than silently refreshing them.
        old_context = next(iter(index.values()))['design_context']
        for row in index.values():
            for ref in row['files']:
                path = _owned_path(source, source / ref['path'])
                require(path.is_file() and file_hash(path) == ref['sha256'], 'checkpoint artifact changed: ' + ref['path'])
    catalog_path = source / 'catalog.json'
    if catalog_path.is_file():
        for ref in read_json(catalog_path)['sources']:
            require(Path(ref['path']).is_file() and file_hash(Path(ref['path'])) == ref['sha256'],
                    'source scene template changed; start a new planning run')
    current_context = design_context(config)
    reasons, reusable, files = [], [], []
    compatible = old_context == current_context and old_policy.get('knowledge_sha256') == policy_manifest()['knowledge_sha256']
    if not compatible: reasons.append('design inputs, device configuration or component knowledge changed')
    audit = {Path(r['path']).resolve(): r for r in report['artifact_authorship']}
    def owned(path):
        return path in audit and audit[path]['matches_recorded_candidate']
    task_path = source / 'task.json'
    if compatible and task_path.is_file() and owned(task_path) and restart_from != 'planning':
        from .contracts import validate_task
        from .coordination import validate_execution_contract
        from .agents import validate_planning_capacity
        task = validate_task(read_json(task_path))
        require(canonical_hash(task) == state.get('versions', {}).get('task'), 'task checkpoint version mismatch')
        require(task['requirement'] == requirement and task['protocol'] == config['protocol'], 'task requirement/protocol changed')
        if 'execution_contract' in task: validate_execution_contract(task)
        validate_planning_capacity(task, read_json(catalog_path))
        if not task['unresolved'] and old_policy.get('prompt_sha256', {}).get('planner') == policy_manifest()['prompt_sha256']['planner']:
            reusable.append('task'); files += [task_path, catalog_path]
        else: reasons.append('planner policy changed or task requirements remain unresolved')
    layout_paths=[source/name for name in ('layout-proposal.json','layout-review.json','approved-layout.json')]
    if 'task' in reusable and all(p.is_file() and owned(p) for p in layout_paths):
        from .layout_design import validate_approved_layout
        hashes=policy_manifest()['prompt_sha256']
        if all(old_policy.get('prompt_sha256',{}).get(k)==hashes[k]
               for k in ('scene_layout_proposal','planner_layout_review')):
            validate_approved_layout(task,read_json(layout_paths[2]),read_json(layout_paths[0]),read_json(layout_paths[1]))
            reusable.append('layout'); files += layout_paths
        else: reasons.append('layout proposal or review policy changed')
    elif 'task' in reusable: reasons.append('planner-approved layout is missing or unaudited')
    scene = _owned_path(source, state['scene_dir']) if state.get('scene_dir') else None
    if 'layout' in reusable and scene and restart_from not in ('planning', 'scene'):
        needed = [scene / name for name in ('scene-plan.json', 'scene.factoryio', 'manifest.json',
                   'io-contract.json', 'versions.json', 'structural-validation.json', 'process-validation.json','layout-reference.json')]
        if all(p.is_file() for p in needed) and owned(scene / 'scene-plan.json'):
            from .workflow import verify_artifact_versions
            verify_artifact_versions(scene)
            approved=read_json(layout_paths[2])
            require(read_json(scene/'layout-reference.json')==approved and
                    read_json(scene/'manifest.json').get('layout_reference_sha256')==canonical_hash(approved),
                    'scene approval reference differs from the recorded design')
            geometry = read_json(scene / 'geometry-validation.json') if (scene / 'geometry-validation.json').is_file() else None
            feedback = read_json(scene / 'scene-feedback.json') if (scene / 'scene-feedback.json').is_file() else {}
            reject = feedback.get('verdict') == 'repair' and any(f.get('target') == 'scene' for f in feedback.get('findings', []))
            reject |= state.get('target') == 'scene' or _supported_repair(state.get('feedback', {}),'scene') or _supported_repair(runtime_feedback,'scene')
            if (read_json(scene / 'process-validation.json')['passed'] and (geometry is None or geometry['passed']) and not reject
                and old_policy.get('prompt_sha256', {}).get('scene') == policy_manifest()['prompt_sha256']['scene']):
                reusable.append('scene'); files += needed
            else: reasons.append('scene needs a model repair or scene policy changed')
    plc = _owned_path(source, state['plc_dir']) if state.get('plc_dir') else None
    if 'scene' in reusable and plc and restart_from not in ('planning', 'scene', 'plc'):
        program_path, result_path = plc / 'program.json', plc / 'codesys-result.json'
        if program_path.is_file() and result_path.is_file() and owned(program_path):
            result = read_json(result_path)
            feedback = runtime_feedback
            rejected = state.get('target') == 'plc' or any(_supported_repair(f,'plc') for f in
                (feedback, state.get('feedback', {}), investigation))
            old_versions = read_json(plc / 'versions.json')
            require(canonical_hash(read_json(program_path)) == old_versions['plc'], 'PLC checkpoint version mismatch')
            require(old_versions['task'] == state['versions']['task'] and old_versions['scene'] == state['versions']['scene']
                    and old_versions['io'] == canonical_hash(read_json(scene / 'io-contract.json')), 'PLC upstream versions changed')
            if all(result.get(k) is True for k in ('compiled', 'bindings_verified', 'task_program_verified')) and not rejected and (
                old_policy.get('prompt_sha256', {}).get('plc') == policy_manifest()['prompt_sha256']['plc']):
                # Historical compile proof permits static-code reuse if a fresh environment gate fails.
                # It never grants deployment, connection or runtime acceptance in the new attempt.
                reusable.append('plc'); files += [program_path, plc / 'versions.json', result_path]
            else: reasons.append('PLC feedback requires repair' if rejected else
                'PLC compile/binding/task verification incomplete' if not all(result.get(k) is True for k in
                    ('compiled','bindings_verified','task_program_verified')) else 'PLC prompt changed; task and scene remain reusable')
    repair_seed=None
    if 'layout' in reusable and 'scene' not in reusable and scene and (scene/'scene-plan.json').is_file() and owned(scene/'scene-plan.json'):
        refs=[scene/'scene-plan.json']
        feedback_path=scene/'scene-feedback.json'
        geometry_path=scene/'geometry-validation.json'
        if geometry_path.is_file() and not read_json(geometry_path).get('passed'):
            feedback={'phase':'scene_geometry','diagnostics':read_json(geometry_path)};refs.append(geometry_path)
            receipt_path=scene/'load-receipt.json'
            if receipt_path.is_file():
                feedback['native_geometry']=read_json(receipt_path).get('native_geometry');refs.append(receipt_path)
        elif feedback_path.is_file():
            feedback=read_json(feedback_path);refs.append(feedback_path)
        else:feedback={'phase':'scene_contract','diagnostics':{'error':state.get('error','revalidate the rejected layout')}}
        repair_seed={'role':'scene','accepted':False,'previous_plan_path':str(scene/'scene-plan.json'),
            'feedback':feedback,'inputs':[{'path':str(p),'sha256':file_hash(p)} for p in refs]}
    return {'schema_version': 1, 'source_run': str(source), 'requirement': requirement,
        'purpose': 'revalidation' if revalidate else 'recovery',
        'source_live_pipeline_passed_at_inspection': report['live_pipeline_passed'],
        'source_status': state['status'], 'source_failure': report['failure'], 'reusable': reusable,
        'restart_from': restart_from, 'invalidation_reasons': reasons,
        'latest_runtime_review': {k:str(v) if isinstance(v,Path) else v for k,v in latest.items()} if latest else None,
        'repair_seed':repair_seed,
        'files': [{'source_path': str(p), 'path': p.relative_to(source).as_posix(), 'sha256': file_hash(p)} for p in files],
        'scene_relative': scene.relative_to(source).as_posix() if 'scene' in reusable else None,
        'plc_relative': plc.relative_to(source).as_posix() if 'plc' in reusable else None,
        'live_steps_reexecuted': ['scene_load', 'geometry_check', 'scene_visual_review', 'compile_and_deploy',
                                 'connection', 'runtime_capture', 'runtime_acceptance', 'runtime_visual_review'],
        'source_execution_sha256': canonical_hash(execution), 'inspected_at': now()}


def restore_resume(plan, output):
    source = Path(plan['source_run'])
    require(not output.is_relative_to(source), 'resume output must be separate from the source run')
    (output / 'memory').mkdir(parents=True, exist_ok=True)
    for ref in plan['files']:
        src = Path(ref['source_path'])
        require(file_hash(src) == ref['sha256'], 'resume input changed after inspection')
        dst = output / ref['path']; dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    relocations = []
    if plan.get('scene_relative'):
        directory = output / plan['scene_relative']
        scene = (directory / 'scene.factoryio').resolve()
        for name in ('manifest.json','structural-validation.json'):
            path = directory / name
            value = read_json(path)
            old = value.get('scene_path')
            require(Path(old).resolve() == (source / plan['scene_relative'] / 'scene.factoryio').resolve(),
                    'source manifest/validation scene path differs from its recorded artifact')
            before = file_hash(path)
            value['scene_path'] = str(scene)
            write_json(path,value)
            relocations.append({'path':path.relative_to(output).as_posix(),'field':'scene_path','old':old,
                                'new':str(scene),'source_sha256':before,'relocated_sha256':file_hash(path)})
        write_json(output / 'memory/relocation.json',{'schema_version':1,'changes':relocations,
            'scene_bytes_changed':False,'scene_sha256':file_hash(scene),
            'authority':'tool-owned artifact location metadata; model plans and source run preserved'})
    for name in ('training-data', 'memory/planner-stages', 'memory/scene-stages'):
        if (source / name).is_dir(): shutil.copytree(source / name, output / name, dirs_exist_ok=True)
    for name in ('run.json', 'result.json'):
        if (source / name).is_file(): shutil.copyfile(source / name, output / 'memory' / ('source-' + name))
    write_json(output / 'resume.json', plan)


class ExperienceMemory:
    """Bounded observations for the same requirement; no automatic success/gold promotion."""
    def __init__(self, directory, redactor=None):
        self.directory = Path(directory)
        self.redactor = redactor or Redactor()

    def remember(self, output, requirement, report, scenario_id=None):
        failure = report.get('failure')
        if not failure: return
        target = self.directory / (canonical_hash(str(output.resolve())) + '.json')
        digest = file_hash(output / 'result.json')
        if target.is_file() and read_json(target).get('result_sha256') == digest: return
        state = read_json(output / 'run.json') if (output / 'run.json').is_file() else {}
        history = state.get('history', [])
        recorded_at = history[-1].get('timestamp') if history else state.get('created_at')
        row = self.redactor.clean({'schema_version': 1, 'kind': 'observed_failure', 'recorded_at': recorded_at or now(),
            'run': str(output.resolve()), 'requirement_sha256': canonical_hash(requirement), 'scenario_id': scenario_id,
            'versions':state.get('versions', {}),
            'failure': {k: failure.get(k) for k in ('category', 'phase', 'active_role', 'target', 'observed_error', 'root_cause_confirmed')},
            'result_sha256': digest, 'review_status': 'pending'})
        row['failure']['observed_error'] = (row['failure'].get('observed_error') or '')[:2000]
        self.directory.mkdir(parents=True, exist_ok=True)
        # One immutable file per attempt avoids concurrent JSONL append races.
        write_json(target, row)

    def retrieve(self, requirement, limit=6):
        rows = []
        for path in self.directory.glob('*.json'):
            try:
                row = read_json(path)
                result = Path(row['run']) / 'result.json'
                if row['requirement_sha256'] == canonical_hash(requirement) and result.is_file() and file_hash(result) == row['result_sha256']:
                    rows.append(row)
            except (ValueError, OSError, KeyError): continue
        return sorted(rows, key=lambda r: r['recorded_at'])[-limit:]

    def import_related_runs(self, runs, requirement):
        """Migrate same-task historical failures without changing the archived attempts."""
        for result in Path(runs).glob('*/result.json'):
            try:
                source = result.parent
                recorded = read_json(source / 'requirement.json')['requirement']
                if recorded != requirement: continue
                report = read_json(result)
                if report.get('failure'):
                    config = read_json(source / 'config.snapshot.json')
                    self.remember(source, recorded, report, config.get('training', {}).get('scenario_id'))
            except (ValueError, OSError, KeyError): continue


def planner_stage_cache(recorder, stage, inputs, validate, value=None, role='planner'):
    """Accepted planner fragments survive a later stage failure; original replies stay immutable."""
    if recorder is None or not isinstance(getattr(recorder, 'directory', None), Path): return None
    key = canonical_hash({'inputs': inputs, 'role_policy': policy_manifest()['prompt_sha256'][role],
                          'knowledge': policy_manifest()['knowledge_sha256']})
    path = recorder.directory.parent / ('memory/' + role + '-stages') / (stage + '-' + key + '.json')
    if value is not None:
        if stage == 'spec' and value.get('unresolved'): return None
        ref = {'stage': stage, 'input_sha256': key, 'candidate_id': recorder.last_candidate_id,
               'output_sha256': canonical_hash(value)}
        write_json(path, ref)
        return ref
    if not path.is_file(): return None
    ref = read_json(path)
    candidate = recorder.directory / 'candidates' / (ref['candidate_id'] + '.json')
    require(candidate.is_file(), 'cached planner candidate missing')
    record = read_json(candidate)
    value = record['structured_output']
    require(record.get('role') == role and record.get('status') == 'response_received' and
            canonical_hash(value) == ref['output_sha256'], 'cached planner candidate changed')
    validate(value)
    recorder.event(role + '_substage_reused', ref)
    return value, ref
