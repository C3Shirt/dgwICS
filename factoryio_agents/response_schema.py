"""Machine-readable constraints for visual review; no verdict or diagnosis is supplied by tools."""
from .contracts import require


def review_schema(payload):
    if payload.get('phase')=='scene' and payload.get('review_task',{}).get('prompt_id')=='vision_layout':
        from .vision_layout import layout_review_schema
        return layout_review_schema(payload)
    def object_(properties):
        return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
    def enum(values):
        require(bool(values), 'review schema enumeration is empty')
        return {'type':'string','enum':list(values)}
    text = {'type':'string'}
    texts = {'type':'array','items':text}
    task = payload['task']
    ids = [o['id'] for o in payload['manifest']['objects']]
    signals = [t['key'] for t in payload['io_contract']['tags']]
    refs = [r['id'] for r in payload['evidence']['records']]
    diagnosis = object_({'process_step':enum(['scene_setup']+[p['id'] for p in task['process']]),
        'signal_keys':{'type':'array','items':enum(signals)}, 'causal_status':enum(['hypothesis','supported']),
        'hypothesis':text,'alternatives':texts,'discriminating_check':text,'expected_result':text,
        'repair_scope':texts,'retest':texts})
    finding = object_({'category':enum(['geometry','configuration','logic','communication','requirements','unknown']),
        'target':enum(['scene','plc','tools','planner','investigate']),
        'object_ids':{'type':'array','items':enum(ids)}, 'observed':text,'expected':text,
        'evidence_ids':{'type':'array','items':enum(refs)},'confidence':{'type':'number'},
        'suggestion':text,'diagnosis':diagnosis})
    versions = object_({k:({'type':'null'} if v is None else enum([v])) for k,v in payload['versions'].items()})
    return object_({'handoff_id':enum([payload['handoff']['id']]), 'versions':versions,
                    'phase':enum([payload['phase']]),'verdict':enum(['pass','repair','inconclusive']),
                    'findings':{'type':'array','items':finding}})
