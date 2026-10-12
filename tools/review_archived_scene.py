"""Replay an archived scene review through Agent3; never operate Factory I/O or PLC."""
import argparse
from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from factoryio_agents.agents import VisionAgent
from factoryio_agents.cli import read_configuration
from factoryio_agents.contracts import canonical_hash, file_hash, read_json, require, write_json
from factoryio_agents.coordination import make_handoff
from factoryio_agents.execution import BudgetedClient, ExecutionBudget
from factoryio_agents.geometry_tools import loaded_geometry
from factoryio_agents.llm import ModelClient
from factoryio_agents.progress import ConsoleProgress, ModelObserver, ObservedClient
from factoryio_agents.prompts import policy_manifest
from factoryio_agents.training import RecordingClient, TrainingRecorder, configured_secret_values


def main():
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Original run directory')
    parser.add_argument('--scene-revision', required=True, help='Scene directory, e.g. scene-003')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=ROOT/'config.agent.json')
    parser.add_argument('--approved-layout',type=Path,help='Separate planner-approved layout reference')
    parser.add_argument('--json-object',action='store_true',help='Diagnostic: use JSON object mode instead of strict visual response schema')
    args = parser.parse_args()
    source, output = args.source.resolve(), args.out.resolve()
    scene = (source/args.scene_revision).resolve()
    require(scene.parent == source and scene.is_dir(), 'scene revision must be a directory directly inside source run')
    require(not output.exists() and not output.is_relative_to(source), 'choose a fresh output directory outside source run')
    paths = {'task.json':source/'task.json', **{name:scene/name for name in
        ('manifest.json', 'io-contract.json', 'versions.json', 'visual/evidence.json', 'load-receipt.json')}}
    if args.approved_layout: paths['approved-layout.json']=args.approved_layout.resolve()
    values = {name:read_json(path) for name,path in paths.items()}
    evidence = deepcopy(values['visual/evidence.json'])
    image_paths = [Path(r['path']) for r in evidence['records'] if r.get('kind') == 'image']
    before = {str(p):file_hash(p) for p in set(paths.values()) | set(image_paths)}
    config = read_configuration(args.config.resolve())
    if args.json_object:
        config['llm']['schema_roles']=[role for role in config['llm'].get('schema_roles',[]) if role!='vision']
    versions = deepcopy(values['versions.json'])
    # These are old captures, re-reviewed under the current policy/configuration.
    # Scene, task and I/O identity remain unchanged. Preserve the original version tuple separately.
    versions.update(agent_policy=canonical_hash(policy_manifest()), config=canonical_hash(config))
    evidence['versions'] = versions
    evidence['review_context'] = {'archived':True, 'source_run':str(source),
        'original_versions':values['versions.json'], 'fresh_capture':False,
        'instruction':'Review the depicted archived scene; this is not evidence of the current software state.'}
    geometry = loaded_geometry(values['load-receipt.json'], values['manifest.json'])
    for name in ('task.json', 'manifest.json', 'io-contract.json'):
        write_json(output/name, values[name])
    write_json(output/'versions.json', versions)
    write_json(output/'evidence.json', evidence)
    write_json(output/'geometry-recheck.json', geometry)
    write_json(output/'source-inputs.json', {'files':before, 'original_versions':values['versions.json']})
    if args.approved_layout: write_json(output/'approved-layout.json',values['approved-layout.json'])
    raw = ModelClient(config['llm'])
    secrets = configured_secret_values(config) + raw.secret_values()
    console = ConsoleProgress(output, secrets=secrets)
    observer = ModelObserver(output, console, secrets=secrets)
    recorder = TrainingRecorder(output/'training-data', source_kind='archived_scene_review_probe',
        secrets=secrets, event_callback=observer.training_event)
    budget = ExecutionBudget(output, config)
    client = BudgetedClient(RecordingClient(ObservedClient(raw, observer, recorder), recorder), budget)
    report = {'kind':'archived_scene_review_probe', 'source_run':str(source), 'scene_revision':scene.name,
        'response_format_mode':'json_object' if args.json_object else 'configured',
        'vision_review_completed':False, 'software_modified':False, 'fresh_capture':False,
        'live_pipeline_passed':False, 'training_gold_created':False,
        'geometry_recheck_passed':geometry['passed'],
        'geometry_recheck_scope':geometry['scope'], 'geometry_recheck_injected_into_model':False,
        'images':[{'path':str(p), 'sha256':before[str(p)]} for p in image_paths]}
    try:
        handoff = make_handoff('orchestrator', 'vision', 'scene_review', versions,
            [output/name for name in ('task.json', 'manifest.json', 'io-contract.json', 'versions.json', 'evidence.json')]+
            ([output/'approved-layout.json'] if args.approved_layout else []),
            'scene-feedback.json', [])
        feedback = VisionAgent(client).review(values['task.json'], values['manifest.json'],
            values['io-contract.json'], evidence, versions, 'scene', None, handoff,
            approved_layout=values.get('approved-layout.json'))
        write_json(output/'scene-feedback.json', feedback)
        report.update(vision_review_completed=True, verdict=feedback['verdict'],
            finding_count=len(feedback['findings']),
            targets=sorted({f['target'] for f in feedback['findings']}),
            pass_despite_unchecked_geometry=feedback['verdict']=='pass' and not geometry['spatial_checks_applied'])
    except Exception as error:
        report.update(error_type=type(error).__name__, error=observer.redactor.clean(str(error)))
    report.update(source_unchanged=all(p.is_file() and file_hash(p)==before[str(p)] for p in set(paths.values())|set(image_paths)),
        model_calls=len(budget.state['calls']), reported_total_tokens=budget.state['observed_total_tokens'])
    write_json(output/'scene-review-result.json', report)
    from json import dumps
    print(dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['vision_review_completed'] and report['source_unchanged'] else 2


if __name__ == '__main__': raise SystemExit(main())
