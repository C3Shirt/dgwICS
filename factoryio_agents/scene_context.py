"""Role-specific, allowlisted prompt contexts with traceable source identities."""
import re
from copy import deepcopy

from .contracts import canonical_hash
from .component_kb import knowledge_base
from .scene_spec import hard_static, DESIGN_SHAPE

PROMPT_VERSION = 'issue4-guided-context-v3'


class PromptContextBuilder:
    def __init__(self, catalog, spec, placements):
        self.catalog, self.spec, self.placements = catalog, spec, placements
        self.sources = {'requirements_sha256': canonical_hash(spec),
            'native_geometry_sha256': canonical_hash(catalog.get('native_geometry', {})),
            'template_sources': {s['id']: s['sha256'] for s in catalog['sources']},
            'knowledge_sha256': knowledge_base().identity}

    def manifest(self, kind, fields, object_ids=()):
        return {'prompt_version': PROMPT_VERSION, 'kind': kind, 'sources': self.sources,
            'object_scope': list(object_ids), 'field_sources': fields,
            'limitations': ['Generated roles/functions are identification context, not independent ground truth.',
                            'Native data describes this loaded software/version, not proven runtime behavior.']}

    def candidates(self, components, requirements=None, selected_prefabs=()):
        rows = requirements if requirements is not None else hard_static(self.spec)
        prefabs = set(selected_prefabs); capabilities = set(); hard_capabilities=set(); unresolved = []
        need = {'feeds_into': {'emitter', 'conveyor'}, 'detects_path': {'sensor', 'conveyor'},
                'transfers_to': {'conveyor'}, 'reachable': {'conveyor'}, 'receives_from': {'conveyor'},
                'ray_intersects_volume': {'sensor'}}
        for req in rows:
            if req['predicate'] in ('count','exists') and (req['arguments'].get('equals')==0 or req['arguments'].get('max')==0):
                continue  # An exclusion needs no candidate device to instantiate.
            capabilities.update(need.get(req['predicate'], set()))
            hard_capabilities.update(need.get(req['predicate'],set()))
            if req['predicate'] in ('count','exists','has_capability') and req['arguments'].get('capability'):
                capabilities.add(req['arguments']['capability']);hard_capabilities.add(req['arguments']['capability'])
            if req['arguments'].get('prefab'): prefabs.add(req['arguments']['prefab'])
            if req['predicate'] == 'receives_from': prefabs.add('Remover')
            for role in (req['subject_role'], req['target_role']):
                if not role: continue
                tokens = set(re.findall(r'[a-z0-9]+', role.lower()))
                for cap, synonyms in {'conveyor': {'conveyor', 'transport', 'belt', 'roller'},
                    'sensor': {'sensor', 'detector', 'detection'}, 'emitter': {'emitter', 'feed', 'supply'},
                    'button': {'button', 'control', 'switch'}}.items():
                    if tokens & synonyms: capabilities.add(cap)
                for c in components:
                    if role.casefold() in c['prefab'].casefold() or c['prefab'].casefold() in role.casefold(): prefabs.add(c['prefab'])
            # Deterministic official retrieval, never model-invented geometry or a scenario preset.
            query = ' '.join([req['source_text'], req['subject_role'] or '', req['target_role'] or ''])[:512]
            for hit in knowledge_base().search(query, limit=8):
                for card in hit['components']: prefabs.update(card['prefabs'])
        chosen = []
        for row in components:
            if row['prefab'] in prefabs or capabilities.intersection(row['measured_capabilities']):
                chosen.append({**deepcopy(row), 'measurement_status': 'measured' if row.get('bounds_local_metres') else 'unmeasured',
                    'component_geometry_local':deepcopy(self.catalog.get('native_geometry',{}).get('prefabs',{}).get(row['prefab'],{}).get('component_geometry_local',{})),
                    'geometry_basis': 'current native prefab receipt', 'knowledge_basis': 'official indexed documentation'})
        for cap in hard_capabilities:
            if not any(cap in c['measured_capabilities'] for c in chosen): unresolved.append({'capability': cap, 'reason': 'No current measured candidate matches the required capability.'})
        explicit = {r['arguments']['prefab'] for r in rows if r['arguments'].get('prefab') and
                    not (r['predicate'] in ('count','exists') and (r['arguments'].get('equals')==0 or r['arguments'].get('max')==0))}
        for prefab in explicit:
            if not any(c['prefab'] == prefab for c in chosen): unresolved.append({'prefab': prefab, 'reason': 'Explicit prefab is absent from the available template catalog.'})
        return {'components': chosen, 'missing_capabilities': unresolved,
            'completeness': 'required explicit prefabs and inferred operator capabilities retained; role ambiguity is reported, not downgraded',
            'catalog_expansion_required': not chosen or bool(unresolved)}

    def design(self, original, components):
        selection = self.candidates(components)
        bindings={}
        for r in hard_static(self.spec):
            if r['predicate']=='count' and r['arguments'].get('equals',r['arguments'].get('min',0))==0:continue
            for role in (r['subject_role'],r['target_role']):
                if role:bindings.setdefault(role,[]).append(r['requirement_id'])
        return {'phase': 'scene_design', 'scene_stage': 'design', 'original_requirement': original, 'requirement_spec': self.spec,
            'required_role_bindings':[{'role':role,'requirement_ids':ids} for role,ids in sorted(bindings.items())],
            'candidate_components': selection['components'], 'candidate_coverage': {k: v for k, v in selection.items() if k != 'components'},
            'placement_capabilities': self.placements,
            'measured_layout_capabilities': {'coordinate_system': self.catalog.get('native_geometry', {}).get('coordinate_system'),
                'world_bounds': self.catalog.get('native_geometry', {}).get('world_bounds'), 'source': 'current native receipt'},
            'relevant_component_guidance': knowledge_base().references([c['prefab'] for c in selection['components']], 'scene'),
            'output_schema_version': 1, 'output_shape': DESIGN_SHAPE,
            'context_manifest': self.manifest('design', {'requirement_spec': 'independent source quotes',
                'required_role_bindings':'role names compiled from applicable independent static requirements',
                'candidate_components': 'role/capability-filtered current catalog', 'placement_capabilities': 'implemented solver interface',
                'relevant_component_guidance': 'official documentation'})}

    def visual(self, original, design, checks, images):
        by_id = {r['requirement_id']: r for r in self.spec['requirements']}; rows = []
        for c in checks['checks']:
            if 'visual' not in c['evidence_methods']: continue
            requirements = [by_id[i] for i in c['requirement_ids']]
            rows.append({**{k: c[k] for k in ('check_id', 'requirement_ids', 'operator', 'object_ids', 'relation_ids')},
                'question': 'Does the visible layout satisfy: ' + ' / '.join(r['source_text'] for r in requirements) + '?',
                'expected_relation': {'predicate': c['operator'], 'parameters': c['arguments'],
                    'source_text': [r['source_text'] for r in requirements]}, 'preferred_views': ['top', 'iso_a', 'iso_b']})
        return {'phase': 'scene_visual', 'original_requirement': original, 'requirement_spec': self.spec,
            'actual_objects': [{k: o[k] for k in ('id', 'prefab', 'roles', 'function')} for o in design['objects']],
            'check_plan': {'checks': rows, 'coverage': checks['coverage'],
                'acceptance_scope': 'visible static relations only; no runtime/electrical response proof'},
            'image_manifest': images, 'supported_views': ['top', 'iso_a', 'iso_b'],
            'relevant_component_guidance': knowledge_base().references([o['prefab'] for o in design['objects']], 'vision'),
            'context_manifest': self.manifest('visual', {'requirement_spec': 'independent source quotes',
                'actual_objects': 'current manifest identities plus design identification labels', 'check_plan': 'independently compiled checks',
                'image_manifest': 'current load and capture receipts'}, [o['id'] for o in design['objects']])}

    def repair(self, design, plan, ticket, verification, visual, components,loaded_measurements=(),image_manifest=()):
        affected = set(ticket['affected_object_ids']); scope = set(affected)
        relevant = [r for r in self.spec['requirements'] if r['requirement_id'] in ticket['requirement_ids']]
        relations = [r for r in design['relations'] if r['subject_id'] in affected or r['target_id'] in affected or
                     set(r['requirement_ids']) & set(ticket['requirement_ids'])]
        for row in relations: scope.update((row['subject_id'], row['target_id']))
        if 'add' in ticket['allowed_operations']:
            for row in verification['checks']:
                if row['check_id'] in ticket['failed_check_ids']:
                    scope.update(i for i in row['object_ids'] if i in ticket['frozen_object_ids'])
        for obj in design['objects']:
            if obj['id'] in affected and obj['placement'].get('relative_to'): scope.add(obj['placement']['relative_to'])
        local = [o for o in design['objects'] if o['id'] in scope]
        measured={o['id']:o for o in loaded_measurements}
        selected = self.candidates(components, relevant, [o['prefab'] for o in local])
        return {'phase': 'scene_repair', 'scene_stage': 'local_repair', 'repair_tickets': [ticket],
            'relevant_requirements': relevant, 'current_objects': local,
            'current_layout_subset': [o for o in plan['objects'] if o['id'] in scope], 'related_relations': relations,
            'native_measurements': {o['id']: {'prefab': o['prefab'], 'instance_source': 'current load receipt' if o['id'] in measured else 'not supplied',
                'instance':deepcopy(measured.get(o['id'])), 'prefab_source':'current native prefab receipt',
                'prefab_local_geometry': self.catalog.get('native_geometry', {}).get('prefabs', {}).get(o['prefab'])} for o in local},
            'measured_layout_capabilities': {'coordinate_system':self.catalog.get('native_geometry',{}).get('coordinate_system'),
                'world_bounds':self.catalog.get('native_geometry',{}).get('world_bounds'),'source':'current native receipt'},
            'allowed_object_ids': ticket['affected_object_ids'], 'supported_patch_operations': ticket['allowed_operations'],
            'placement_capabilities': self.placements, 'candidate_components': selected['components'],
            'candidate_coverage': {k: v for k, v in selected.items() if k != 'components'},
            'relevant_component_guidance': knowledge_base().references([o['prefab'] for o in local], 'scene'),
            'evidence_refs': {'ids': ticket['evidence_ids'], 'image_manifest':list(image_manifest), 'checks': [r for r in verification['checks'] if r['check_id'] in ticket['failed_check_ids']],
                'visual_checks': [r for r in visual['check_results'] if r['check_id'] in ticket['failed_check_ids']],
                'global_findings': [f for f in visual['global_review']['findings'] if set(f['requirement_ids']) & set(ticket['requirement_ids'])]},
            'output_schema_version': 1, 'patch_shape': {'reason': 'justification', 'changes': [{'object_id': 'allowed ID', 'placement': {'kind': 'supported kind'}}],
                'add_objects': [], 'remove_object_ids': [], 'relation_updates': [], 'remove_relation_ids': [],
                'relation_impacts':[{'relation_id':'affected relation ID','expected_effect':'expected consequence, not a validation claim'}],'unresolved': []},
            'context_manifest': self.manifest('repair', {'relevant_requirements': 'violated independent requirements',
                'current_layout_subset': 'current resolved positions, including frozen anchors', 'native_measurements': 'current native receipt',
                'evidence_refs': 'current validated checks and visual observations', 'repair_tickets': 'verified violations'}, sorted(scope))}
