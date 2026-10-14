"""Current-version multi-view evidence and concise relation-level visual contracts."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .contracts import require, file_hash, read_json

VIEWS = ('top', 'iso_a', 'iso_b')


def image_context(rendered, directory):
    manifest = rendered['manifest']; scene_sha = manifest['scene_sha256']
    receipt = read_json(directory / 'load-receipt.json'); scene = Path(receipt['scene_path']).resolve()
    require(receipt.get('software_load_confirmed') is True and receipt['scene_sha256'] == scene_sha == file_hash(scene), 'current scene identity changed before review')
    require(rendered['evidence'].get('versions', {}).get('scene') == scene_sha, 'image evidence has stale scene version')
    rows = []; ids = set()
    for record in rendered['evidence']['records']:
        if record['kind'] != 'image': continue
        path = Path(record['path']).resolve(); meta = read_json(path.with_name(path.stem + '.bridge.json'))
        response = meta['bridge_response']; frame = response['data']
        require(record['id'] not in ids, 'duplicate image ID'); ids.add(record['id'])
        require(path in [p.resolve() for p in rendered['images']], 'image is not part of current rendered evidence')
        require(record['sha256'] == meta['image_sha256'] == frame['image_sha256'] == file_hash(path), 'captured image hash changed')
        require(Path(frame['scene_path']).resolve() == scene, 'screenshot belongs to a different loaded scene')
        view = record['camera_view']; require(view in VIEWS, 'unsupported view ID')
        camera = meta.get('camera_receipt')
        require(camera and camera.get('ok') is True and camera.get('action') == 'camera_fit', 'camera view was not confirmed')
        require(camera['data'].get('camera_fit', {}).get('view') == view, 'camera receipt differs from image view')
        timestamp = response['completed_at']
        require(datetime.fromisoformat(timestamp) >= datetime.fromisoformat(receipt['confirmed_at']), 'image predates this scene load')
        rows.append({'id': record['id'], 'kind': 'image', 'view_id': view, 'sha256': record['sha256'],
            'scene_sha256': scene_sha, 'timestamp': timestamp, 'load_request_id': receipt['request_id'],
            'capture_request_id': response['request_id']})
    require({r['view_id'] for r in rows} == set(VIEWS), 'current scene needs top, iso_a and iso_b evidence')
    require([Path(r['path']).resolve() for r in rendered['evidence']['records'] if r['kind']=='image']==
            [p.resolve() for p in rendered['images']], 'image/request order differs from image manifest')
    return rows


def validate_legacy_visual(value, checks, spec, design, images):
    require(isinstance(value, dict) and set(value) == {'global_review', 'check_results'}, 'invalid relation visual response fields')
    objects = {o['id'] for o in design['objects']}; image_ids = {i['id'] for i in images}
    requirements = {r['requirement_id'] for r in spec['requirements']}; relations = {r['id'] for r in design['relations']}
    wanted = {c['check_id']: c for c in checks['checks'] if 'visual' in c['evidence_methods']}
    result = deepcopy(value); global_ = result['global_review']
    require(isinstance(global_, dict) and set(global_) == {'status', 'summary', 'image_ids', 'findings'}, 'invalid global review')
    require(global_['status'] in ('satisfied', 'violated', 'unobservable') and isinstance(global_['summary'], str) and global_['summary'].strip(), 'invalid global status/summary')
    require(isinstance(global_['findings'], list), 'global findings must be an array')
    def refs(row):
        require(isinstance(row['object_ids'], list) and set(row['object_ids']) <= objects, 'visual references unknown object')
        require(isinstance(row['image_ids'], list) and row['image_ids'] and set(row['image_ids']) <= image_ids, 'visual result needs current image IDs')
        for field in ('observed', 'reason', 'repair_intent'):
            require(isinstance(row[field], str), 'visual explanation must be text')
        require(row['observed'].strip() and row['reason'].strip(), 'visual observation/reason is empty')
    require(isinstance(global_['image_ids'], list) and global_['image_ids'] and set(global_['image_ids']) <= image_ids, 'global review needs image evidence')
    for finding in global_['findings']:
        require(set(finding) == {'status', 'object_ids', 'requirement_ids', 'observed', 'reason', 'repair_intent', 'image_ids'}, 'invalid global finding')
        require(finding['status'] in ('violated', 'unobservable'), 'invalid global finding status'); refs(finding)
        require(isinstance(finding['requirement_ids'], list) and set(finding['requirement_ids']) <= requirements, 'unknown requirement in global finding')
        if finding['status'] == 'violated' and not finding['requirement_ids']:
            finding['status'] = 'unobservable'; global_['status'] = 'unobservable'
            finding['reason'] += ' No original requirement supports a new hard constraint.'
    require(global_['status'] != 'satisfied' or not global_['findings'], 'global pass has unresolved findings')
    require(global_['status'] != 'violated' or any(f['status'] == 'violated' for f in global_['findings']), 'global violation needs findings')
    require(isinstance(result['check_results'], list), 'visual check_results must be an array')
    seen = set()
    for row in result['check_results']:
        require(set(row) == {'check_id', 'status', 'object_ids', 'relation_ids', 'image_ids', 'observed', 'reason', 'repair_intent', 'requested_view'}, 'invalid visual check fields')
        key = row['check_id']; require(key in wanted and key not in seen, 'unknown/duplicate visual check'); seen.add(key)
        require(row['status'] in ('satisfied', 'violated', 'unobservable'), 'invalid visual status'); refs(row)
        require(set(row['object_ids']) <= set(wanted[key]['object_ids']), 'visual check targets unrelated objects')
        require(isinstance(row['relation_ids'], list) and set(row['relation_ids']) <= relations and
                set(row['relation_ids']) <= set(wanted[key]['relation_ids']), 'visual check references unrelated relation')
        require(row['requested_view'] is None or row['requested_view'] in VIEWS, 'unsupported requested view')
        if row['status'] == 'satisfied': require(set(row['object_ids']) == set(wanted[key]['object_ids']), 'visual pass must identify all checked objects')
    require(seen == set(wanted), 'visual response omitted dynamic checks')
    return result


def _array(): return {'type': 'array', 'items': {'type': 'string'}}
def _text(): return {'type': 'string'}
def _object(properties): return {'type': 'object', 'additionalProperties': False, 'required': list(properties), 'properties': properties}

_common = {'object_ids': _array(), 'image_ids': _array(), 'observed': _text(), 'reason': _text(), 'repair_intent': _text()}
LEGACY_VISUAL_SCHEMA = _object({
    'global_review': _object({'status': {'type': 'string', 'enum': ['satisfied', 'violated', 'unobservable']},
        'summary': _text(), 'image_ids': _array(), 'findings': {'type': 'array', 'items': _object({
            **_common, 'status': {'type': 'string', 'enum': ['violated', 'unobservable']}, 'requirement_ids': _array()})}}),
    'check_results': {'type': 'array', 'items': _object({**_common, 'check_id': _text(), 'relation_ids': _array(),
        'status': {'type': 'string', 'enum': ['satisfied', 'violated', 'unobservable']},
        'requested_view': {'type': ['string', 'null'], 'enum': [None, *VIEWS]}})}})

ISSUE_SHAPE = {'check_id':'supplied check ID or null for an unbound concern',
    'status':'violated|unobservable','suspect_object_ids':['affected supplied IDs'],
    'observation':'what the screenshots show','spatial_observation':'auxiliary measurement interpretation, or empty string',
    'reason':'contradiction or missing evidence','repair_intent':'relational correction, or empty string',
    'evidence_image_ids':['attached image IDs'],'requested_view':None}
ISSUES_CONTRACT = {'inspection_complete':'boolean; true only after all tasks and views have been inspected',
    'summary':'Required nonempty assessment. If no problem was found, explicitly say Layout correct.',
    'issues':[ISSUE_SHAPE], 'no_anomaly_response':{'inspection_complete':True,'summary':'Layout correct.','issues':[]}}
VISUAL_SCHEMA = _object({'inspection_complete':{'type':'boolean'},'summary':{'type':'string','minLength':1},'issues':{'type':'array','items':_object({
    'check_id':{'type':['string','null']},'status':{'type':'string','enum':['violated','unobservable']},
    'suspect_object_ids':_array(),'observation':_text(),'spatial_observation':_text(),'reason':_text(),
    'repair_intent':_text(),'evidence_image_ids':_array(),
    'requested_view':{'type':['string','null'],'enum':[None,*VIEWS]}})}})


def validate_visual(value, checks, spec, design, images):
    """Validate sparse model findings, then deterministically restore internal maps.

    Omitted rows are not independent observations: they mean no anomaly was
    reported in a declared complete inspection. Raw output stays issues-only.
    """
    require(isinstance(value,dict) and set(value)=={'inspection_complete','summary','issues'},'Return one JSON object with inspection_complete, a nonempty summary and issues; [] is not a valid report')
    require(type(value['inspection_complete']) is bool and isinstance(value['issues'],list),'invalid sparse inspection fields')
    require(isinstance(value['summary'],str) and value['summary'].strip(),'inspection summary must explicitly state the assessment, not be empty')
    objects={o['id'] for o in design['objects']};image_ids={i['id'] for i in images}
    wanted={c['check_id']:c for c in checks['checks'] if c['requirement_ids']}
    findings=[];by_check={};raw=deepcopy(value)
    for issue in raw['issues']:
        require(isinstance(issue,dict) and set(issue)==set(ISSUE_SHAPE),'invalid issue fields; use supplied output_contract')
        key=issue['check_id'];require(key is None or key in wanted,'issue references unknown check ID')
        require(issue['status'] in ('violated','unobservable'),'only anomalous checks may be returned')
        ids=issue['suspect_object_ids'];require(isinstance(ids,list) and len(set(ids))==len(ids) and set(ids)<=objects,'issue references unknown/duplicate object')
        require(isinstance(issue['evidence_image_ids'],list) and issue['evidence_image_ids'] and set(issue['evidence_image_ids'])<=image_ids,'issue needs actual attached image IDs')
        require(all(isinstance(issue[k],str) for k in ('observation','spatial_observation','reason','repair_intent')) and issue['observation'].strip() and issue['reason'].strip(),'issue needs observation and reason')
        require(issue['requested_view'] is None or issue['requested_view'] in VIEWS,'unsupported requested view')
        if key is not None:require(set(ids)<=set(wanted[key]['object_ids']),'issue targets unrelated objects')
        if issue['status']=='violated' and key is not None and wanted[key]['operator'] not in ('count','exists'):
            require(ids,'a placement violation needs the suspected object ID')
        status=issue['status'] if key is not None else 'unobservable'
        req_ids=wanted[key]['requirement_ids'] if key else []
        f={'check_id':key,'status':status,'object_ids':ids,'requirement_ids':req_ids,'observed':issue['observation'],
           'reason':issue['reason'],'repair_intent':issue['repair_intent'],'image_ids':issue['evidence_image_ids']}
        if key is None:f['reason']+=' No compiled original requirement supports this extra concern; it requires review, not an automatic patch.'
        findings.append(f)
        if key is not None:by_check.setdefault(key,[]).append(issue)
    rows=[]
    for key,c in wanted.items():
        issues=by_check.get(key)
        if issues:
            def joined(field):return ' '.join(i[field] for i in issues if i[field])
            requested=list(dict.fromkeys(i['requested_view'] for i in issues if i['requested_view']))
            row={'check_id':key,'status':'violated' if any(i['status']=='violated' for i in issues) else 'unobservable',
                'object_ids':list(dict.fromkeys(o for i in issues for o in i['suspect_object_ids'])),
                'relation_ids':c['relation_ids'],'image_ids':list(dict.fromkeys(o for i in issues for o in i['evidence_image_ids'])),
                'observed':joined('observation'),'reason':joined('reason'),'repair_intent':joined('repair_intent'),
                'requested_view':requested[0] if requested else None,'requested_views':requested,
                'spatial_observation':joined('spatial_observation'),'evidence_basis':'reported_visual_issue'}
        else:
            row={'check_id':key,'status':'satisfied' if value['inspection_complete'] else 'unobservable',
                'object_ids':c['object_ids'],'relation_ids':c['relation_ids'],'image_ids':list(image_ids),
                'observed':'No anomaly reported.' if value['inspection_complete'] else 'Inspection was not completed.',
                'reason':'Implicit absence of reported anomaly, not a per-check model observation.' if value['inspection_complete'] else 'Model did not complete all supplied tasks.',
                'repair_intent':'','requested_view':None,'spatial_observation':'',
                'evidence_basis':'complete_inspection_no_reported_issue' if value['inspection_complete'] else 'incomplete_inspection'}
        rows.append(row)
    status='violated' if any(f['status']=='violated' for f in findings) else 'unobservable' if findings or not value['inspection_complete'] else 'satisfied'
    return {'inspection_complete':value['inspection_complete'],'summary':raw['summary'],'issues':raw['issues'],'raw_output':raw,'contract_version':2,
        'global_review':{'status':status,'summary':raw['summary'],
                         'image_ids':list(image_ids),'findings':findings},'check_results':rows}
