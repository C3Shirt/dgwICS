"""Verify an interrupted planner through the framework, without scene/PLC operations."""
import argparse
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from factoryio_agents.agents import PlannerAgent
from factoryio_agents.cli import read_configuration
from factoryio_agents.contracts import file_hash, read_json, require, write_json
from factoryio_agents.coordination import make_handoff
from factoryio_agents.execution import BudgetedClient, ExecutionBudget
from factoryio_agents.llm import ModelClient
from factoryio_agents.memory import inspect_resume, restore_resume
from factoryio_agents.progress import ConsoleProgress, ModelObserver, ObservedClient
from factoryio_agents.training import RecordingClient, TrainingRecorder, configured_secret_values


def main():
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=ROOT/'config.agent.json')
    args = parser.parse_args()
    source, output = args.source.resolve(), args.out.resolve()
    require(not output.exists(), 'choose a fresh diagnostic output directory')
    config = read_configuration(args.config.resolve())
    plan = inspect_resume(source, config)
    require(read_json(source/'run.json').get('active_role') == 'planner', 'source was not interrupted in planning')
    before = {str(p.relative_to(source)): file_hash(p) for p in source.rglob('*') if p.is_file()}
    restore_resume(plan, output)
    # A partial planner has no task.json yet; preserve its original catalog and requirement.
    for name in ('catalog.json', 'requirement.json'):
        shutil.copyfile(source/name, output/name)
    raw = ModelClient(config['llm'])
    secrets = configured_secret_values(config) + raw.secret_values()
    console = ConsoleProgress(output, secrets=secrets)
    observer = ModelObserver(output, console, secrets=secrets)
    recorder = TrainingRecorder(output/'training-data', source_kind='planning_recovery_probe',
        secrets=secrets, event_callback=observer.training_event)
    budget = ExecutionBudget(output, config)
    client = BudgetedClient(RecordingClient(ObservedClient(raw, observer, recorder), recorder), budget)
    report = {'kind':'planning_recovery_probe', 'source_run':str(source), 'planning_verified':False,
              'software_modified':False, 'live_pipeline_passed':False, 'training_gold_created':False}
    try:
        handoff = make_handoff('orchestrator', 'planner', 'planning', {},
            [source/'requirement.json', source/'catalog.json'], 'task.json', [])
        task = PlannerAgent(client, staged=True, staged_measurements=True).plan(
            plan['requirement'], read_json(output/'catalog.json'), config['protocol'], handoff)
        write_json(output/'task.json', task)
        report['planning_verified'] = True
        from json import loads
        events = [loads(line) for line in (recorder.directory/'events.jsonl').read_text(encoding='utf-8').splitlines()
                  if line.strip()]
        report['reused_subtasks'] = [e['details']['stage'] for e in events
            if e.get('run_id') == recorder.run_id and e['event'] == 'planner_substage_reused']
    except Exception as error:
        report.update(error_type=type(error).__name__, error=observer.redactor.clean(str(error)))
    after = {str(p.relative_to(source)): file_hash(p) for p in source.rglob('*') if p.is_file()}
    report.update(source_unchanged=before == after, model_calls=len(budget.state['calls']),
                  reported_total_tokens=budget.state['observed_total_tokens'])
    write_json(output/'planning-recovery-result.json', report)
    from json import dumps
    print(dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['planning_verified'] and report['source_unchanged'] else 2


if __name__ == '__main__': raise SystemExit(main())
