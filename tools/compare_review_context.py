"""Replay archived context projection without model calls or software operations."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from factoryio_agents.contracts import read_json, write_json
from factoryio_agents.coordination import model_review_evidence
from factoryio_agents.review_context import project_review_payload
from factoryio_agents.supervisor import progress_view
from factoryio_agents.training import json_text


def compare(source):
    state=read_json(source/'run.json'); attempt=state['training_collection']['run_id']
    candidates=[read_json(p) for p in (source/'training-data/candidates').glob('*.json')]
    rows=[]
    for r in sorted(candidates,key=lambda r:r.get('started_at','')):
        # Recovery copies are excluded, exactly as in the unique-call efficiency audit.
        if r.get('started_at','') < state['created_at'] or r['role'] not in ('vision','supervisor'):continue
        old=r['payload']
        if r['role']=='vision':
            refs=(old.get('handoff') or {}).get('inputs',[])
            ref=next((a for a in refs if Path(a['path']).name=='evidence.json'),None)
            if not ref:continue
            raw=read_json(Path(ref['path']))
            new=project_review_payload({**old,'evidence':model_review_evidence(raw,24,old['io_contract'],compact_status=True)},'vision')
            # The original process definitions, measured verdicts and values survive.
            assert new['task']['process']==old['task']['process']
            assert [t['key'] for t in new['io_contract']['tags']]==[t['key'] for t in old['io_contract']['tags']]
            assert new['io_contract']['bindings']==old['io_contract']['bindings']
            assert new['versions']==old['versions']
            if old['phase']=='runtime':assert new['task']['acceptance']==old['task']['acceptance']
            issued=True
        else:
            if len(old.get('allowed_actions',[]))!=1:continue
            ev={e['id']:e.get('value') for e in old.get('evidence',[])}
            new=progress_view(ev.get('run_state',{}),ev.get('result'),ev.get('resume_plan'))
            issued=False
        before,after=len(json_text(old)),len(json_text(new))
        rows.append({'candidate_id':r['candidate_id'],'role':r['role'],'phase':r.get('phase'),
            'old_payload_chars':before,'new_payload_chars':after,
            'payload_reduction_percent':round((1-after/before)*100,2),
            'new_model_call_required':issued,'images_unchanged':True,
            'old_reported_usage':r.get('model_metadata',{}).get('usage')})
    return {'schema_version':1,'source_run':str(source.resolve()),'scope':'archived text-context projection replay',
        'model_calls_issued':False,'software_modified':False,'measured_token_savings':None,
        'limits':'character changes are not token/latency/success improvements; images and prompt-policy effects require live measurement',
        'calls':rows}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();result=compare(args.source);write_json(args.out,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
