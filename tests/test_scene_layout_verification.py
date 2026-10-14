"""Synthetic contracts and adapter integration, explicitly not live visual acceptance."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import Mock

from factoryio_agents.contracts import ContractError, canonical_hash, file_hash, write_json, read_json
from factoryio_agents.runtime import utc_now
from factoryio_agents.scene_spec import validate_requirement_spec, validate_requirement_bindings, validate_scene_design
from factoryio_agents.scene_verifier import default_registry, compile_check_plan, evaluate_plan, fuse_results, repair_ticket, project_scene
from factoryio_agents.scene_context import PromptContextBuilder
from factoryio_agents.scene_layout import apply_ticket_patch, PLACEMENTS
from factoryio_agents.scene_visual import validate_visual, image_context
import test_simple_framework as fixture
from test_simple_framework import DESIGN


def requirement(text, predicate='count', role='transport', arguments=None, target=None, key='R1', phase='static_scene'):
    return {'requirement_id':key,'source_text':text,'origin':'explicit_user','phase':phase,'subject_role':role,
        'target_role':target,'predicate':predicate,'arguments':arguments if arguments is not None else {'equals':1},'required':True,'evidence_methods':[]}


def spec(text, rows=None):
    return {'schema_version':1,'original_requirement':text,'requirements':rows or [requirement(text)]}


def visual(checks, image_ids, status='satisfied', requested=None):
    return {'global_review':{'status':'satisfied','summary':'Synthetic fixture only.','image_ids':image_ids,'findings':[]},
        'check_results':[{'check_id':c['check_id'],'status':status,'object_ids':c['object_ids'],
            'relation_ids':c['relation_ids'],'image_ids':image_ids,'observed':'Synthetic relation observation.',
            'reason':'Compared with independent fixture requirement.','repair_intent':'Adjust the affected relation.' if status=='violated' else '',
            'requested_view':requested} for c in checks['checks'] if 'visual' in c['evidence_methods']]}


class SceneLayoutVerificationTests(unittest.TestCase):
    def setUp(self):
        fixture.SimpleFrameworkTests.setUp(self)
        self.config.pop('scene_verification')
        self.design=deepcopy(DESIGN);self.design['objects'][0]['roles']=['transport'];self.design['objects'][1]['roles']=['feed']
        self.design['relations']=[{'id':'feed_relation','subject_id':'emitter','target_id':'conveyor','predicate':'feeds_into','parameters':{},'requirement_ids':['R2']}]
        self.registry=default_registry()
        self.text='One conveyor. Feed above the carrier.'
        self.spec=spec(self.text,[requirement('One conveyor.'),requirement('Feed above the carrier.','feeds_into','feed',{},'transport','R2')])

    def plan(self, design=None):
        from factoryio_agents.simple import design_to_plan
        return design_to_plan(design or self.design,self.catalog)

    def evaluate(self, design=None, requirements=None, plan=None):
        d=design or self.design;s=requirements or self.spec;p=plan or self.plan(d)
        checks=compile_check_plan(s,d,self.registry)
        return checks,evaluate_plan(checks,d,p,self.catalog,self.registry)

    def fake_tools(self):
        tools=Mock();tools.catalog.return_value=deepcopy(self.catalog)
        def render(plan,catalog,directory):
            from factoryio_agents.scene import build_scene
            from factoryio_agents.geometry_tools import loaded_geometry
            built=build_scene(plan,catalog,directory/'scene.factoryio',self.config['protocol'],auto_io_mappings=True)
            write_json(directory/'manifest.json',built['manifest'])
            measurements=project_scene(plan,catalog);keys={o['id']:o['key'] for o in built['manifest']['objects']}
            receipt={'request_id':'load-'+directory.name,'software_load_confirmed':True,'scene_path':str((directory/'scene.factoryio').resolve()),
                'scene_sha256':built['manifest']['scene_sha256'],'confirmed_at':utc_now(),
                'native_geometry':{'objects':[{**m,'key':keys[i],'native_registered':True} for i,m in measurements.items()]}}
            write_json(directory/'load-receipt.json',receipt)
            evidence={'versions':{'scene':built['manifest']['scene_sha256']},'records':[],'reconciliation':{'passed':True}}
            paths=[]
            for view in ('top','iso_a','iso_b'):
                path=directory/'visual'/('frame-'+view+'.png');path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'synthetic image '+view.encode()+directory.name.encode())
                row={'id':'image-'+view,'kind':'image','path':str(path.resolve()),'sha256':file_hash(path),'camera_view':view}
                evidence['records'].append(row);paths.append(path)
                write_json(path.with_name(path.stem+'.bridge.json'),{'image_sha256':row['sha256'],
                    'camera_receipt':{'ok':True,'action':'camera_fit','data':{'camera_fit':{'view':view}}},
                    'bridge_response':{'request_id':'capture-'+view,'completed_at':utc_now(),'data':{'scene_path':receipt['scene_path'],'image_sha256':row['sha256']}}})
            geometry=loaded_geometry(receipt,built['manifest'],check_connections=False)
            write_json(directory/'geometry-validation.json',geometry);write_json(directory/'visual/evidence.json',evidence)
            return {**built,'evidence':evidence,'images':paths,'geometry':geometry}
        tools.render.side_effect=render
        return tools

    def test_requirement_source_coverage_and_assumption_authority(self):
        validate_requirement_spec(self.spec,self.text)
        missing=spec(self.text,[requirement('One conveyor.')])
        with self.assertRaisesRegex(ContractError,'omits'):validate_requirement_spec(missing,self.text)
        changed=deepcopy(self.spec);changed['requirements'][0]['origin']='design_assumption'
        with self.assertRaisesRegex(ContractError,'assumptions'):validate_requirement_spec(changed,self.text)
        changed=deepcopy(self.spec);changed['requirements'][0]['origin']='confirmed_engineering'
        with self.assertRaisesRegex(ContractError,'cannot confirm'):validate_requirement_spec(changed,self.text)

    def test_counts_one_two_five_and_non_chain_topology(self):
        for count in (1,2,5):
            d=deepcopy(self.design);d['objects']=[deepcopy(d['objects'][0]) for _ in range(count)];d['relations']=[]
            for i,o in enumerate(d['objects']):o.update(id='belt_'+str(i),placement={'kind':'absolute','position':[40+i*12,4,60+i*48],'yaw_deg':0})
            s=spec(str(count)+' independent carriers',[requirement(str(count)+' independent carriers',arguments={'equals':count,'capability':'conveyor'})])
            checks,rows=self.evaluate(d,s)
            self.assertEqual(rows[0]['status'],'satisfied');self.assertEqual(len(checks['coverage']['R1']),1)

    def test_no_emitter_no_conveyor_and_missing_required_object(self):
        d=deepcopy(self.design);d['objects']=[d['objects'][1]];d['relations']=[]
        d['objects'][0]['placement']={'kind':'absolute','position':[100,4,100],'yaw_deg':0}
        s=spec('A feed fixture only',[requirement('A feed fixture only',role='feed')])
        _,rows=self.evaluate(d,s);self.assertEqual(rows[0]['status'],'satisfied')
        _,rows=self.evaluate(d,spec('Required conveyor'));self.assertEqual(rows[0]['status'],'violated')

    def test_same_prefab_roles_do_not_get_crossed(self):
        d=deepcopy(self.design);d['objects']=[d['objects'][0],deepcopy(d['objects'][0])];d['objects'][1].update(id='other',roles=['other_station'])
        d['objects'][1]['placement']={'kind':'absolute','position':[150,4,150],'yaw_deg':90};d['relations']=[]
        checks,rows=self.evaluate(d,spec('First station only'));self.assertEqual(checks['checks'][0]['object_ids'],['conveyor'])
        self.assertEqual(rows[0]['status'],'satisfied')

    def test_geometry_counterexample_and_unsupported_cannot_be_voted_away(self):
        p=self.plan();p['objects'][1]['position'][0]+=30;checks,rows=self.evaluate(plan=p)
        rows[-1]['status']='satisfied'
        review=visual(checks,['top'])
        fused=fuse_results(checks,rows,review,registration_passed=True)
        self.assertEqual(fused['checks'][1]['status'],'violated')
        rows[1]['status']='unsupported';self.assertEqual(fuse_results(checks,rows,review,registration_passed=True)['checks'][1]['status'],'unsupported')
        ticket=repair_ticket(checks,fused,review,self.design)
        self.assertEqual(ticket['affected_object_ids'],['emitter']);self.assertEqual(ticket['frozen_object_ids'],['conveyor'])

    def test_unknown_tolerance_and_unobservable_are_preserved(self):
        checks,rows=self.evaluate();rows[1].update(status='unknown',reason='Required transfer tolerance missing.')
        rows[-1]['status']='satisfied'
        self.assertEqual(fuse_results(checks,rows,visual(checks,['top']),registration_passed=True)['checks'][1]['status'],'unknown')
        rows[1]['status']='satisfied'
        self.assertEqual(fuse_results(checks,rows,visual(checks,['top'],'unobservable'),registration_passed=True)['status'],'unknown')

    def test_operator_registry_rejects_code_unknown_arguments_and_fake_capability(self):
        for predicate in ('eval(__import__("os"))','unknown_mesh_collision'):
            checks,rows=self.evaluate(requirements=spec('Unknown capability',[requirement('Unknown capability',predicate)]))
            self.assertEqual(checks['checks'][0]['operator'],'unsupported');self.assertEqual(rows[0]['status'],'unsupported')
        with self.assertRaisesRegex(ContractError,'unsupported arguments'):
            self.evaluate(requirements=spec('A conveyor',[requirement('A conveyor',arguments={'equals':1,'python':'evil'})]))
        with self.assertRaisesRegex(ContractError,'nonfinite|numeric|nonnegative'):
            self.evaluate(requirements=spec('A spacing',[requirement('A spacing','distance_range','feed',{'max_metres':float('nan')},'transport')]))

    def test_local_repair_preserves_anchor_and_requires_evidence_scope(self):
        p=self.plan();p['objects'][1]['position'][0]+=30;checks,rows=self.evaluate(plan=p);rows[-1]['status']='satisfied'
        v=visual(checks,['top']);fused=fuse_results(checks,rows,v,registration_passed=True);ticket=repair_ticket(checks,fused,v,self.design)
        patch={'changes':[{'object_id':'emitter','placement':{'kind':'emitter_over_conveyor','relative_to':'conveyor','along_fraction':.37}}]}
        repaired,_=apply_ticket_patch(p,self.design,patch,ticket,self.spec,self.catalog)
        self.assertEqual(repaired['objects'][0],p['objects'][0]);self.assertNotEqual(repaired['objects'][1],p['objects'][1])
        with self.assertRaises(ContractError):apply_ticket_patch(p,self.design,{'changes':[{'object_id':'conveyor','position':[0,0,0]}]},ticket,self.spec,self.catalog)

    def test_visual_contract_requires_every_check_and_existing_views(self):
        checks,_=self.evaluate();v=visual(checks,['top']);manifest=[{'id':'top'}]
        validate_visual(v,checks,self.spec,self.design,manifest)
        v['check_results']=[]
        with self.assertRaisesRegex(ContractError,'omitted'):validate_visual(v,checks,self.spec,self.design,manifest)
        v=visual(checks,['top'],'unobservable',requested='free_camera')
        with self.assertRaisesRegex(ContractError,'unsupported requested view'):validate_visual(v,checks,self.spec,self.design,manifest)

    def test_contexts_are_scoped_and_visual_has_no_generated_coordinate_leak(self):
        from factoryio_agents.scene_layout import catalog_context
        builder=PromptContextBuilder(self.catalog,self.spec,PLACEMENTS);checks,rows=self.evaluate()
        payload=builder.visual(self.text,self.design,checks,[{'id':'top','view_id':'top'}])
        self.assertNotIn('placement',str(payload['actual_objects']));self.assertNotIn('expected_position',str(payload))
        self.assertNotIn('native_geometry',payload);self.assertIn('question',payload['check_plan']['checks'][0])
        p=self.plan();p['objects'][1]['position'][0]+=30;checks,rows=self.evaluate(plan=p);rows[-1]['status']='satisfied'
        v=visual(checks,['top']);fused=fuse_results(checks,rows,v,registration_passed=True);ticket=repair_ticket(checks,fused,v,self.design)
        repair=builder.repair(self.design,p,ticket,fused,v,catalog_context(self.catalog))
        self.assertEqual(repair['allowed_object_ids'],['emitter']);self.assertEqual({o['id'] for o in repair['current_layout_subset']},{'emitter','conveyor'})
        self.assertNotIn('original_requirement',repair);self.assertNotIn('current_design',repair)
        self.assertEqual(repair['context_manifest']['prompt_version'],'issue4-guided-context-v3')

    def test_scene_only_loop_uses_independent_oracle_and_verified_capture_version(self):
        from factoryio_agents.simple import SimpleWorkflow
        checks,_=self.evaluate();client=Mock();client.complete.side_effect=[deepcopy(self.design),visual(checks,['image-top','image-iso_a','image-iso_b'])]
        tools=self.fake_tools();w=SimpleWorkflow(self.config,self.root/'run',client=client,tools=tools,requirement_spec=self.spec)
        r=w.run(self.text);self.assertEqual(r['status'],'scene_verified',r)
        self.assertFalse(r['plc_executed']);self.assertFalse(r['live_pipeline_passed'])
        payload=client.complete.call_args_list[-1].args[2];self.assertIn('image_manifest',payload);self.assertIn('actual_objects',payload)
        self.assertTrue((w.output/'scene-000/verification.json').is_file())
        self.assertEqual(read_json(w.output/'requirement-spec.json')['original_requirement'],self.text)

    def test_real_loop_patch_reloads_and_freezes_unaffected_objects(self):
        from factoryio_agents.simple import SimpleWorkflow
        checks,_=self.evaluate();good=visual(checks,['image-top','image-iso_a','image-iso_b'])
        bad=visual(checks,['image-top','image-iso_a','image-iso_b'],'violated')
        patch={'changes':[{'object_id':'emitter','placement':{'kind':'emitter_over_conveyor','relative_to':'conveyor','along_fraction':.3}}]}
        client=Mock();client.complete.side_effect=[deepcopy(self.design),bad,patch,good];tools=self.fake_tools()
        w=SimpleWorkflow(self.config,self.root/'repair',client=client,tools=tools,requirement_spec=self.spec,inject_fault='emitter_gap')
        r=w.run(self.text);self.assertEqual(r['status'],'scene_verified',r);self.assertEqual(tools.render.call_count,2)
        first,second=[c.args[0] for c in tools.render.call_args_list]
        self.assertEqual(first['objects'][0],second['objects'][0])
        self.assertNotEqual(read_json(w.output/'scene-000/image-index.json')[0]['scene_sha256'],read_json(w.output/'scene-001/image-index.json')[0]['scene_sha256'])

    def test_capture_hash_mutation_and_old_scene_are_rejected(self):
        tools=self.fake_tools();directory=self.root/'capture';directory.mkdir()
        rendered=tools.render(self.plan(),self.catalog,directory);image_context(rendered,directory)
        rendered['images'][0].write_bytes(b'changed')
        with self.assertRaisesRegex(ContractError,'hash'):image_context(rendered,directory)
        rendered=tools.render(self.plan(),self.catalog,self.root/'capture2')
        rendered['evidence']['versions']['scene']='old'
        with self.assertRaisesRegex(ContractError,'stale'):image_context(rendered,self.root/'capture2')

    def test_online_requirement_extraction_is_separate_from_design(self):
        from factoryio_agents.simple import SimpleWorkflow
        checks,_=self.evaluate();client=Mock();client.complete.side_effect=[deepcopy(self.spec),deepcopy(self.design),visual(checks,['image-top','image-iso_a','image-iso_b'])]
        w=SimpleWorkflow(self.config,self.root/'online',client=client,tools=self.fake_tools())
        r=w.run(self.text);self.assertEqual(r['status'],'scene_verified',r)
        self.assertEqual([c.args[2]['phase'] for c in client.complete.call_args_list],['scene_requirements','scene_design','scene_visual'])

    def test_new_path_does_not_enable_plc_by_request_text(self):
        from factoryio_agents.simple import SimpleWorkflow
        with self.assertRaisesRegex(ContractError,'scene-only'):SimpleWorkflow(self.config,self.root/'plc',with_plc=True,scene_verification='relations')

    def test_scoring_does_not_treat_unknown_as_success(self):
        from factoryio_agents.scene_experiments import score_cases
        r=score_cases([{'all_check_ids':['a','b'],'expected_violated_check_ids':['a'],'observed_violated_check_ids':['b']}])
        self.assertEqual(r['error_detection_recall'],0);self.assertEqual(r['false_positive_rate'],1);self.assertIsNone(r['repair_success'])

    def test_universal_empty_selection_is_not_accepted(self):
        s=spec('Every fixture is within the room',[requirement('Every fixture is within the room','forall','absent_role',{'operator':'within_bounds','arguments':{}})])
        _,rows=self.evaluate(requirements=s);self.assertEqual(rows[0]['status'],'not_applicable')

    def test_unobservable_stops_without_silent_pass_or_infinite_retake(self):
        from factoryio_agents.simple import SimpleWorkflow
        checks,_=self.evaluate();client=Mock();client.complete.side_effect=[deepcopy(self.design),visual(checks,['image-top','image-iso_a','image-iso_b'],'unobservable')]
        w=SimpleWorkflow(self.config,self.root/'unknown',client=client,tools=self.fake_tools(),requirement_spec=self.spec,max_view_retries=0)
        r=w.run(self.text);self.assertEqual(r['status'],'needs_attention');self.assertEqual(r['target'],'vision')

    def test_resume_reuses_independent_requirements_and_design(self):
        from factoryio_agents.simple import SimpleWorkflow
        checks,_=self.evaluate();good=visual(checks,['image-top','image-iso_a','image-iso_b']);first=Mock();first.complete.side_effect=[deepcopy(self.design),good]
        w=SimpleWorkflow(self.config,self.root/'source',client=first,tools=self.fake_tools(),requirement_spec=self.spec);self.assertEqual(w.run(self.text)['status'],'scene_verified')
        client=Mock();client.complete.return_value=good
        resumed=SimpleWorkflow(self.config,self.root/'resumed',client=client,tools=self.fake_tools())
        r=resumed.resume(w.output);self.assertEqual(r['status'],'scene_verified',r);self.assertEqual(client.complete.call_count,1)
        self.assertEqual(client.complete.call_args.args[0],'vision')

    def test_requirement_selector_bindings_and_quoted_context(self):
        s=deepcopy(self.spec);s['requirements'][0]['arguments']={'equals':1,'prefab':'conveyor'}
        with self.assertRaisesRegex(ContractError,'exact prefab'):validate_requirement_bindings(s,self.catalog,self.registry)
        s['requirements'][0]['arguments']={'equals':1,'capability':'conveyor'}
        validate_requirement_bindings(s,self.catalog,self.registry)
        s['requirements'][0]['arguments']['prefab']=None
        with self.assertRaisesRegex(ContractError,'Omit unused'):validate_requirement_bindings(s,self.catalog,self.registry)
        s=deepcopy(self.spec);s['requirements'][1]['source_text']='Feed is above conveyor'
        with self.assertRaisesRegex(ContractError,'R2.*literal substring'):validate_requirement_spec(s,self.text)
        s=spec('One conveyor. Static only.',[requirement('One conveyor.')]);s['context_fragments']=[{'source_text':'Static only.','reason':'Execution scope.'}]
        validate_requirement_spec(s,s['original_requirement'])

    def test_negative_exists_and_unknown_capability_in_visual_mode(self):
        s=spec('No remover',[requirement('No remover','exists',None,{'equals':0,'prefab':'Remover'})])
        _,rows=self.evaluate(requirements=s);self.assertEqual(rows[0]['status'],'satisfied')
        checks,_=self.evaluate()
        catalog=deepcopy(self.catalog);catalog['native_geometry']['prefabs']['Emitter']['component_geometry_local'].pop('emitter')
        rows=evaluate_plan(checks,self.design,self.plan(),catalog,self.registry,geometry_enabled=False)
        self.assertEqual(rows[1]['status'],'unsupported')

    def test_visual_relation_links_and_minimal_ticket_from_findings(self):
        s=deepcopy(self.spec);s['requirements'][1].update(predicate='visual_relation',arguments={'question':'Is the feeder above the carrier?'})
        checks,rows=self.evaluate(requirements=s);self.assertEqual(checks['checks'][1]['relation_ids'],['feed_relation'])
        v=visual(checks,['top'],'violated')
        v['global_review']['findings']=[{'status':'violated','object_ids':['emitter'],'requirement_ids':['R2'],
            'observed':'Emitter misses carrier.','reason':'Required receiving footprint absent.','repair_intent':'Move emitter.','image_ids':['top']}]
        rows[-1]['status']='satisfied';fused=fuse_results(checks,rows,v,registration_passed=True)
        ticket=repair_ticket(checks,fused,v,self.design)
        self.assertEqual(ticket['affected_object_ids'],['emitter']);self.assertEqual(ticket['frozen_object_ids'],['conveyor'])

    def test_schema_fallback_is_bounded_restored_and_keeps_validation(self):
        from types import SimpleNamespace
        from factoryio_agents.llm import ModelClient,ModelServiceError,response_format
        from factoryio_agents.scene_layout import checked_call
        schema={'type':'object'};client=ModelClient({'base_url':'http://unused','response_schemas':{'vision':schema},'schema_roles':['vision'],'json_mode':False})
        observed=[]
        def call(role,prompt,payload,images):
            observed.append(client.config['response_schemas'].get('vision'))
            if len(observed)==1:raise ContractError('vision model response must be object')
            self.assertEqual(response_format(client.config,'vision',payload),{'type':'json_object'})
            self.assertEqual(payload['response_schema'],schema);return {'accepted':True}
        w=SimpleNamespace(raw_model=client,call=call,trace=Mock())
        out=checked_call(w,'vision','JSON inspection',{'phase':'scene_visual'},lambda v:v,[])
        self.assertTrue(out['accepted']);self.assertEqual(observed,[schema,None]);self.assertEqual(client.config['response_schemas']['vision'],schema)
        self.assertEqual(client.config['schema_roles'],['vision']);self.assertFalse(client.config['json_mode'])
        w.call=Mock(side_effect=ModelServiceError('vision model HTTP 503'))
        with self.assertRaises(ModelServiceError):checked_call(w,'vision','JSON inspection',{},lambda v:v,[])
        self.assertEqual(w.call.call_count,1)

    def test_partial_resume_reuses_valid_requirements_only(self):
        from factoryio_agents.simple import SimpleWorkflow
        checks,_=self.evaluate();good=visual(checks,['image-top','image-iso_a','image-iso_b'])
        client=Mock();client.complete.side_effect=[deepcopy(self.spec),{'unresolved':['Unavailable configuration']}]
        first=SimpleWorkflow(self.config,self.root/'partial',client=client,tools=self.fake_tools())
        self.assertEqual(first.run(self.text)['status'],'needs_attention');self.assertTrue((first.output/'requirement-spec.json').is_file())
        next_client=Mock();next_client.complete.side_effect=[deepcopy(self.design),good]
        resumed=SimpleWorkflow(self.config,self.root/'continued',client=next_client,tools=self.fake_tools())
        self.assertEqual(resumed.resume(first.output)['status'],'scene_verified')
        self.assertEqual([c.args[2]['phase'] for c in next_client.complete.call_args_list],['scene_design','scene_visual'])

    def test_partial_resume_rejects_invalid_saved_binding_without_replacing_source(self):
        from factoryio_agents.simple import SimpleWorkflow
        checks,_=self.evaluate();good=visual(checks,['image-top','image-iso_a','image-iso_b'])
        client=Mock();client.complete.side_effect=[deepcopy(self.spec),{'unresolved':['Unavailable configuration']}]
        first=SimpleWorkflow(self.config,self.root/'old_partial',client=client,tools=self.fake_tools());first.run(self.text)
        old=deepcopy(self.spec);old['requirements'][0]['arguments']={'equals':1,'prefab':'conveyor'}
        write_json(first.output/'requirement-spec.json',old);state=read_json(first.output/'run.json');state['requirements_sha256']=canonical_hash(old);write_json(first.output/'run.json',state)
        source_bytes=(first.output/'requirement-spec.json').read_bytes()
        next_client=Mock();next_client.complete.side_effect=[deepcopy(self.spec),deepcopy(self.design),good]
        resumed=SimpleWorkflow(self.config,self.root/'binding_recovery',client=next_client,tools=self.fake_tools())
        self.assertEqual(resumed.resume(first.output)['status'],'scene_verified')
        self.assertEqual((first.output/'requirement-spec.json').read_bytes(),source_bytes)
        self.assertEqual(read_json(resumed.output/'rejected-requirement-spec.json'),old)
        self.assertEqual(read_json(resumed.output/'requirements-recovery.json')['rejected_sha256'],canonical_hash(old))

    def test_comparison_reuses_captures_and_quarantines_training_candidates(self):
        from factoryio_agents.simple import SimpleWorkflow
        from factoryio_agents.scene_benchmark import compare_captures
        checks,_=self.evaluate();good=visual(checks,['image-top','image-iso_a','image-iso_b'])
        first_client=Mock();first_client.complete.side_effect=[deepcopy(self.design),good]
        source=SimpleWorkflow(self.config,self.root/'comparison_source',client=first_client,tools=self.fake_tools(),requirement_spec=self.spec)
        self.assertEqual(source.run(self.text)['status'],'scene_verified')
        write_json(source.output/'catalog.json',self.catalog)
        baseline={'verdict':'pass','summary':'Synthetic baseline','findings':[]}
        client=Mock();client.complete.side_effect=[baseline,baseline,good];tools=self.fake_tools()
        comparison=SimpleWorkflow(self.config,self.root/'comparison',client=client,tools=tools)
        result=compare_captures(comparison,source.output)
        self.assertEqual(result['status'],'visual_comparison_recorded');tools.render.assert_not_called();self.assertEqual(client.complete.call_count,3)
        self.assertIsNone(result['accuracy_metrics']);self.assertEqual(comparison.training.source_kind,'visual_benchmark')

    def test_missing_object_patch_uses_count_selector_and_preserves_anchor(self):
        d=deepcopy(self.design);d['objects']=[d['objects'][0]];d['relations']=[]
        s=spec('Two conveyors',[requirement('Two conveyors',role=None,arguments={'equals':2,'capability':'conveyor'})])
        p=self.plan(d);checks,rows=self.evaluate(d,s,p);rows[-1]['status']='satisfied'
        v=visual(checks,['top']);fused=fuse_results(checks,rows,v,registration_passed=True)
        ticket=repair_ticket(checks,fused,v,d)
        self.assertEqual(ticket['affected_object_ids'],[]);self.assertEqual(ticket['frozen_object_ids'],['conveyor'])
        self.assertEqual(ticket['allowed_operations'],['add'])
        from factoryio_agents.scene_layout import catalog_context
        payload=PromptContextBuilder(self.catalog,s,PLACEMENTS).repair(d,p,ticket,fused,v,catalog_context(self.catalog))
        self.assertEqual([o['id'] for o in payload['current_layout_subset']],['conveyor'])
        self.assertEqual(payload['allowed_object_ids'],[])
        obj=deepcopy(d['objects'][0]);obj.update(id='added',roles=['new_transport'],placement={'kind':'absolute','position':[120,4,160],'yaw_deg':0})
        fixed,updated=apply_ticket_patch(p,d,{'add_objects':[obj]},ticket,s,self.catalog)
        self.assertEqual(fixed['objects'][0],p['objects'][0]);self.assertEqual(len(updated['objects']),2)
        wrong=deepcopy(self.design['objects'][1]);wrong['placement']={'kind':'absolute','position':[120,4,160],'yaw_deg':0}
        with self.assertRaisesRegex(ContractError,'matching count selector'):apply_ticket_patch(p,d,{'add_objects':[wrong]},ticket,s,self.catalog)

    def test_capture_attachment_order_must_match_view_manifest(self):
        tools=self.fake_tools();directory=self.root/'wrong_order';directory.mkdir()
        rendered=tools.render(self.plan(),self.catalog,directory)
        rendered['images'].reverse()
        with self.assertRaisesRegex(ContractError,'order'):image_context(rendered,directory)
