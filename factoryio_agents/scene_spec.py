"""Small, strict scene contracts. Requirements are independent of generated layouts."""
from copy import deepcopy
import math
import re

from .contracts import require, canonical_hash

IDENTITY = r'[A-Za-z0-9_-]{1,80}'
STATUSES = ('satisfied', 'violated', 'unknown', 'unobservable', 'unsupported', 'not_applicable')
PLACEMENTS = {
    'absolute': {'position': 'integer XYZ grid, Y vertical', 'yaw_deg': '0|90|180|270'},
    'anchor': {'position': 'integer XYZ grid, Y vertical', 'yaw_deg': '0|90|180|270'},
    'conveyor_after_conveyor': {'relative_to': 'measured straight conveyor ID', 'clearance_grid': 'nonnegative integer'},
    'emitter_over_conveyor': {'relative_to': 'measured conveyor ID', 'along_fraction': 'upstream-to-downstream fraction in [0,1]'},
    'sensor_beside_conveyor': {'relative_to': 'measured conveyor ID', 'along_fraction': 'fraction in [0,1]',
        'side': 'left|right of flow', 'height_above_surface_grid': 'integer', 'clearance_grid': 'nonnegative integer'},
    'remover_after_conveyor': {'relative_to': 'measured conveyor ID', 'clearance_grid': 'nonnegative integer'},
    'grid_offset': {'relative_to': 'measured conveyor ID', 'offset_grid': 'integer XYZ offset', 'yaw_deg': '0|90|180|270'}}


def identity(value):
    require(isinstance(value, str) and re.fullmatch(IDENTITY, value), 'invalid stable ID: ' + str(value))
    return value


def finite(value):
    if isinstance(value, dict):
        for child in value.values(): finite(child)
    elif isinstance(value, list):
        for child in value: finite(child)
    elif type(value) is float:
        require(math.isfinite(value), 'nonfinite contract parameter')


def validate_requirement_spec(value, original, *, trusted=False):
    require(isinstance(value, dict), 'RequirementSpec must be a JSON object')
    fields={'schema_version','original_requirement','requirements','context_fragments'}
    require(set(value) <= fields,
            'Return the RequirementSpec itself, not the request envelope; unexpected root keys: '+str(sorted(set(value)-fields)))
    require(value.get('original_requirement') == original, 'RequirementSpec must preserve the original requirement')
    rows = value.get('requirements')
    require(isinstance(rows, list) and rows, 'RequirementSpec needs requirements')
    ids, covered = set(), set()
    result = deepcopy(value)
    for row in result['requirements']:
        require(isinstance(row, dict) and set(row) <= {'requirement_id', 'source_text', 'origin', 'phase', 'subject_role',
                'target_role', 'predicate', 'arguments', 'required', 'evidence_methods'}, 'invalid requirement fields')
        key = identity(row.get('requirement_id')); require(key not in ids, 'duplicate requirement ID'); ids.add(key)
        source = row.get('source_text'); require(isinstance(source, str) and source.strip(), 'requirement needs source_text')
        require(row.get('origin') in ('explicit_user', 'confirmed_engineering', 'design_assumption'), 'invalid requirement origin')
        require(row.get('phase') in ('static_scene', 'runtime'), 'invalid requirement phase')
        require(type(row.get('required')) is bool, 'required must be boolean')
        require(isinstance(row.get('predicate'), str) and row['predicate'], 'requirement predicate missing')
        require(isinstance(row.get('arguments', {}), dict), 'requirement arguments must be an object')
        row.setdefault('arguments', {}); row.setdefault('target_role', None); row.setdefault('subject_role', None)
        for field in ('subject_role', 'target_role'):
            if row[field] is not None: identity(row[field])
        if row['origin'] == 'design_assumption':
            require(not row['required'], 'design assumptions cannot become hard requirements')
        if row['origin'] == 'confirmed_engineering':
            require(trusted, 'model output cannot confirm engineering constraints')
        if row['origin'] == 'explicit_user':
            require(source in original, 'Requirement '+key+' source_text is not a literal substring: '+repr(source)+
                    '. Copy a contiguous quote from original_requirement, including its conjunctions. Several requirements may share the same original quote.')
            for match in re.finditer(re.escape(source), original): covered.update(range(match.start(), match.end()))
        methods = row.get('evidence_methods', [])
        require(isinstance(methods, list) and set(methods) <= {'structure', 'native_geometry', 'visual'}, 'invalid evidence method')
    fragments=result.get('context_fragments',[])
    require(isinstance(fragments,list),'context_fragments must be an array')
    for fragment in fragments:
        require(isinstance(fragment,dict) and set(fragment)=={'source_text','reason'} and
                isinstance(fragment['source_text'],str) and fragment['source_text'] and fragment['source_text'] in original and
                isinstance(fragment['reason'],str) and fragment['reason'].strip(),'context fragments must quote and explain nonconstraint framing')
        for match in re.finditer(re.escape(fragment['source_text']),original):covered.update(range(match.start(),match.end()))
    omitted = [i for i, char in enumerate(original) if char.isalnum() and i not in covered]
    gaps=[];start=None
    for i in range(len(original)+1):
        if i<len(original) and i not in covered:
            if start is None:start=i
        elif start is not None:
            text=original[start:i].strip()
            if any(c.isalnum() for c in text):gaps.append(text)
            start=None
    require(not omitted, 'RequirementSpec omits source fragments: '+str(gaps)+'. Keep constraints as requirements; quote nonconstraint framing in context_fragments with a reason.')
    finite(result); result['schema_version'] = 1
    return result


def hard_static(spec):
    return [r for r in spec['requirements'] if r['required'] and r['phase'] == 'static_scene'
            and r['origin'] != 'design_assumption']


def validate_requirement_bindings(spec, catalog, registry):
    """Validate selector vocabulary before accepting independent model extraction.

    Unavailable requirements remain unsupported; this checks adapter bindings,
    not the semantic accuracy of a quoted requirement.
    """
    prefabs = {t['prefab'] for t in catalog['templates']}
    capabilities = {c for m in catalog.get('native_geometry', {}).get('prefabs', {}).values()
                    for c in m.get('component_geometry_local', {})}
    def check(operator, arguments):
        if operator not in registry.operators: return
        registry.validate_arguments(operator, arguments)
        if operator == 'forall':
            check(arguments['operator'], arguments.get('arguments', {}))
        elif operator in ('count', 'exists', 'has_capability'):
            if 'prefab' in arguments:
                require(arguments['prefab'] in prefabs,
                        'Unknown exact prefab selector: ' + arguments['prefab'] +
                        '. Use an exact available_components prefab, or its declared measured capability for a component category. Preserve genuinely unavailable equipment as unsupported.')
            if 'capability' in arguments:
                require(arguments['capability'] in capabilities,
                        'Unknown measured capability selector: ' + arguments['capability'] +
                        '. Use declared measured_capabilities; retain unavailable requirements as unsupported.')
    for row in hard_static(spec):
        from .contracts import ContractError
        try: check(row['predicate'], row['arguments'])
        except ContractError as error:
            raise ContractError('Requirement '+row['requirement_id']+' invalid '+row['predicate']+' arguments '+str(row['arguments'])+
                                ': '+str(error)+'. Omit unused argument keys; optional does not mean nullable.') from None
    return spec


def validate_scene_design(design, spec, catalog):
    require(isinstance(design, dict) and isinstance(design.get('objects'), list) and design['objects'], 'scene design needs objects')
    identity(design.get('scene_id'))
    available = {t['prefab'] for t in catalog['templates']}; ids = set(); placement_errors=[]
    for obj in design['objects']:
        key = identity(obj.get('id')); require(key not in ids, 'duplicate object ID'); ids.add(key)
        require(obj.get('prefab') in available, 'component is not in supplied catalog')
        roles = obj.get('roles'); require(isinstance(roles, list) and roles and len(set(roles)) == len(roles), 'object needs distinct roles')
        for role in roles: identity(role)
        require(isinstance(obj.get('function'), str) and obj['function'].strip(), 'object function missing')
        require(isinstance(obj.get('placement'), dict), 'object placement missing')
        placement=obj['placement'];kind=placement.get('kind')
        require(kind in PLACEMENTS,'unsupported placement kind: '+str(kind))
        expected={'kind',*PLACEMENTS[kind]}
        if set(placement)!=expected:
            placement_errors.append(obj['id']+': Placement '+str(kind)+' requires exact keys '+str(sorted(expected))+
                '; missing '+str(sorted(expected-set(placement)))+'; unexpected '+str(sorted(set(placement)-expected)))
    mapped_roles={role for obj in design['objects'] for role in obj['roles']}
    needed=set()
    for req in hard_static(spec):
        if req['predicate']=='count' and req['arguments'].get('equals',req['arguments'].get('min',0))==0:continue
        needed.update(req[k] for k in ('subject_role','target_role') if req[k])
    if not needed<=mapped_roles:
        placement_errors.append('Unmapped requirement roles: '+str(sorted(needed-mapped_roles))+
            '. An existing physical object can serve several requirement roles; include all implemented roles in its roles array.')
    require(not placement_errors,'; '.join(placement_errors))
    relations = design.get('relations'); require(isinstance(relations, list), 'scene design needs relations array')
    relation_ids = set(); requirement_ids = {r['requirement_id'] for r in spec['requirements']}
    for row in relations:
        require(isinstance(row, dict) and set(row) == {'id', 'subject_id', 'predicate', 'target_id', 'parameters', 'requirement_ids'}, 'invalid relation fields')
        key = identity(row['id']); require(key not in relation_ids, 'duplicate relation ID'); relation_ids.add(key)
        require(row['subject_id'] in ids and row['target_id'] in ids and row['subject_id'] != row['target_id'], 'invalid relation endpoints')
        require(isinstance(row['predicate'], str) and isinstance(row['parameters'], dict), 'invalid relation predicate/parameters')
        require(isinstance(row['requirement_ids'], list) and set(row['requirement_ids']) <= requirement_ids, 'relation references unknown requirement')
    finite(design)
    return deepcopy(design)


def validate_plan(plan, catalog):
    templates = {t['template_id']: t for t in catalog['templates']}; ids = set()
    for obj in plan['objects']:
        key = identity(obj['id']); require(key not in ids, 'duplicate layout object'); ids.add(key)
        require(obj['template_id'] in templates, 'unknown layout template')
        require(isinstance(obj['position'], list) and len(obj['position']) == 3 and all(type(v) is int for v in obj['position']), 'layout requires integer XYZ')
        require(type(obj['yaw_deg']) in (int, float) and obj['yaw_deg'] in (0, 90, 180, 270), 'layout requires cardinal yaw')
    finite(plan)
    return plan


def contract_version(spec, design, plan):
    return {'requirements': canonical_hash(spec), 'design': canonical_hash(design), 'layout': canonical_hash(plan)}


REQUIREMENT_SHAPE = {
    'original_requirement': 'verbatim input',
    'context_fragments':[{'source_text':'verbatim nonconstraint framing only','reason':'why this is framing, not an omitted constraint'}],
    'requirements': [{'requirement_id': 'stable ID', 'source_text': 'verbatim source fragment',
        'origin': 'explicit_user or design_assumption', 'phase': 'static_scene or runtime',
        'subject_role': 'semantic role or null', 'target_role': 'semantic role or null',
        'predicate': 'registered operator, or the unavailable capability name', 'arguments': {},
        'required': True, 'evidence_methods': ['structure', 'native_geometry', 'visual']}]}

DESIGN_SHAPE = {'scene_id': 'stable ID', 'scene_goal': 'physical scene purpose', 'control_intent': [],
    'objects': [{'id': 'stable ID', 'prefab': 'catalog prefab', 'roles': ['requirement role'],
        'function': 'purpose in this task', 'placement': {'kind': 'supplied placement kind'}, 'settings': []}],
    'relations': [{'id': 'stable ID', 'subject_id': 'object ID', 'predicate': 'relation', 'target_id': 'object ID',
        'parameters': {}, 'requirement_ids': ['source requirement ID']}], 'unresolved': []}
