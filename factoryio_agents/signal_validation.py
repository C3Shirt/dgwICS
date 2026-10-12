"""Scene-independent interpreter for model-authored measurement contracts.

No scene names, conveyor IDs, process steps, timer constants or PLC logic live here.
The same primitives evaluate transport, routing, interlocks and other observable
processes. Unknown semantics, missing data and vacuous checks fail closed.
"""
from datetime import datetime
from pathlib import Path
import re

from .contracts import ContractError, finite, nonempty, require, file_hash, read_json

KINDS = ('sequence', 'duration', 'alternation', 'invariant', 'probe_state')
BUTTON_PREFABS = ('StartPushButton', 'StopPushButton', 'ResetPushButton')
PROBE_HOLD_MIN_SECONDS = .4
PROBE_HOLD_MAX_SECONDS = 3


def native_probe_constraints():
    """Expose the interpreter's limits before a model authors native test actions."""
    return {'hold_seconds': {'minimum': PROBE_HOLD_MIN_SECONDS, 'maximum': PROBE_HOLD_MAX_SECONDS,
                            'inclusive': True, 'applies_to': ['press', 'release'],
                            'reason': 'Every pressed or released state needs a measurement window; '
                                      'a release is not an instantaneous action.'},
            'supported_button_prefabs': list(BUTTON_PREFABS)}


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', value), 'invalid measurement identifier')
    return value


def conditions(value, signals):
    require(isinstance(value, dict) and bool(value), 'conditions must be a nonempty object mapping actual alias KEYS to predicates')
    require(set(value) <= set(signals), 'conditions contain unknown alias KEYS '+str(sorted(set(value)-set(signals)))+
            '; valid aliases are '+str(list(signals))+'. Example: {"actual_alias":true}, not {"alias":"actual_alias","condition":true}')
    for condition in value.values():
        if type(condition) is bool: continue
        require(isinstance(condition, dict) and set(condition) == {'op','value'} and
                condition['op'] in ('eq','lt','lte','gt','gte'), 'unsupported signal condition')
        finite(condition['value'], 'condition value')


def edge(value, signals):
    require(isinstance(value, dict) and set(value) == {'signal','edge'} and value['signal'] in signals and
            value['edge'] in ('rising','falling'), 'invalid measurement edge')


def validate_signal_contract(rule, task, *, signals_only=False, probes_only=False):
    refs = rule.get('signals')
    require(isinstance(refs, dict) and refs, 'signal_contract needs signal references')
    objects = {o['id']: o for o in task['objects']}
    for alias, ref in refs.items():
        identifier(alias)
        require(isinstance(ref, dict) and set(ref) == {'object_id','kind','index','meaning'}, 'invalid signal reference fields')
        require(ref['object_id'] in objects, 'signal '+alias+' references unknown object; use '+str(list(objects)))
        require(ref['kind'] in ('input','output'), 'signal '+alias+' kind must be input or output')
        require(type(ref['index']) is int and ref['index'] >= 0, 'signal '+alias+' needs nonnegative integer index')
        require(ref['meaning'] in ('raw','occupied','pressed'), 'signal '+alias+' invalid meaning '+str(ref['meaning'])+
                '; allowed ENUM is raw, occupied, pressed. Every OUTPUT (motor, lamp, enable) MUST use raw.')
        require(ref['meaning'] == 'raw' or ref['kind'] == 'input', 'output cannot use input normalization')
    if signals_only: return rule
    probes = rule.get('probes', [])
    require(isinstance(probes, list) and len(probes) <= 8, 'invalid probe list')
    phases, probe_ids = set(), set()
    for probe in probes:
        pid = identifier(probe.get('id')); require(pid not in probe_ids, 'duplicate probe ID'); probe_ids.add(pid)
        require(probe.get('stage') in ('startup','after_capture'), 'invalid probe stage')
        if probe.get('wait_for') is not None:
            conditions(probe['wait_for'], refs)
            require(0 < finite(probe.get('timeout_seconds'), 'probe timeout') <= 30, 'invalid probe timeout')
        actions = probe.get('actions')
        require(isinstance(actions, list) and 1 <= len(actions) <= 12, 'invalid native action list')
        held = set()
        for action in actions:
            label = identifier(action.get('label')); phase = pid + '.' + label
            require(phase not in phases, 'duplicate probe phase'); phases.add(phase)
            oid = action.get('object_id')
            require(oid in objects and objects[oid]['prefab'] in BUTTON_PREFABS and type(action.get('pressed')) is bool,
                    'probe action requires a supported native momentary button')
            field = 'probe ' + phase + '.hold_seconds'
            hold = finite(action.get('hold_seconds'), field)
            require(PROBE_HOLD_MIN_SECONDS <= hold <= PROBE_HOLD_MAX_SECONDS,
                    f'probe hold outside supported bounds: {field}={hold:g}; allowed inclusive range '
                    f'[{PROBE_HOLD_MIN_SECONDS:g}, {PROBE_HOLD_MAX_SECONDS:g}] seconds. '
                    'This applies to EVERY action, both pressed=true and pressed=false (release). '
                    'Correct all invalid hold_seconds values; preserve the buttons and test coverage.')
            if action['pressed']: held.add(oid)
            else: held.discard(oid)
        require(not held, 'probe must release all pressed buttons')
    if probes_only: return rule
    checks = rule.get('checks')
    require(isinstance(checks, list) and checks, 'signal_contract needs checks')
    ids = set(); steps = {s['id'] for s in task['process']}
    for check in checks:
        cid = identifier(check.get('id')); require(cid not in ids, 'duplicate measurement check ID'); ids.add(cid)
        require(check.get('kind') in KINDS, 'unsupported measurement primitive')
        covered = check.get('process_ids')
        require(isinstance(covered, list) and covered and set(covered) <= steps, 'measurement references unknown process steps')
        if check['kind'] == 'sequence':
            require(isinstance(check.get('steps'), list) and 2 <= len(check['steps']) <= 20, 'invalid measured sequence')
            for item in check['steps']: edge(item, refs)
        elif check['kind'] in ('duration','alternation'):
            edge(check.get('start'), refs); edge(check.get('end'), refs)
            if check['kind'] == 'duration':
                low = finite(check.get('min_seconds'), 'duration min'); high = finite(check.get('max_seconds'), 'duration max')
                require(0 <= low < high <= 86400, 'invalid duration bounds')
                if 'while' in check: conditions(check['while'], refs)
        elif check['kind'] in ('invariant','probe_state'):
            conditions(check.get('expect'), refs)
            if check['kind'] == 'invariant': conditions(check.get('when'), refs)
            else:
                require(check.get('phase') in phases, 'check references unknown probe phase')
                require(0 <= finite(check.get('settle_seconds'), 'probe settle') <= .5, 'invalid probe settling limit')
            require(type(check.get('min_samples')) is int and check['min_samples'] >= 1, 'invalid check sample minimum')
        if check['kind'] in ('sequence','duration','alternation'):
            require(type(check.get('min_matches')) is int and check['min_matches'] >= 1, 'invalid check match minimum')
    for probe in probes:
        for action in probe['actions']:
            phase = probe['id'] + '.' + action['label']
            aliases = {a for a,r in refs.items() if r['object_id'] == action['object_id'] and
                       r['kind'] == 'input' and r['meaning'] == 'pressed'}
            applicable = [c for c in checks if c['kind'] == 'probe_state' and c['phase'] == phase and
                          any(c['expect'].get(a) is action['pressed'] for a in aliases) and
                          any(refs[a]['kind'] == 'output' for a in c['expect'])]
            require(bool(applicable), 'native probe phase needs pressed-state and PLC-output measurement: ' + phase)
    return rule


def resolve_signals(rule, io_contract):
    require(isinstance(io_contract, dict), 'measurement needs the current I/O contract')
    resolved = {}
    for alias, ref in rule['signals'].items():
        rows = [t for t in io_contract['tags'] if t.get('object_id') == ref['object_id'] and
                t['kind'] == ref['kind'] and t.get('index') == ref['index']]
        require(len(rows) == 1, 'cannot resolve unique measurement signal: ' + alias)
        tag = rows[0]; result = {'key': tag['key'], 'data_type': tag['data_type'], 'meaning': ref['meaning']}
        if ref['meaning'] != 'raw':
            rows = [s for s in io_contract.get('component_semantics', []) if s['tag_key'] == tag['key'] and
                    s.get('app_version_confirmed') is True]
            field = ref['meaning'] + '_value'
            require(len(rows) == 1 and type(rows[0]['semantics'].get(field)) is bool,
                    'measurement input semantics unconfirmed: ' + alias)
            require(tag['data_type'] == 'bool', 'normalization requires BOOL')
            result['active_value'] = rows[0]['semantics'][field]
        resolved[alias] = result
    return resolved


def state_from_tags(tags, resolved):
    by_key = {t['id']: t for t in tags}; values = {}
    for alias, ref in resolved.items():
        require(ref['key'] in by_key, 'trace missing signal ' + alias)
        tag = by_key[ref['key']]
        require(all(tag.get(f) is False for f in ('isForced','openCircuit','shortCircuit')),
                'measurement signal forced, faulty or status unknown: ' + alias)
        value = tag.get('value')
        if ref['data_type'] == 'bool': require(type(value) is bool, 'measurement BOOL type mismatch')
        else: finite(value, 'numeric measurement')
        values[alias] = value == ref['active_value'] if 'active_value' in ref else value
    return values


def matches(state, expected):
    for alias, value in expected.items():
        actual = state[alias]
        if type(value) is bool:
            if actual is not value: return False
        else:
            require(type(actual) in (int,float), 'numeric comparison received nonnumeric signal')
            target = value['value']
            if not {'eq':actual == target,'lt':actual < target,'lte':actual <= target,
                    'gt':actual > target,'gte':actual >= target}[value['op']]: return False
    return True


def samples_from_evidence(evidence, resolved):
    samples = []; ids = set()
    for row in evidence.get('records', []):
        if row.get('kind') != 'tag_sample': continue
        rid = nonempty(row.get('id'), 'sample ID'); require(rid not in ids, 'duplicate measurement sample ID'); ids.add(rid)
        timestamp = datetime.fromisoformat(row.get('timestamp','').replace('Z','+00:00'))
        require(timestamp.tzinfo is not None, 'measurement timestamp needs timezone')
        elapsed = finite(row.get('elapsed_seconds'), 'measurement elapsed')
        require(elapsed >= 0 and (not samples or elapsed > samples[-1]['t']), 'measurement samples not strictly ordered')
        samples.append({'id':rid,'t':elapsed,'phase':row.get('probe_phase'),'stage':row.get('probe_stage'),
                        'state':state_from_tags(row['tags'], resolved)})
    require(len(samples) >= 2, 'insufficient measurement samples')
    return samples


def edge_observed(spec, before, after):
    a, b = before['state'][spec['signal']], after['state'][spec['signal']]
    require(type(a) is bool and type(b) is bool, 'discrete edges require BOOL signals')
    return (not a and b) if spec['edge'] == 'rising' else (a and not b)


def evaluate_check(check, samples, probe_starts):
    issues, refs, measurements = [], [], []
    kind = check['kind']
    if kind in ('invariant','probe_state'):
        rows = [s for s in samples if matches(s['state'],check['when'])] if kind == 'invariant' else [
            s for s in samples if s['phase'] == check['phase'] and
            s['t'] >= probe_starts.get(check['phase'], float('inf')) + check['settle_seconds']]
        refs = [s['id'] for s in rows]
        if len(rows) < check['min_samples']: issues.append('insufficient qualifying observations; check is not vacuously true')
        bad = [s['id'] for s in rows if not matches(s['state'],check['expect'])]
        if bad: issues.append('expected signal conditions violated at ' + ','.join(bad[:8]))
        measurements = [{'observations':len(rows),'violations':len(bad)}]
    else:
        # Startup/operator probes have their own explicitly checked windows. Temporal
        # process tests use the capture stage, so the final intentional Stop is not
        # misclassified as an extra process dwell.
        rows = [s for s in samples if s['stage'] != 'after_capture']
        cursor, pending, partial = 0, None, []
        for before, after in zip(rows, rows[1:]):
            pair = [before['id'],after['id']]
            if kind == 'sequence':
                consumed = set()
                while cursor < len(check['steps']) and edge_observed(check['steps'][cursor],before,after):
                    token = (check['steps'][cursor]['signal'],check['steps'][cursor]['edge'])
                    if token in consumed: break
                    consumed.add(token); partial.extend(pair); cursor += 1
                    if cursor == len(check['steps']):
                        measurements.append({'source_record_ids':list(dict.fromkeys(partial)),
                            'completed_seconds':after['t'],'edge_order_resolution':'polling intervals; co-observed edges have no sub-frame ordering'})
                        refs.extend(partial); cursor, partial = 0, []
            elif kind == 'duration':
                if edge_observed(check['start'],before,after):
                    if pending is not None: issues.append('new start before pending duration ended')
                    pending = {'bounds':[before['t'],after['t']],'refs':pair[:],'hold_ids':[]}
                if pending is not None:
                    pending['hold_ids'].append(after['id'])
                    ending = edge_observed(check['end'],before,after)
                    if not ending and 'while' in check and not matches(after['state'],check['while']):
                        issues.append('duration hold condition violated')
                    if ending:
                        pending['refs'].extend(pair)
                        bounds = [before['t']-pending['bounds'][1],after['t']-pending['bounds'][0]]
                        if bounds[0] < check['min_seconds'] or bounds[1] > check['max_seconds']:
                            issues.append('measured duration interval outside contract: ' + str(bounds))
                        measurements.append({'duration_bounds_seconds':bounds,
                            'observed_duration_seconds':after['t']-pending['bounds'][1],
                            'evaluated_hold_record_ids':pending['hold_ids'],
                            'source_record_ids':list(dict.fromkeys(pending['refs']))})
                        refs.extend(pending['refs']); pending = None
            else:
                # Close before opening at a co-observed boundary. Expose resolution;
                # do not manufacture a strict ordering inside a polling frame.
                if edge_observed(check['end'],before,after):
                    if pending is None: issues.append('completion without an observed start')
                    else:
                        pending.extend(pair); measurements.append({'source_record_ids':list(dict.fromkeys(pending)),
                            'completed_seconds':after['t'],'boundary_interval_seconds':[before['t'],after['t']]})
                        refs.extend(pending); pending = None
                if edge_observed(check['start'],before,after):
                    if pending is not None: issues.append('new start before previous completion; overlap observed')
                    pending = pair[:]
        if len(measurements) < check['min_matches']: issues.append('insufficient complete measured matches')
    return {'id':check['id'],'kind':kind,'process_ids':check['process_ids'],'passed':not issues,
            'issues':issues,'measurements':measurements,'source_record_ids':list(dict.fromkeys(refs))}


def verify_probe_receipts(rule, evidence, missing_issues=None):
    starts = {}; records = {r['id']:r for r in evidence.get('records',[]) if r.get('id')}
    for probe in rule.get('probes',[]):
        for action in probe['actions']:
            phase = probe['id'] + '.' + action['label']; rid = 'operator-' + phase
            if rid not in records and missing_issues is not None:
                missing_issues.append('native probe action missing: ' + phase)
                continue
            require(rid in records, 'native probe action missing: ' + phase)
            row = records[rid]; path = Path(row.get('receipt_path',''))
            require(row.get('kind') == 'operator_action' and row.get('source') == 'native_operator_button' and
                    row.get('object_id') == action['object_id'] and row.get('pressed') is action['pressed'] and
                    path.is_file() and file_hash(path) == row.get('receipt_sha256'), 'invalid native probe receipt: ' + phase)
            receipt = read_json(path); actual = receipt.get('data',{}).get('operator_button',{})
            require(receipt.get('ok') is True and receipt.get('action') == 'operator_button' and
                    actual.get('native_input') is True and actual.get('pressed') is action['pressed'] and
                    actual.get('object_key') == row.get('object_key') and bool(row.get('object_key')),
                    'native probe action unconfirmed')
            starts[phase] = finite(row.get('elapsed_seconds'), 'probe time')
    return starts


def validate_signal_trace(rule, task, evidence, io_contract):
    try:
        validate_signal_contract(rule, task)
        resolved = resolve_signals(rule, io_contract)
        samples = samples_from_evidence(evidence, resolved)
        receipt_issues = []
        probe_starts = verify_probe_receipts(rule, evidence, receipt_issues)
        checks = [evaluate_check(c,samples,probe_starts) for c in rule['checks']]
        issues = receipt_issues + [c['id'] + ': ' + issue for c in checks for issue in c['issues']]
        for probe in rule.get('probes',[]):
            if probe.get('wait_for') is not None and not any(s['phase'] == probe['id'] + '.wait' and
                    matches(s['state'],probe['wait_for']) for s in samples):
                issues.append('probe trigger not measured: ' + probe['id'])
        return {'kind':'signal_contract','passed':not issues,'issues':issues,'checks':checks,
                'resolved_signals':resolved,'source_record_ids':list(dict.fromkeys(r for c in checks for r in c['source_record_ids'])),
                'scope':'model-authored observable rules; interval-bounded timing, no inferred mass or arbitrary item identity'}
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        return {'kind':'signal_contract','passed':False,'issues':[str(exc)],'checks':[]}
