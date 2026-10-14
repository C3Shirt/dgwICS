"""Role-specific, allowlisted prompt contexts with traceable source identities."""
import re
import math
from copy import deepcopy

from .contracts import canonical_hash
from .component_kb import knowledge_base
from .scene_spec import hard_static, DESIGN_SHAPE

PROMPT_VERSION = 'issue4-inspection-issues-v4'


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

    def visual(self, original, design, checks, images, plan=None, native_receipt=None, *, spatial_context=True):
        from .scene_visual import ISSUES_CONTRACT
        spec_rows={r['requirement_id']:r for r in self.spec['requirements']}
        tasks=[];relations=[];equipment=[]
        for r in hard_static(self.spec):
            if r['predicate'] in ('count','exists'):
                equipment.append({'requirement_id':r['requirement_id'],'role':r['subject_role'],
                                  'selection_and_quantity':r['arguments'],'source_requirement':r['source_text']})
        for c in checks['checks']:
            if not c['requirement_ids']:continue
            source=[spec_rows[i]['source_text'] for i in c['requirement_ids']]
            expected={'check_id':c['check_id'],'subject_ids':c.get('subject_ids',c['object_ids']),
                'reference_ids':c.get('target_ids',[]),'description':' / '.join(source),
                'predicate':c['operator'],'parameters':c['arguments']}
            relations.append(expected)
            tasks.append({'check_id':c['check_id'],'subject_ids':expected['subject_ids'],
                'reference_ids':expected['reference_ids'],'focus':'Compare visible equipment and functional regions with: '+' / '.join(source),
                'preferred_views':['top','iso_a','iso_b']})
        receipt=native_receipt or {};native=receipt.get('native_geometry',{})
        identities={o['key']:o['id'] for o in receipt.get('manifest_objects',[])}
        measured={identities.get(o.get('key'),o.get('id')):o for o in native.get('objects',[])}
        resolved={o['id']:o for o in (plan or {}).get('objects',[])}
        profiles=[];function_paragraphs=[]
        for obj in design['objects']:
            refs=knowledge_base().references([obj['prefab']],'vision');card=refs['components'][0] if refs['components'] else None
            linked=[r for r in relations if obj['id'] in r['subject_ids']+r['reference_ids']]
            m=measured.get(obj['id']);pose=None
            if spatial_context and m:
                rotation=m.get('rotation');yaw=None
                if isinstance(rotation,list) and len(rotation)==4:
                    x,y,z,w=rotation;yaw=round(math.degrees(math.atan2(2*(w*y+x*z),1-2*(y*y+z*z)))%360,6)
                pose={'position_grid':m.get('position_grid'),'yaw_deg':yaw,'rotation_quaternion':rotation,
                      'fine_position':m.get('fine_position'),'fine_rotation':m.get('fine_rotation'),
                      'source':'current native load receipt; actual state under inspection'}
            profile={'id':obj['id'],'prefab':m.get('prefab') if m else obj['prefab'],
                'roles':obj['roles'],'task_role_source':'role bindings from design linked to independent requirements',
                'function':card['function'] if card else 'Official equipment function is not indexed.',
                'function_basis':card['function_basis'] if card else 'missing_official_knowledge',
                'visual_identity':{'appearance':card.get('appearance') if card else None,
                    'distinctive_features':card.get('markers',[]) if card else [],
                    'sources':card['sources'] if card else [],
                    'identity_limit':'Same-prefab objects require labels or multi-view spatial evidence, not appearance alone.'},
                'scene_file_configuration':deepcopy(resolved.get(obj['id'],{}).get('settings',[])),
                'configuration_source':'settings in confirmed loaded scene file; not independently read runtime configuration',
                'native_presence':'confirmed' if m and m.get('native_registered') is True else 'not confirmed',
                'actual_pose':pose,
                'native_spatial_state':{'bounds_world_metres':m.get('bounds_world'),'component_geometry_world':m.get('component_geometry',{}),
                    'source':'current native load receipt','limitation':'Outer bounds and pivots are not proof of an active functional region.'} if spatial_context and m else None,
                'expected_relations':linked}
            if spatial_context and m and not m.get('component_geometry'):
                profile['native_spatial_state']['missing_measurements']=['Separate active functional-region geometry is not provided by this receipt.']
            carrier=(m or {}).get('component_geometry',{}).get('conveyor')
            if spatial_context and carrier:
                direction=carrier.get('direction',[]);bounds=carrier.get('surface_bounds')
                axis=next((j for j in (0,2) if len(direction)==3 and abs(abs(direction[j])-1)<1e-6 and
                           all(abs(direction[k])<1e-6 for k in range(3) if k!=j)),None)
                frame={'flow_direction_world':direction,'source':'current native conveyor direction; auxiliary identification context',
                       'longitudinal_axis':('X','Y','Z')[axis] if axis is not None else None,
                       'lateral_axis':('Z' if axis==0 else 'X') if axis is not None else None}
                if axis is not None and bounds:
                    frame.update(upstream_coordinate_metres=bounds['min' if direction[axis]>0 else 'max'][axis],
                                 downstream_coordinate_metres=bounds['max' if direction[axis]>0 else 'min'][axis],
                                 interpretation='Separation on the longitudinal axis may be the intended upstream/downstream relation. Lateral alignment is evaluated on the lateral axis.')
                profile['native_spatial_state']['transport_frame']=frame
            profiles.append(profile)
            if card:function_paragraphs.append(obj['prefab']+' equipment: '+card['function'])
        intent=self.spec.get('scene_intent')
        description=(intent['process_description'] if intent else
            'The independent user request describes this intended industrial arrangement: '+original+
            ' Its explicit equipment and spatial objectives are listed below; no unspecified process stage is assumed.')
        description+=' Equipment principles relevant to interpreting this scene: '+' '.join(dict.fromkeys(function_paragraphs))
        description+=' Required collaboration and spatial objectives: '+' '.join(dict.fromkeys(r['description'] for r in relations))
        description+=' This is an intended process and static arrangement, not proven material motion or PLC execution. Inspect functional regions in relation to support surfaces and paths, not merely whether housings appear nearby.'
        return {'phase':'scene_visual','scene_overview':{'goal':intent['goal'] if intent else original,
            'process_description':description,'process_stages':intent['process_stages'] if intent else [],
            'required_equipment':equipment,'acceptance_scope':'static_layout_only; runtime requirements are deferred',
            'background_basis':'independent requirements and sourced device knowledge; narrative adds no mandatory constraints'},
            'coordinate_system':{k:native.get('coordinate_system',{}).get(k) for k in ('axis_order','vertical_axis','horizontal_axes','grid_metres','pivot_world_formula')} if spatial_context else None,
            'objects':profiles,'expected_relations':relations,'inspection_tasks':tasks,
            'images':[{'image_id':i['id'],'view_id':i.get('view_id')} for i in images],
            'supported_views':['top','iso_a','iso_b'],'output_contract':ISSUES_CONTRACT,
            'inspection_mode':'geometry_assisted' if spatial_context else 'visual_only'}

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
