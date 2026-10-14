"""Task-bound reusable operators; no scene names or mandatory device topology."""
from copy import deepcopy
import itertools
import math

from .contracts import require, canonical_hash
from .geometry_tools import project_object
from .scene_spec import hard_static, finite, STATUSES

EPS = 1e-7  # Floating-point comparison only, never an engineering clearance.


def result(check, status, checker, reason, observed=None, evidence_ids=(), missing=None):
    require(status in STATUSES, 'invalid verification status')
    return {'check_id': check['check_id'], 'status': status, 'checker': checker,
        'object_ids': check['object_ids'], 'observed': observed, 'expected': check['arguments'],
        'evidence_ids': list(evidence_ids), 'reason': reason, 'missing_capability': missing}


class CheckerRegistry:
    def __init__(self): self.operators = {}

    def register(self, name, function, arguments, *, capability=(), methods=('native_geometry', 'visual')):
        require(name not in self.operators, 'duplicate checker')
        self.operators[name] = {'function': function, 'arguments': arguments,
            'capabilities': list(capability), 'evidence_methods': list(methods)}

    def describe(self):
        meanings={'count':'Count selected physical roles, optionally filtered by exact prefab or measured capability.',
            'exists':'Presence of the selected physical role; no transport equipment is implied.',
            'feeds_into':'Subject emitter origin lies above target carrier footprint. Upstream region can also be reviewed visually.',
            'receives_from':'Subject receiving volume (currently Remover) intersects target carrier downstream continuation; visible proximity/height also need review.',
            'detects_path':'Subject sensor beam reaches target carrier path. Orientation and height limits only apply when explicitly requested.',
            'transfers_to':'Selected upstream and downstream carriers have compatible measured straight carrying surfaces. Missing permitted gap/height tolerance remains unknown.',
            'reachable':'Reachability over the selected transfer relations, not all components.',
            'visual_relation':'Qualitative visible arrangement, including region or topology questions without exact numeric geometry.',
            'unsupported':'A physical capability not provided by the current adapters; not a substitute for a supported visual question.',
            'forall':'Apply a supported nonrecursive predicate to only selected role instances.'}
        return {key: {**{k:deepcopy(v) for k,v in row.items() if k!='function'},'meaning':meanings.get(key,key.replace('_',' '))} for key,row in self.operators.items()}

    def validate_arguments(self, name, arguments):
        require(name in self.operators, 'unregistered checker')
        require(isinstance(arguments, dict), 'operator arguments must be an object')
        allowed = self.operators[name]['arguments']
        require(not set(arguments) - set(allowed), 'unsupported arguments for ' + name + ': ' + str(sorted(set(arguments) - set(allowed))))
        for key, value in arguments.items():
            type_ = allowed[key]
            if type_ == 'number': require(type(value) in (int, float) and math.isfinite(value), 'invalid numeric ' + key)
            elif type_ == 'nonnegative': require(type(value) in (int, float) and math.isfinite(value) and value >= 0, 'invalid nonnegative ' + key)
            elif type_ == 'integer': require(type(value) is int and value >= 0, 'invalid integer ' + key)
            elif type_ == 'axes': require(isinstance(value, list) and value and set(value) <= {'X', 'Y', 'Z'}, 'invalid axes')
            elif type_ == 'bounds':
                require(isinstance(value, dict) and set(value) == {'min', 'max'} and
                        all(isinstance(value[k], list) and len(value[k]) == 3 and all(type(v) in (int, float) for v in value[k]) for k in value), 'invalid bounds')
                require(all(a <= b for a, b in zip(value['min'], value['max'])), 'inverted bounds')
            elif type_ == 'string': require(isinstance(value, str) and value, 'invalid string ' + key)
            elif type_ == 'boolean': require(type(value) is bool, 'invalid boolean ' + key)
            elif type_ == 'object': require(isinstance(value,dict),'invalid object '+key)
        finite(arguments)
        if name=='forall':
            require(arguments.get('operator') in self.operators and arguments['operator'] not in ('forall','registered','unsupported'),'forall needs a supported nonrecursive predicate')
            self.validate_arguments(arguments['operator'],arguments.get('arguments',{}))

    def evaluate(self, check, context):
        name = check['operator']; self.validate_arguments(name, check['arguments'])
        return self.operators[name]['function'](check, context)


def compile_check_plan(spec, design, registry):
    roles = {}
    for obj in design['objects']:
        for role in obj['roles']: roles.setdefault(role, []).append(obj['id'])
    checks = []
    for req in hard_static(spec):
        key = req['requirement_id']; operator = req['predicate']; args = deepcopy(req['arguments'])
        subjects = roles.get(req['subject_role'], []) if req['subject_role'] else [o['id'] for o in design['objects']]
        targets = roles.get(req['target_role'], []) if req['target_role'] else []
        if operator not in registry.operators:
            checks.append({'check_id': key + '-unsupported', 'requirement_ids': [key], 'operator': 'unsupported',
                'object_ids': subjects + [t for t in targets if t not in subjects], 'arguments': {'capability': operator},
                'relation_ids': [], 'evidence_methods': [], 'applicability': 'required_static'})
            continue
        registry.validate_arguments(operator, args)
        pairs = [(subjects, [], [])]
        if req['target_role']:
            related = [r for r in design['relations'] if key in r['requirement_ids'] and (r['predicate'] == operator or operator == 'visual_relation')
                       and r['subject_id'] in subjects and r['target_id'] in targets]
            if req['subject_role'] == req['target_role']:
                # Only explicitly selected relation edges. Reachability is a separate task predicate.
                pairs = [([r['subject_id']], [r['target_id']], [r['id']]) for r in related] or [(subjects, [], [])]
            else:
                pairs = []
                for subject in subjects or [None]:
                    links = [r for r in related if r['subject_id'] == subject]
                    selected = [(r['target_id'], [r['id']]) for r in links] if links else [(t, []) for t in targets or [None]]
                    for target, relation_ids in selected:
                        pairs.append(([subject] if subject else [], [target] if target else [], relation_ids))
        for index, (ss, tt, relations) in enumerate(pairs):
            methods = registry.operators[args['operator'] if operator=='forall' else operator]['evidence_methods']
            checks.append({'check_id': key + '-' + str(index + 1), 'requirement_ids': [key], 'operator': operator,
                'object_ids': list(dict.fromkeys(ss + tt)), 'subject_ids': ss, 'target_ids': tt,
                'arguments': args, 'relation_ids': relations, 'evidence_methods': list(methods), 'applicability': 'required_static'})
    checks.append({'check_id': 'G-registration', 'requirement_ids': [], 'operator': 'registered',
        'object_ids': [o['id'] for o in design['objects']], 'subject_ids': [o['id'] for o in design['objects']],
        'target_ids': [], 'relation_ids': [], 'arguments': {}, 'evidence_methods': ['native_geometry'], 'applicability': 'software_prerequisite'})
    coverage = {r['requirement_id']: [c['check_id'] for c in checks if r['requirement_id'] in c['requirement_ids']] for r in hard_static(spec)}
    require(all(coverage.values()), 'hard static requirement is not covered')
    return {'schema_version': 1, 'requirements_sha256': canonical_hash(spec), 'checks': checks, 'coverage': coverage,
            'deferred_requirement_ids': [r['requirement_id'] for r in spec['requirements'] if r['phase'] == 'runtime']}


def project_scene(plan, catalog):
    native = catalog.get('native_geometry', {}); templates = {t['template_id']: t for t in catalog['templates']}
    scale = native.get('coordinate_system', {}).get('grid_metres'); objects = {}
    for obj in plan['objects']:
        prefab = templates[obj['template_id']]['prefab']; metric = native.get('prefabs', {}).get(prefab)
        if metric and scale and isinstance(metric.get('bounds_local_metres'), dict):
            objects[obj['id']] = project_object(obj, prefab, metric, scale)
    return objects


def evaluate_plan(check_plan, design, plan, catalog, registry, *, receipt=None, geometry_enabled=True):
    if receipt is None:
        measured = project_scene(plan, catalog); checker = 'projected_geometry'
        evidence = ['layout-' + canonical_hash(plan)]
    else:
        keys = {o['key']: o['id'] for o in receipt.get('manifest_objects', [])}
        measured = {keys.get(o['key'], o.get('id')): {**o, 'id': keys.get(o['key'], o.get('id'))}
                    for o in receipt.get('native_geometry', {}).get('objects', []) if keys.get(o['key'], o.get('id'))}
        checker = 'native_geometry'; evidence = ['load-' + receipt['request_id']]
    context = {'objects': {o['id']: o for o in design['objects']}, 'measured': measured, 'catalog': catalog,
        'relations': design['relations'], 'checker': checker, 'evidence_ids': evidence, 'geometry_enabled': geometry_enabled}
    return [registry.evaluate(c, context) for c in check_plan['checks']]


def _answer(check, ctx, status, reason, observed=None, missing=None):
    return result(check, status, ctx['checker'], reason, observed, ctx['evidence_ids'], missing)


def _selection(check, ctx, *, pair=False, capabilities=()):
    ids = check.get('subject_ids', [])
    if not ids or (pair and not check.get('target_ids')):
        return _answer(check, ctx, 'violated', 'Required object role or relation endpoint is missing.', {'present_object_ids': check['object_ids']})
    for identity in check['object_ids']:
        if identity not in ctx['measured']:
            return _answer(check, ctx, 'unsupported', 'Native measurement is unavailable for a required object.', missing='native_geometry:' + identity)
    if capabilities:
        selected = [ids[0], check['target_ids'][0]] if pair else ids
        for identity, capability in zip(selected, capabilities):
            if capability and capability not in ctx['measured'][identity].get('component_geometry', {}):
                return _answer(check, ctx, 'unsupported', 'Required measured component capability is unavailable.', missing=capability + ':' + identity)
    if not ctx['geometry_enabled']:
        return _answer(check, ctx, 'unknown', 'Native geometry ablated in this comparison mode.')
    return None


def _count(c, ctx):
    a = c['arguments']; selected = c.get('subject_ids', [])
    if 'prefab' in a: selected = [i for i in selected if ctx['objects'][i]['prefab'] == a['prefab']]
    if 'capability' in a:
        if any(i not in ctx['measured'] for i in selected): return _answer(c, ctx, 'unsupported', 'Required count selector has no native capability evidence.', missing=a['capability'])
        selected = [i for i in selected if a['capability'] in ctx['measured'][i].get('component_geometry', {})]
    count = len(selected)
    require(any(k in a for k in ('equals', 'min', 'max')) or c['operator'] == 'exists', 'count needs a requested bound')
    minimum = a.get('min', 1 if c['operator'] == 'exists' and 'equals' not in a else 0)
    passed = minimum <= count <= a.get('max', math.inf) and ('equals' not in a or count == a['equals'])
    row=_answer(c, ctx, 'satisfied' if passed else 'violated', 'Object-role count compared with independent requirement.', {'count': count})
    row['object_ids']=selected
    return row


def _registered(c, ctx):
    if ctx['checker'] != 'native_geometry': return _answer(c, ctx, 'unknown', 'Registration requires actual software loading.')
    missing = [i for i in c['object_ids'] if i not in ctx['measured'] or type(ctx['measured'][i].get('native_registered')) is not bool]
    if missing: return _answer(c, ctx, 'unsupported', 'Native registration receipt is incomplete.', missing='registration:' + ','.join(missing))
    failed = [i for i in c['object_ids'] if not ctx['measured'][i]['native_registered']]
    row = _answer(c, ctx, 'violated' if failed else 'satisfied', 'Actual voxel object registration.', {'failed_object_ids': failed})
    if failed: row['object_ids'] = failed
    return row


def _forall(c,ctx):
    if not c.get('subject_ids'):return _answer(c,ctx,'not_applicable','No selected role instances; a universal statement is not evidence that required equipment exists.')
    registry=default_registry();rows=[]
    for identity in c['subject_ids']:
        inner={**c,'operator':c['arguments']['operator'],'arguments':c['arguments'].get('arguments',{}),'subject_ids':[identity],
               'object_ids':list(dict.fromkeys([identity]+c.get('target_ids',[])))}
        rows.append(registry.evaluate(inner,ctx))
    statuses={r['status'] for r in rows}
    status=next((s for s in ('violated','unsupported','unknown','unobservable','not_applicable') if s in statuses),'satisfied')
    return _answer(c,ctx,status,'Universal check over only the requirement-selected role instances.',rows)


def _capability(c, ctx):
    missing = _selection(c, ctx)
    if missing: return missing
    require('capability' in c['arguments'], 'has_capability needs capability')
    passed = all(c['arguments']['capability'] in ctx['measured'][i].get('component_geometry', {}) for i in c['subject_ids'])
    return _answer(c, ctx, 'satisfied' if passed else 'unsupported', 'Capability checked against native component measurements.',
                   missing=None if passed else c['arguments']['capability'])


def _spatial(c, ctx):
    pair = c['operator'] not in ('within_bounds',)
    missing = _selection(c, ctx, pair=pair)
    if missing: return missing
    a = ctx['measured'][c['subject_ids'][0]]['bounds_world']; args = c['arguments']
    if c['operator'] == 'within_bounds':
        b = args.get('bounds') or ctx['catalog'].get('native_geometry', {}).get('world_bounds')
        if not b: return _answer(c, ctx, 'unsupported', 'Measured world bounds unavailable.', missing='world_bounds')
        passed = all(all(b['min'][j] - EPS <= ctx['measured'][i]['bounds_world']['min'][j] and
                         ctx['measured'][i]['bounds_world']['max'][j] <= b['max'][j] + EPS for j in range(3)) for i in c['subject_ids'])
        return _answer(c, ctx, 'satisfied' if passed else 'violated', 'Bounds containment, not a mesh collision test.')
    b = ctx['measured'][c['target_ids'][0]]['bounds_world']
    ca = [(lo + hi) / 2 for lo, hi in zip(a['min'], a['max'])]; cb = [(lo + hi) / 2 for lo, hi in zip(b['min'], b['max'])]
    observed = {'center_delta_metres': [x - y for x, y in zip(ca, cb)]}
    if c['operator'] == 'relative_direction':
        require(args.get('axis') in ('X', 'Y', 'Z') and args.get('direction') in ('positive', 'negative'), 'relative_direction needs axis and direction')
        delta = observed['center_delta_metres'][('X', 'Y', 'Z').index(args['axis'])]
        passed = delta > EPS if args['direction'] == 'positive' else delta < -EPS
    elif c['operator'] == 'aligned':
        require('axes' in args and 'tolerance_metres' in args, 'alignment needs requested axes and tolerance')
        passed = all(abs(observed['center_delta_metres'][('X', 'Y', 'Z').index(axis)]) <= args['tolerance_metres'] + EPS for axis in args['axes'])
    elif c['operator'] == 'distance_range':
        require(any(k in args for k in ('min_metres', 'max_metres')), 'distance_range needs requested bounds')
        observed['distance_metres'] = math.dist(ca, cb)
        passed = args.get('min_metres', 0) - EPS <= observed['distance_metres'] <= args.get('max_metres', math.inf) + EPS
    else:
        passed = all(min(a['max'][j], b['max'][j]) >= max(a['min'][j], b['min'][j]) - EPS for j in range(3))
    return _answer(c, ctx, 'satisfied' if passed else 'violated', 'Measured spatial relation compared with task parameters.', observed)


def _industrial(c, ctx):
    op = c['operator']; caps = {'feeds_into': ('emitter', 'conveyor'), 'transfers_to': ('conveyor', 'conveyor'),
        'detects_path': ('sensor', 'conveyor'), 'receives_from': ('', 'conveyor'), 'ray_intersects_volume': ('sensor', '')}[op]
    missing = _selection(c, ctx, pair=True, capabilities=caps)
    if missing: return missing
    subject = ctx['measured'][c['subject_ids'][0]]; target = ctx['measured'][c['target_ids'][0]]; args = c['arguments']
    if op == 'ray_intersects_volume':
        from .geometry_tools import ray_intersects_bounds
        ray = subject['component_geometry']['sensor']; b = target['bounds_world']
        passed = ray_intersects_bounds(ray['origin'], ray['direction'], ray['range_metres'], b)
        return _answer(c, ctx, 'satisfied' if passed else 'violated', 'Finite native ray intersects requested object volume; no trigger claim.')
    from .geometry_tools import relation_geometry
    observation = relation_geometry(op, subject, target, args)
    return _answer(c, ctx, observation['status'], observation['reason'], observation.get('observed'), observation.get('missing_capability'))


def _reachable(c, ctx):
    missing = _selection(c, ctx, pair=True)
    if missing: return missing
    graph = {}; observed = []
    for row in ctx['relations']:
        if row['predicate'] != 'transfers_to': continue
        edge = {**c, 'operator': 'transfers_to', 'subject_ids': [row['subject_id']], 'target_ids': [row['target_id']],
                'object_ids': [row['subject_id'], row['target_id']]}
        r = _industrial(edge, ctx); observed.append({'relation_id': row['id'], 'status': r['status']})
        if r['status'] == 'unsupported': return _answer(c, ctx, 'unsupported', r['reason'], observed, r['missing_capability'])
        if r['status'] == 'unknown': return _answer(c, ctx, 'unknown', r['reason'], observed)
        if r['status'] == 'satisfied': graph.setdefault(row['subject_id'], set()).add(row['target_id'])
    visited = set(c['subject_ids']); pending = list(visited)
    while pending:
        for identity in graph.get(pending.pop(), set()) - visited: visited.add(identity); pending.append(identity)
    passed = set(c['target_ids']) <= visited
    return _answer(c, ctx, 'satisfied' if passed else 'violated', 'Reachability over selected and geometrically checked transfer relations.', observed)


def default_registry():
    r = CheckerRegistry()
    r.register('registered', _registered, {}, methods=('native_geometry',))
    r.register('forall', _forall, {'operator':'string','arguments':'object'})
    for op in ('exists', 'count'): r.register(op, _count, {'equals': 'integer', 'min': 'integer', 'max': 'integer', 'prefab': 'string', 'capability': 'string'}, methods=('structure',))
    r.register('has_capability', _capability, {'capability': 'string'}, methods=('native_geometry',))
    r.register('within_bounds', _spatial, {'bounds': 'bounds'}, methods=('native_geometry',))
    r.register('relative_direction', _spatial, {'axis': 'string', 'direction': 'string'})
    r.register('aligned', _spatial, {'axes': 'axes', 'tolerance_metres': 'nonnegative'})
    r.register('distance_range', _spatial, {'min_metres': 'nonnegative', 'max_metres': 'nonnegative'})
    r.register('intersects_volume', _spatial, {})
    r.register('ray_intersects_volume', _industrial, {}, capability=('sensor',))
    for op in ('feeds_into', 'transfers_to', 'receives_from', 'detects_path'):
        r.register(op, _industrial, {'max_gap_metres': 'nonnegative', 'height_tolerance_metres': 'nonnegative',
            'crossing_required': 'boolean', 'max_flow_dot': 'nonnegative', 'min_height_metres': 'number', 'max_height_metres': 'number'})
    r.register('reachable', _reachable, {'max_gap_metres': 'nonnegative', 'height_tolerance_metres': 'nonnegative'})
    r.register('visual_relation', lambda c, ctx: _answer(c, ctx, 'unknown', 'This requested relation requires visual evidence.'), {'question': 'string'}, methods=('visual',))
    r.register('unsupported', lambda c, ctx: _answer(c, ctx, 'unsupported', 'Requested checker is unavailable.', missing=c['arguments']['capability']), {'capability': 'string'}, methods=())
    return r


def fuse_results(check_plan, geometry_results, visual, *, registration_passed):
    by_id = {r['check_id']: r for r in geometry_results}; seen = {r['check_id']: r for r in visual['check_results']}; rows = []
    for check in check_plan['checks']:
        native = by_id[check['check_id']]; observation = seen.get(check['check_id'])
        row = deepcopy(native)
        geometry_incomplete = native['status']=='unknown' and 'native_geometry' in check['evidence_methods'] and 'ablated' not in native['reason']
        if native['status'] not in ('violated', 'unsupported') and not geometry_incomplete and 'visual' in check['evidence_methods']:
            if observation:
                row.update(status=observation['status'], checker='geometry_and_visual', reason=observation['reason'],
                    evidence_ids=list(dict.fromkeys(native['evidence_ids'] + observation['image_ids'])))
            else: row.update(status='unobservable', reason='Required visual check was not returned.')
        if observation and observation['status'] == 'violated' and native['status'] != 'unsupported':
            row.update(status='violated', checker='geometry_and_visual', reason=observation['reason'],
                       evidence_ids=list(dict.fromkeys(native['evidence_ids'] + observation['image_ids'])))
            if native['status']!='violated':row['object_ids']=observation['object_ids']
        rows.append(row)
    satisfied = registration_passed and all(r['status'] == 'satisfied' for r in rows) and visual['global_review']['status'] == 'satisfied'
    return {'status': 'satisfied' if satisfied else 'violated' if any(r['status'] == 'violated' for r in rows) or
            visual['global_review']['status'] == 'violated' else 'unknown', 'checks': rows, 'coverage': check_plan['coverage'],
            'registration_passed': registration_passed, 'static_requirements_covered': satisfied,
            'scope': 'finite static scene evidence; no PLC or actual material transport acceptance', 'global_review': visual['global_review']}


def repair_ticket(check_plan, verification, visual, design):
    violated = [r for r in verification['checks'] if r['status'] == 'violated']; checks = {c['check_id']: c for c in check_plan['checks']}
    findings = [f for f in visual['global_review']['findings'] if f['status'] == 'violated' and f['requirement_ids']]
    objects=set();count_operations=set()
    for row in violated:
        check=checks[row['check_id']]
        if check['operator'] in ('exists','count'):
            args=check['arguments'];minimum=args.get('equals',args.get('min',1 if check['operator']=='exists' else 0))
            deficit=row.get('observed',{}).get('count',0)<minimum
            count_operations.add('add' if deficit else 'remove')
            targets=[] if deficit else row['object_ids']
        elif check['operator']=='registered':targets=row['object_ids']
        elif check['operator']=='transfers_to':targets=check.get('target_ids',[])
        elif check['operator'] in ('feeds_into','detects_path','receives_from','ray_intersects_volume','relative_direction','aligned','distance_range'):
            targets=check.get('subject_ids',[])
        elif check['operator']=='visual_relation':
            named={i for f in findings if set(f['requirement_ids']) & set(check['requirement_ids']) for i in f['object_ids']}
            targets=[i for i in row['object_ids'] if i in named] or row['object_ids']
        else:targets=row['object_ids']
        objects.update(targets)
    objects.update(i for f in findings for i in f['object_ids'])
    requirement_ids = {i for r in violated for i in checks[r['check_id']]['requirement_ids']} | {i for f in findings for i in f['requirement_ids']}
    operations = ['change', 'replace'] if objects else []
    operations += sorted(count_operations)
    if any(not f['object_ids'] for f in findings): operations.append('add')
    if not violated and not findings: return None
    return {'ticket_id': 'ticket-' + canonical_hash([violated, findings])[:16], 'failed_check_ids': [r['check_id'] for r in violated],
        'requirement_ids': sorted(requirement_ids), 'affected_object_ids': sorted(objects),
        'issue_type': 'requirement_relation_violation', 'evidence_ids': sorted({i for r in violated for i in r['evidence_ids']} |
            {i for f in findings for i in f['image_ids']}), 'repair_intent': [r['reason'] for r in violated] + [f['repair_intent'] for f in findings],
        'allowed_operations': sorted(set(operations)), 'frozen_object_ids': sorted({o['id'] for o in design['objects']} - objects)}
