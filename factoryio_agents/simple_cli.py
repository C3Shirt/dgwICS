"""CLI for the issue #1 scene-first framework. It does not start or program a PLC."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import uuid

from .cli import read_configuration
from .contracts import file_hash,read_json,write_json
from .progress import ConsoleProgress
from .simple import SimpleWorkflow

ROOT=Path(__file__).resolve().parents[1]


def main(argv=None):
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description='最小场景闭环：一次场景设计 → 真实加载/截图 → 一次视觉检查 → 必要的局部修复。')
    parser.add_argument('prompt',nargs='?',help='场景的原始需求')
    parser.add_argument('--config',type=Path,default=ROOT/'config.agent.json')
    parser.add_argument('--out',type=Path)
    parser.add_argument('--resume',type=Path,help='复用这个简化流程任务的设计，重新加载和检查')
    parser.add_argument('--max-repairs',type=int,default=2)
    parser.add_argument('--inject-fault',choices=('emitter_gap','sensor_parallel'),help='仅用于受控布局故障实验，记录中明确标记')
    parser.add_argument('--quiet',action='store_true')
    args=parser.parse_args(argv)
    if not args.prompt and not args.resume:parser.error('provide a scene requirement or --resume')
    if args.prompt and args.resume:parser.error('resume uses the original requirement; do not supply a new prompt')
    output=(args.out or ROOT/'runs'/('simple-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])).resolve()
    if output.exists():parser.error('choose a fresh output directory')
    try:
        config=read_configuration(args.config)
        progress=ConsoleProgress(output,quiet=args.quiet)
        workflow=SimpleWorkflow(config,output,progress=progress,max_repairs=args.max_repairs,inject_fault=args.inject_fault)
        output.mkdir(parents=True)
        write_json(output/'config.snapshot.json',workflow.redactor.clean(config))
        sources=[ROOT/'factoryio_agents'/p for p in ('simple.py','simple_prompts.py','simple_cli.py','scene.py','geometry_tools.py','runtime.py')]
        sources.extend([ROOT/'tools/factoryio_bridge/workflow_hook.py',ROOT/'tools/factoryio_bridge/AgentBridge.cs'])
        write_json(output/'execution.json',{'framework':'simple','interface':'cli','framework_source_storage':'hashes_only',
            'source_hashes':{str(p.relative_to(ROOT)):file_hash(p) for p in sources}})
        result=workflow.resume(args.resume) if args.resume else workflow.run(args.prompt)
        write_json(output/'result.json',result)
        print(json.dumps({**result,'output':str(output)},ensure_ascii=False,indent=2))
        return 0 if result['status']=='scene_verified' else 2
    except Exception as error:
        print(json.dumps({'status':'needs_configuration','error':str(error),'output':str(output)},ensure_ascii=False))
        return 2


if __name__=='__main__':raise SystemExit(main())
