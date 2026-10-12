"""Replay an existing task through Agent2 proposal and Agent1 review; no software control."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from factoryio_agents.cli import read_configuration
from factoryio_agents.contracts import canonical_hash, file_hash, read_json, require, write_json
from factoryio_agents.layout_design import audit_layout_approval
from factoryio_agents.progress import ConsoleProgress
from factoryio_agents.prompts import policy_manifest
from factoryio_agents.training import configured_secret_values
from factoryio_agents.workflow import Workflow


def main():
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True,help='Existing run containing task.json and catalog.json')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--config',type=Path,default=ROOT/'config.agent.json')
    args=parser.parse_args()
    source,output=args.source.resolve(),args.out.resolve()
    require(not output.exists() and not output.is_relative_to(source),'choose a fresh diagnostic output outside source run')
    paths=[source/'task.json',source/'catalog.json']
    before={str(p):file_hash(p) for p in paths}
    task,catalog=[read_json(p) for p in paths]
    config=read_configuration(args.config.resolve())
    require(task['protocol']==config['protocol'] and not task['unresolved'],'source task protocol/unresolved requirements differ')
    console=ConsoleProgress(output,secrets=configured_secret_values(config))
    workflow=Workflow(config,output,progress=console)
    for name,value in [('task',task),('catalog',catalog),('agent-policy',policy_manifest()),
                       ('requirement',{'requirement':task['requirement'],'protocol':task['protocol']})]:
        write_json(output/(name+'.json'),value)
    workflow.state['versions']={'task':canonical_hash(task),'scene':None,'io':None,'plc':None,
        'config':canonical_hash(config),'agent_policy':canonical_hash(policy_manifest())}
    write_json(output/'source-inputs.json',{'source_run':str(source),'files':before})
    report={'kind':'layout_handoff_probe','source_run':str(source),'layout_approved':False,
        'approval_provenance_verified':False,'software_modified':False,'fresh_capture':False,
        'live_pipeline_passed':False,'training_gold_created':False,
        'task_replanned':False,'scope':'Agent2 semantic proposal -> Agent1 review; existing task and archived capabilities'}
    try:
        approved=workflow.approve_layout(task,catalog)
        if approved is not None:
            candidates=[read_json(p) for p in (output/'training-data/candidates').glob('*.json')]
            report.update(layout_approved=True,
                approval_provenance_verified=bool(audit_layout_approval(output,approved,candidates)),
                approved_layout_path=str(output/'approved-layout.json'),object_count=len(approved['objects']))
        else: report.update(status=workflow.state['status'],feedback=workflow.state.get('feedback'))
    except KeyboardInterrupt:
        report.update(status='cancelled',error='keyboard_interrupt')
    except Exception as error:
        report.update(status='failed',error_type=type(error).__name__,error=workflow.redactor.clean(str(error)))
    report.update(source_unchanged=before=={str(p):file_hash(p) for p in paths},
        model_calls=len(workflow.budget.state['calls']),reported_total_tokens=workflow.budget.state['observed_total_tokens'])
    write_json(output/'layout-handoff-result.json',report)
    import json
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['layout_approved'] and report['approval_provenance_verified'] and report['source_unchanged'] else 2


if __name__=='__main__': raise SystemExit(main())
