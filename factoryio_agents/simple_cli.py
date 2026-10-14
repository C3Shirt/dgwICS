"""CLI for the issue #1 minimal scene and optional PLC loop."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import uuid

from .cli import read_configuration,protected_credentials
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
    parser.add_argument('--with-plc',action='store_true',default=None,help='继续 PLC 生成、编译部署与真实工艺检查')
    parser.add_argument('--keep-running',action='store_true',default=None,help='通过本次运行检查后保持两端运行，需要 --with-plc')
    parser.add_argument('--max-plc-repairs',type=int)
    parser.add_argument('--runtime-seconds',type=float)
    parser.add_argument('--reset-mode',choices=('warm','cold'))
    parser.add_argument('--start-from',choices=('scene','plc'),help='plc 需要 --resume 与 --with-plc，复用已通过布局')
    parser.add_argument('--start-local-services',action='store_true',default=None,help='服务启动权限不足时由框架请求 Windows UAC 确认')
    parser.add_argument('--plc-mode',choices=('compile','run'),help='compile 只生成和离线编译；run 编译部署并运行')
    parser.add_argument('--inject-fault',choices=('emitter_gap','sensor_parallel'),help='仅用于受控布局故障实验，记录中明确标记')
    parser.add_argument('--quiet',action='store_true')
    parser.add_argument('--check',action='store_true',help='只读检查本地接口和服务，不调用模型或修改软件')
    args=parser.parse_args(argv)
    if not args.prompt and not args.resume and not args.check:parser.error('provide a scene requirement or --resume')
    if args.prompt and args.resume:parser.error('resume uses the original requirement; do not supply a new prompt')
    output=(args.out or ROOT/'runs'/('simple-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])).resolve()
    if output.exists():parser.error('choose a fresh output directory')
    try:
        config=read_configuration(args.config)
        if args.check:
            from .cli import preflight
            from .codesys_services import service_control
            protected_credentials(config)
            result=preflight(config);result['services']=[service_control(kind,start=False) for kind in ('gateway','runtime')]
            result['models'].pop('planner',None);result['issues']=[s for s in result['issues'] if not s.startswith('planner model')]
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if not result['issues'] and all(s.get('service_running') for s in result['services']) else 2
        old=read_json(args.resume/'run.json') if args.resume else {}
        options=old.get('execution_options',{})
        defaults={'with_plc':old.get('execution_scope')=='scene_and_plc','keep_running':False,'max_plc_repairs':2,
                  'runtime_seconds':30,'reset_mode':'warm','start_local_services':False,'plc_mode':'run'}
        options={k:getattr(args,k) if getattr(args,k) is not None else options.get(k,v) for k,v in defaults.items()}
        if args.plc_mode is not None:options['with_plc']=True
        start_from=args.start_from or ('plc' if args.resume and options['with_plc'] and old.get('visual_verdict')=='pass' else 'scene')
        if options['with_plc'] and options['plc_mode']=='run':protected_credentials(config)
        progress=ConsoleProgress(output,quiet=args.quiet)
        workflow=SimpleWorkflow(config,output,progress=progress,max_repairs=args.max_repairs,inject_fault=args.inject_fault,
            start_from=start_from,**options)
        output.mkdir(parents=True)
        write_json(output/'config.snapshot.json',workflow.redactor.clean(config))
        sources=[ROOT/'factoryio_agents'/p for p in ('simple.py','simple_prompts.py','simple_cli.py','simple_plc.py','simple_runtime.py',
            'scene.py','geometry_tools.py','runtime.py','plc.py','plc_scaffold.py','binding_tools.py','synchronized_runtime.py','codesys_bridge.py')]
        sources.extend([ROOT/'tools/factoryio_bridge/workflow_hook.py',ROOT/'tools/factoryio_bridge/AgentBridge.cs'])
        write_json(output/'execution.json',{'framework':'simple','interface':'cli','framework_source_storage':'hashes_only',
            'execution_options':options,'started_from':start_from,
            'source_hashes':{str(p.relative_to(ROOT)):file_hash(p) for p in sources}})
        result=workflow.resume(args.resume) if args.resume else workflow.run(args.prompt)
        write_json(output/'result.json',result)
        print(json.dumps({**result,'output':str(output)},ensure_ascii=False,indent=2))
        return 0 if result['status'] in ('scene_verified','plc_compiled','passed') else 2
    except Exception as error:
        print(json.dumps({'status':'needs_configuration','error':str(error),'output':str(output)},ensure_ascii=False))
        return 2


if __name__=='__main__':raise SystemExit(main())
