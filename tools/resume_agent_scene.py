"""Resume a recorded planner/scene run with model repairs and native tool evidence.

No scene geometry or PLC logic is authored here. All model candidates are retained.
"""
from pathlib import Path
import argparse
import json
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools/factoryio_bridge')]
from client import call
from factoryio_agents.contracts import ContractError, canonical_hash, read_json, require, version_tuple, write_json
from factoryio_agents.coordination import attach_component_semantics
from factoryio_agents.prompts import policy_manifest
from factoryio_agents.llm import ModelServiceError
from factoryio_agents.runtime import collect_evidence, run_hook, utc_now
from factoryio_agents.workflow import Workflow, load_config, run_lock


def resume(config_path: Path, output: Path):
    config = load_config(config_path)
    w = Workflow(config, output)
    old = read_json(output / 'run.json')
    w.state.update(old)
    w.state['policy'] = policy_manifest()
    w.state['training_collection']['last_resume_run_id'] = w.training.run_id if w.training else None
    task, catalog = read_json(output / 'task.json'), read_json(output / 'catalog.json')
    require(not (output / 'scene-stage-result.json').exists(), 'scene stage already recorded')
    w.state['versions'] = {'task': canonical_hash(task), 'scene': None, 'io': None, 'plc': None,
                           'config': canonical_hash(config), 'agent_policy': canonical_hash(policy_manifest())}
    records = [read_json(p) for p in (output / 'training-data').rglob('*.json')]
    records = [r for r in records if r.get('role') == 'scene' and r.get('structured_output')]
    previous = max(records, key=lambda r: r['started_at'])['structured_output'] if records else None
    feedback = {'phase': 'scene_contract', 'diagnostics': {'error': old.get('error')},
        'constraints': ['Return only documented scene-plan fields; no tag_overrides or extra report fields.',
            'Use exact component_context.settings XML paths. Tag[0] is not XML; indices are 1-based.',
            'Use unique contiguous input channels starting at 0 below 14 and output channels below 12.',
            "Map Running with {channel:'BitInput6',system_tag:'RunInputKey'} in a free low input slot.",
            'Disable forcing on conveyor and Emitter outputs using their real XML paths.',
            'Enable BoxM alone, Emitter.UpTo=1; every setting must exist in its template.']}
    write_json(output / 'resume-context.json', {'previous_policy': old['policy'], 'current_policy': policy_manifest(),
                                              'config': str(config_path.resolve()), 'feedback': feedback})
    with run_lock(output):
        for attempt in range(1, 5):
            w.state['scene_revision'] = old.get('scene_revision', 0) + attempt
            plan = w.author.plan(task, catalog, previous, feedback, w.handoff('scene', 'repair',
                [output / 'task.json', output / 'catalog.json', output / 'resume-context.json'], 'complete scene-plan.json'))
            write_json(output / f'candidate-plan-{w.state["scene_revision"]:03d}.json', plan)
            previous = plan
            try:
                directory, built = w.revision(task, plan, catalog)
                require(built['process_validation']['passed'], str(built['process_validation']))
                counts = built['io_contract']['channel_counts']
                require(counts.get('BitInputCount', 0) <= 14 and counts.get('BitOutputCount', 0) <= 12,
                        'driver exceeds inspected CODESYS capacity; move inherited Running into low channel')
                for obj in built['manifest']['objects']:
                    if obj['prefab'] in ('Emitter', 'RollerConveyor4M'):
                        require(all(t.get('use_forced_value') is False for t in obj['tags']), 'PLC actuator is forced')
                scene = directory / 'scene.factoryio'
                request = {'schema_version': 1, 'request_id': uuid.uuid4().hex, 'scene_path': str(scene.resolve()),
                           'scene_sha256': built['manifest']['scene_sha256'], 'versions': w.state['versions']}
                write_json(directory / 'load-request.json', request)
                run_hook(config['factoryio']['load_command'], {'request': str(directory / 'load-request.json'),
                    'confirmation': str(directory / 'load-receipt.json'), 'scene': str(scene), 'output': str(directory)}, 30)
                receipt = read_json(directory / 'load-receipt.json')
                require(receipt['software_load_confirmed'] is True, 'native scene loading not confirmed')
                built['manifest']['software_load_confirmed'] = True
                built['io_contract']['software_load_confirmed'] = True
                attach_component_semantics(built['manifest'], built['io_contract'], receipt['app_version'])
                versions = version_tuple(task, built['manifest']['scene_sha256'], built['io_contract'], None, config)
                w.state['versions'] = versions
                for name, value in [('manifest', built['manifest']), ('io-contract', built['io_contract']), ('versions', versions)]:
                    write_json(directory / (name + '.json'), value)
                evidence = collect_evidence(config['factoryio'], directory / 'visual', versions, built['io_contract'], seconds=0)
                require(evidence['reconciliation']['passed'], 'native tags differ from generated contract')
                evidence['records'].append({'id': 'scene-load-receipt', 'kind': 'scene_load_receipt', 'timestamp': utc_now(), 'receipt': receipt})
                command = config['factoryio']['load_command']
                ipc = Path(command[command.index('--ipc') + 1])
                status = call(ipc, 'status')
                for index in range(min(3, status['data']['saved_camera_count'])):
                    call(ipc, 'camera', index=index)
                    capture = call(ipc, 'capture', size=1)
                    evidence['records'].append({'id': f'view-{index}', 'kind': 'image', 'timestamp': capture['completed_at'],
                        'path': capture['data']['image_path'], 'sha256': capture['data']['image_sha256'],
                        'source': 'native_saved_camera', 'camera_index': index})
                write_json(directory / 'visual/evidence.json', evidence)
                w.transition('scene_loaded', scene_dir=str(directory), versions=versions)
                feedback = w.observer.review(task, built['manifest'], built['io_contract'], evidence, versions, 'scene', None,
                    w.handoff('vision', 'scene_review', [directory / 'manifest.json', directory / 'io-contract.json',
                              directory / 'visual/evidence.json'], 'scene-feedback.json'))
                write_json(directory / 'scene-feedback.json', feedback)
                if feedback['verdict'] == 'pass':
                    result = {'status': 'scene_verified', 'scene_dir': str(directory.resolve()), 'versions': versions,
                              'external_model_used': True, 'runtime_verified': False, 'task_unresolved': task['unresolved']}
                    write_json(output / 'scene-stage-result.json', result)
                    w.transition('scene_verified', **{k:v for k,v in result.items() if k != 'status'})
                    print(json.dumps(result, ensure_ascii=False)); return
                w.record_repair('scene', 'scene_review', feedback, plan)
                if feedback['verdict'] != 'repair' or {f['target'] for f in feedback['findings']} != {'scene'}:
                    w.transition('needs_attention', feedback=feedback); print('scene review needs investigation'); return
            except ModelServiceError as error:
                w.transition('needs_attention', target='tools', failure_kind='model_service', error=str(error))
                return
            except (ContractError, ValueError, RuntimeError, OSError) as error:
                feedback = {'phase': 'scene_contract', 'diagnostics': {'error': str(error)}}
                w.record_repair('scene', 'scene_contract', feedback, plan)
                w.transition('scene_repair_requested', error=str(error))
        w.transition('repair_limit_reached', phase='scene')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True); p.add_argument('--out', type=Path, required=True)
    args = p.parse_args(); resume(args.config.resolve(), args.out.resolve())
