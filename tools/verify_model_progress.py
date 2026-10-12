"""Small real-provider CLI probe of framework logging; no scene/PLC operations or gold samples."""
from copy import deepcopy
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from factoryio_agents.contracts import require, write_json
from factoryio_agents.llm import ModelClient
from factoryio_agents.progress import ConsoleProgress, ModelObserver, ObservedClient
from factoryio_agents.training import RecordingClient, TrainingRecorder, configured_secret_values
from factoryio_agents.workflow import load_config


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'config.agent.json')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--image',type=Path)
    args=parser.parse_args()
    require(not args.out.exists(),'probe output exists; choose a fresh directory')
    config=load_config(args.config.resolve());llm=deepcopy(config['llm'])
    llm['stream']=True;llm['schema_roles']=[]
    llm.setdefault('request_options',{}).update({
        'planner':{'max_tokens':512,'thinking':{'type':'disabled'}},
        'vision':{'max_tokens':512,'enable_thinking':False}})
    client=ModelClient(llm);secrets=configured_secret_values(config)+client.secret_values()
    console=ConsoleProgress(args.out,secrets=secrets)
    observer=ModelObserver(args.out,console,secrets=secrets)
    recorder=TrainingRecorder(args.out/'training-data',source_kind='connectivity_probe',
        secrets=secrets,event_callback=observer.training_event)
    wrapped=RecordingClient(ObservedClient(client,observer,recorder),recorder)
    results=[]
    prompt='这是日志与流式接收通讯探针。仅返回 JSON，字段 notes 为一条中文说明组成的数组，unresolved 为数组。说明已收到请求；有图片时可简述可见内容。不要生成程序、XML或场景，不声称仿真验收通过。'
    for role in ('planner','vision'):
        try:
            images=[args.image.resolve()] if role=='vision' and args.image else None
            result=wrapped.complete(role,prompt,{'phase':'connectivity_probe','requirement':'验证本角色真实服务的公开输出及流式传输日志'},images)
            require(isinstance(result.get('notes'),list) and bool(result['notes']),'probe notes missing')
            exchange=client.exchange_metadata()
            results.append({'role':role,'received':True,'response_mode':exchange.get('response_mode'),
                'streaming_requested':exchange.get('streaming_requested'),'stream_stats':exchange.get('stream_stats'),
                'usage':exchange.get('usage'),'candidate_id':recorder.last_candidate_id})
        except Exception as error:
            results.append({'role':role,'received':False,'error_type':type(error).__name__,
                'error':observer.redactor.clean(str(error))})
    report={'kind':'connectivity_probe','results':results,'software_modified':False,
        'live_pipeline_passed':False,'training_gold_created':False}
    write_json(args.out/'probe-result.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if all(r['received'] for r in results) else 2


if __name__=='__main__':raise SystemExit(main())
