"""Explicit benchmark perturbations and scoring; never default production rules."""
from copy import deepcopy
import math

from .contracts import require, canonical_hash
from .geometry_tools import project_object


def inject_fault(plan, design, catalog, kind):
    result = deepcopy(plan); templates = {t['template_id']: t['prefab'] for t in catalog['templates']}
    native = catalog['native_geometry']; scale = native['coordinate_system']['grid_metres']
    measured = {o['id']: project_object(o, templates[o['template_id']], native['prefabs'][templates[o['template_id']]], scale) for o in result['objects']}
    if kind == 'missing_object':
        require(len(result['objects'])>1,'missing-object injection needs a remaining renderable scene')
        target = result['objects'][-1]
    else:
        capability = 'emitter' if kind == 'emitter_gap' else 'sensor' if kind == 'sensor_parallel' else 'conveyor'
        candidates = [o for o in result['objects'] if capability in measured[o['id']]['component_geometry']]
        require(candidates, 'fault has no measured compatible object')
        if kind == 'transfer_gap':
            links = [r for r in design['relations'] if r['predicate'] == 'transfers_to']
            require(links, 'transfer gap injection needs a selected transfer relation')
            target = next(o for o in candidates if o['id'] == links[0]['target_id'])
        else: target = candidates[0]
    original = deepcopy(target)
    if kind == 'missing_object': result['objects'].remove(target)
    elif kind == 'sensor_parallel': target['yaw_deg'] = (target['yaw_deg'] + 90) % 360
    elif kind == 'height_offset': target['position'][1] += max(1, math.ceil((measured[target['id']]['bounds_world']['max'][1] - measured[target['id']]['bounds_world']['min'][1]) / scale))
    else:
        bounds = measured[target['id']]['bounds_world']
        if kind == 'emitter_gap':
            belts = [r for r in measured.values() if 'conveyor' in r['component_geometry']]
            require(belts, 'feed offset injection needs a measured carrier')
            links = [r for r in design['relations'] if r['subject_id'] == target['id'] and r['predicate'] == 'feeds_into']
            carrier = measured[links[0]['target_id']] if links else belts[0]
            flow = carrier['component_geometry']['conveyor']['direction']; axis = 2 if abs(flow[0]) > abs(flow[2]) else 0
            target['position'][axis] += math.ceil((carrier['bounds_world']['max'][axis] - carrier['bounds_world']['min'][axis] + bounds['max'][axis] - bounds['min'][axis]) / scale) + 1
        else:
            flow = measured[target['id']]['component_geometry']['conveyor']['direction']; axis = max((0, 2), key=lambda i: abs(flow[i]))
            target['position'][axis] += (1 if flow[axis] > 0 else -1) * (math.ceil((bounds['max'][axis] - bounds['min'][axis]) / scale) + 1)
    return result, {'kind': kind, 'object_id': target['id'], 'before': original,
        'after': deepcopy(target) if kind != 'missing_object' else None, 'source_layout_sha256': canonical_hash(plan),
        'scope': 'controlled experiment perturbation, not an acceptance rule'}


def score_cases(cases):
    """Ground truth must be supplied by an independently reviewed benchmark manifest."""
    tp = fp = fn = tn = localized = repairs = repair_trials = preserved = preservation_trials = 0
    for case in cases:
        truth = set(case['expected_violated_check_ids']); predicted = set(case['observed_violated_check_ids'])
        universe = set(case['all_check_ids']); require(truth <= universe and predicted <= universe, 'benchmark check IDs differ')
        tp += len(truth & predicted); fp += len(predicted - truth); fn += len(truth - predicted); tn += len(universe - truth - predicted)
        if truth & predicted: localized += int(set(case.get('expected_object_ids', [])) == set(case.get('observed_object_ids', [])))
        if case.get('repair_attempted'):
            repair_trials += 1; repairs += int(case.get('post_repair_static_verified') is True)
            preservation_trials += 1; preserved += int(case.get('unaffected_objects_preserved') is True)
    return {'true_positive': tp, 'false_positive': fp, 'false_negative': fn, 'true_negative': tn,
        'error_detection_recall': tp / (tp + fn) if tp + fn else None,
        'false_positive_rate': fp / (fp + tn) if fp + tn else None,
        'object_localization_count': localized, 'repair_success': repairs / repair_trials if repair_trials else None,
        'unaffected_object_preservation': preserved / preservation_trials if preservation_trials else None,
        'ground_truth_basis': 'independently supplied expected check and object IDs'}
