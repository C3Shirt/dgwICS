"""Read recorded agent runs; report deduplicated usage without invoking models or software."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def stamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def summarize(rows):
    usage_rows = [r for r in rows if isinstance(r.get('model_metadata', {}).get('usage'), dict)]
    times = sorted(r['latency_seconds'] for r in rows if isinstance(r.get('latency_seconds'), (int, float)))
    total = {'calls':len(rows), 'finished_calls':sum(bool(r.get('finished_at')) for r in rows),
        'usage_recorded_calls':len(usage_rows), 'usage_unavailable_calls':len(rows)-len(usage_rows),
        'call_errors':sum(r.get('status')=='call_error' for r in rows),
        'truncated_calls':sum(r.get('model_metadata',{}).get('finish_reason')=='length' for r in rows),
        'recorded_model_latency_seconds':round(sum(times),3),
        'latency_p95_seconds':round(times[max(0,math.ceil(len(times)*.95)-1)],3) if times else None}
    for field in ('prompt_tokens','completion_tokens','total_tokens'):
        total[field] = sum(r['model_metadata']['usage'].get(field,0) or 0 for r in usage_rows)
    total['reasoning_tokens'] = sum((r['model_metadata']['usage'].get('completion_tokens_details') or {}).get('reasoning_tokens',0) or 0 for r in usage_rows)
    total['cached_prompt_tokens'] = sum(r['model_metadata']['usage'].get('prompt_cache_hit_tokens',
        (r['model_metadata']['usage'].get('prompt_tokens_details') or {}).get('cached_tokens',0)) or 0 for r in usage_rows)
    total['recorded_image_tokens'] = sum((r['model_metadata']['usage'].get('prompt_tokens_details') or {}).get('image_tokens',0) or 0 for r in usage_rows)
    total['format_or_contract_feedback_calls'] = sum(any(k in r.get('payload',{}) for k in ('format_feedback','stage_rejection')) for r in rows)
    return total


def audit(root, pattern):
    directories = sorted(p for p in root.glob(pattern) if p.is_dir())
    records, variants, occurrences = {}, defaultdict(set), defaultdict(list)
    events, run_data, errors = {}, {}, []
    for directory in directories:
        state = read(directory/'run.json') if (directory/'run.json').is_file() else {}
        result = read(directory/'result.json') if (directory/'result.json').is_file() else {}
        traces = []
        for path in (directory/'trace.jsonl',directory/'training-data/events.jsonl'):
            if not path.is_file():
                continue
            for number,line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(),1):
                try:
                    event = json.loads(line)
                    if path.name=='trace.jsonl':
                        traces.append(event)
                    else:
                        digest=hashlib.sha256(json.dumps(event,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                        events[digest]=event
                except (ValueError,TypeError):
                    errors.append({'path':str(path),'line':number})
        local = []
        for path in sorted((directory/'training-data/candidates').glob('*.json')):
            record=read(path)
            if record.get('source_kind')!='live_model':
                continue
            key=record['candidate_id']; local.append(key); occurrences[key].append(str(directory))
            variants[key].add(hashlib.sha256(path.read_bytes()).hexdigest())
            if key not in records or (record.get('finished_at') or '')>(records[key].get('finished_at') or ''):
                records[key]=record
        run_data[str(directory)]={'state':state,'result':result,'traces':traces,'candidate_ids':local}
    rejected={e.get('candidate_id') for e in events.values() if 'rejected' in e.get('event','')}
    calls=[]
    for key,record in records.items():
        metadata=record.get('model_metadata') or {}; payload=record.get('payload') or {}
        fields=sorted(((k,size(v)) for k,v in payload.items()),key=lambda r:r[1],reverse=True)
        allowed=payload.get('allowed_actions')
        calls.append({'candidate_id':key,'first_seen_directory':occurrences[key][0],
            'record_copies':len(occurrences[key]),'record_variants':len(variants[key]),
            'role':record.get('role'),'phase':record.get('phase'),'run_id':record.get('run_id'),
            'started_at':record.get('started_at'),'finished_at':record.get('finished_at'),
            'latency_seconds':record.get('latency_seconds'),'status':record.get('status'),
            'model':metadata.get('returned_model') or metadata.get('requested_model'),
            'http_status':metadata.get('http_status'),'finish_reason':metadata.get('finish_reason'),
            'usage':metadata.get('usage'),'error_type':(record.get('error') or {}).get('type'),
            'contract_rejection_recorded':key in rejected,'payload_characters':size(payload),
            'largest_payload_fields_characters':dict(fields[:7]),
            'format_or_contract_feedback':any(k in payload for k in ('format_feedback','stage_rejection')),
            'allowed_action_count':len(allowed) if isinstance(allowed,list) else None})
    calls.sort(key=lambda r:r.get('started_at') or '')
    runs=[]
    for name,data in run_data.items():
        state,result,traces=data['state'],data['result'],data['traces']
        start=stamp(state.get('created_at')) or (stamp(traces[0]['timestamp']) if traces else None)
        own=[records[k] for k in data['candidate_ids'] if start is not None and (stamp(records[k].get('started_at')) or 0)>=start]
        endpoints=[stamp(r.get('finished_at')) for r in own if r.get('finished_at')]
        endpoints += [stamp(t['timestamp']) for t in traces]
        elapsed=round(max(endpoints)-start,3) if endpoints and start is not None else None
        phase_times=defaultdict(float)
        for left,right in zip(traces,traces[1:]):
            phase_times[left.get('phase','unknown')]+=max(0,stamp(right['timestamp'])-stamp(left['timestamp']))
        scene_checks=[]
        for p in Path(name).glob('scene-*/plc-*/acceptance.json'):
            value=read(p); checks=[c for rule in value.get('rules',[]) for c in rule.get('checks',[])]
            scene_checks.append({'path':str(p),'passed':value.get('passed'),'checks':len(checks),
                                 'checks_passed':sum(c.get('passed') is True for c in checks)})
        runs.append({'directory':name,'workflow_status':state.get('status'),
            'recorded_live_pipeline_passed':result.get('live_pipeline_passed'),
            'failure_category':(result.get('failure') or {}).get('category'),
            'recorded_elapsed_seconds':elapsed,'elapsed_scope':'creation through final recorded trace/model completion; excludes unrecorded observer work',
            'new_calls':summarize(own),'new_call_ids':[r['candidate_id'] for r in own],
            'trace_phase_elapsed_seconds':{k:round(v,3) for k,v in phase_times.items()},
            'reusable_artifacts':(state.get('resume') or {}).get('reusable',[]),
            'acceptance_revisions':scene_checks})
    by_role={role:summarize([r for r in records.values() if r['role']==role]) for role in sorted({r['role'] for r in records.values()})}
    return {'schema_version':1,'scope':{'runs_root':str(root),'directory_glob':pattern,
        'deduplication':'candidate_id across copied checkpoints; prefer completed record',
        'live_model_only':True,'model_calls_issued':False,'software_modified':False,
        'not_a_controlled_model_or_framework_benchmark':True,
        'token_counts_are_provider_usage_not_prices':True,
        'excluded':'runs outside the pattern, standalone investigation artifacts, unrecorded human/debugging time'},
        'totals':{'directories':len(directories),'candidate_record_files':sum(map(len,occurrences.values())),
            'duplicate_copies_excluded':sum(map(len,occurrences.values()))-len(records),**summarize(list(records.values()))},
        'by_role':by_role,'single_action_supervisor_calls':sum(c['role']=='supervisor' and c['allowed_action_count']==1 for c in calls),
        'contract_rejection_events':dict(Counter(e['event'] for e in events.values() if 'rejected' in e.get('event',''))),
        'runs':runs,'calls':calls,'unreadable_jsonl_records':errors}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs-root',type=Path,default=Path('runs'))
    parser.add_argument('--glob',default='*')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if args.out.exists():
        parser.error('output already exists; preserve the previous audit')
    result=audit(args.runs_root.resolve(),args.glob)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'output':str(args.out.resolve()),'totals':result['totals'],'by_role':result['by_role'],
                     'single_action_supervisor_calls':result['single_action_supervisor_calls']},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
