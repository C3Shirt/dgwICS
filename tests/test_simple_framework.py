"""Minimal scene loop tests use injected images/tools, never claim native acceptance."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from factoryio_agents.contracts import ContractError, read_json, write_json


DESIGN={'scene_id':'simple_transfer','scene_goal':'Feed one box, convey it past two sensors and remove it.',
    'control_intent':['One box at a time. Stop and reset both systems together.'],
    'objects':[{'id':'conveyor','prefab':'RollerConveyor6M','function':'Transport boxes',
        'placement':{'kind':'anchor','position':[100,3,100],'yaw_deg':0}},
        {'id':'emitter','prefab':'Emitter','function':'Supply one box',
         'placement':{'kind':'emitter_over_conveyor','relative_to':'conveyor','along_fraction':.1}}]}


class SimpleFrameworkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.image=self.root/'frame.png';self.image.write_bytes(b'injected test image')
        from test_coordination import source_fixture
        from factoryio_agents.scene import build_catalog
        import xml.etree.ElementTree as ET
        xml=ET.fromstring(source_fixture());emitter=xml.find('Object/Emitter')
        ET.SubElement(emitter,'Parts',BoxS='True',BoxM='False');ET.SubElement(emitter,'Bases',Pallet='False')
        self.source=self.root/'source.factoryio';self.source.write_text(ET.tostring(xml,encoding='unicode'),encoding='utf-8')
        self.catalog=build_catalog([self.source])
        self.catalog['native_geometry']={'coordinate_system':{'axis_order':['X','Y','Z'],'vertical_axis':'Y','grid_metres':.125},
            'world_bounds':{'min':[0,0,0],'max':[59.625,9,35.625]},'prefabs':{
            'RollerConveyor6M':{'bounds_local_metres':{'min':[-.5,-.3,-3],'max':[.5,.3,3]},
                'component_geometry_local':{'conveyor':{'direction':[0,0,1],'surface_bounds':{'min':[-.5,-.3,-3],'max':[.5,.3,3]}}}},
            'Emitter':{'bounds_local_metres':{'min':[-.4,-.2,-.5],'max':[.4,.3,.5]},
                'component_geometry_local':{'emitter':{'origin':[0,-.125,0]}}}}}
        self.config={'llm':{},'protocol':{'name':'modbus_tcp','factoryio_role':'client','plc_role':'server','host':'127.0.0.1','port':502,'unit_id':1},
            'scene_sources':[str(self.source)],'factoryio':{},'training':{'enabled':True}}

    def module(self):
        from factoryio_agents import simple
        return simple

    def tools(self, geometries=None):
        tools=Mock();tools.catalog.return_value=deepcopy(self.catalog)
        results=iter(geometries or [{'passed':True,'spatial_checks_applied':True,'issues':[]}])
        def render(plan,catalog,directory):
            from factoryio_agents.scene import build_scene
            built=build_scene(plan,catalog,directory/'scene.factoryio',self.config['protocol'],auto_bool_mappings=True)
            evidence={'records':[{'id':'image-top','kind':'image','path':str(self.image)}],
                'reconciliation':{'passed':True}}
            geometry=next(results)
            write_json(directory/'manifest.json',built['manifest']);write_json(directory/'io-contract.json',built['io_contract'])
            write_json(directory/'geometry-validation.json',geometry);write_json(directory/'visual/evidence.json',evidence)
            return {**built,'geometry':geometry,'evidence':evidence,'images':[self.image]}
        tools.render.side_effect=render
        return tools

    def test_happy_path_uses_only_one_design_and_one_image_review(self):
        client=Mock();client.complete.side_effect=[deepcopy(DESIGN),{'verdict':'pass','summary':'Aligned','findings':[]}]
        workflow=self.module().SimpleWorkflow(self.config,self.root/'run',client=client,tools=self.tools())
        result=workflow.run('Make a single conveyor transfer scene')
        self.assertEqual(result['status'],'scene_verified')
        self.assertEqual([c.args[0] for c in client.complete.call_args_list],['scene','vision'])
        self.assertFalse(result['live_pipeline_passed']);self.assertFalse(result['plc_executed'])
        vision=client.complete.call_args.args[2]
        self.assertEqual(vision['requirement'],'Make a single conveyor transfer scene')
        self.assertNotIn('handoff',vision);self.assertNotIn('position',str(vision['objects']))
        self.assertNotIn('native_geometry',vision);self.assertNotIn('measurement_checks',vision)
        self.assertEqual(len(list((workflow.output/'training-data/candidates').glob('*.json'))),2)

    def test_native_failure_cannot_be_overridden_by_a_visual_pass(self):
        client=Mock();client.complete.side_effect=[deepcopy(DESIGN),{'verdict':'pass','summary':'Looks plausible','findings':[]}]
        tools=self.tools([{'passed':False,'spatial_checks_applied':False,'issues':[],'scope':'missing_conveyor_geometry'}])
        result=self.module().SimpleWorkflow(self.config,self.root/'blocked',client=client,tools=tools).run('Make a conveyor')
        self.assertEqual(result['status'],'needs_attention');self.assertEqual(result['target'],'tools')
        self.assertEqual(client.complete.call_count,2)

    def test_local_patch_preserves_every_unaffected_object(self):
        module=self.module();plan=module.design_to_plan(DESIGN,self.catalog)
        patch={'changes':[{'object_id':'emitter','position':[230,9,135]}]}
        changed=module.apply_local_patch(plan,patch,{'emitter'},self.catalog)
        self.assertEqual(changed['objects'][0],plan['objects'][0])
        self.assertNotEqual(changed['objects'][1]['position'],plan['objects'][1]['position'])
        with self.assertRaises(ContractError):module.apply_local_patch(plan,patch,{'conveyor'},self.catalog)

    def test_repair_loop_calls_only_scene_patch_then_image_review(self):
        client=Mock();client.complete.side_effect=[deepcopy(DESIGN),
            {'verdict':'repair','summary':'Feed needs moving','findings':[{'object_ids':['emitter'],'status':'issue',
                'observed':'Feed is detached','reason':'The item cannot reach the belt','suggestion':'Move over belt','image_ids':['image-top']}]},
            {'changes':[{'object_id':'emitter','position':[238,9,130]}]},
            {'verdict':'pass','summary':'Connected','findings':[]}]
        result=self.module().SimpleWorkflow(self.config,self.root/'repair',client=client,tools=self.tools([
            {'passed':True,'spatial_checks_applied':True,'issues':[]},
            {'passed':True,'spatial_checks_applied':True,'issues':[]}])).run('Make a conveyor')
        self.assertEqual(result['status'],'scene_verified')
        self.assertEqual([c.args[0] for c in client.complete.call_args_list],['scene','vision','scene','vision'])
        self.assertEqual(client.complete.call_args_list[2].args[2]['allowed_object_ids'],['emitter'])

    def test_visual_issues_need_object_identity_and_current_image_evidence(self):
        bad={'verdict':'repair','summary':'Wrong','findings':[{'object_ids':['ghost'],'status':'issue','observed':'Wrong',
            'reason':'Wrong','suggestion':'Move','image_ids':['old-image']}]}
        with self.assertRaises(ContractError):self.module().validate_review(bad,{'emitter'},{'image-top'})

    def test_invalid_patch_is_corrected_once_by_the_scene_agent_without_replanning(self):
        client=Mock();client.complete.side_effect=[deepcopy(DESIGN),
            {'verdict':'repair','summary':'Feed gap','findings':[{'object_ids':['emitter'],'status':'issue',
                'observed':'Feed detached','reason':'Box misses belt','suggestion':'Move above belt','image_ids':['image-top']}]},
            {'changes':[{'object_id':'emitter','placement':{'kind':'emitter_over_conveyor','relative_to':'conveyor','along_fraction':0}}]},
            {'changes':[{'object_id':'emitter','placement':{'kind':'emitter_over_conveyor','relative_to':'conveyor','along_fraction':.2}}]},
            {'verdict':'pass','summary':'Feed connected','findings':[]}]
        result=self.module().SimpleWorkflow(self.config,self.root/'correction',client=client,tools=self.tools([
            {'passed':True,'spatial_checks_applied':True,'issues':[]},
            {'passed':True,'spatial_checks_applied':True,'issues':[]}])).run('Make a conveyor')
        self.assertEqual(result['status'],'scene_verified')
        self.assertEqual([c.args[0] for c in client.complete.call_args_list],['scene','vision','scene','scene','vision'])
        self.assertIn('strictly between',client.complete.call_args_list[3].args[2]['format_error'])

    def test_unidentified_issue_stops_for_attention_instead_of_allowing_an_unsafe_patch(self):
        value={'verdict':'repair','summary':'A required button cannot be identified','findings':[{
            'object_ids':[],'status':'issue','observed':'No readable button label',
            'reason':'Cannot establish the identity','suggestion':'Capture a close view','image_ids':['image-top']}]}
        normalized=self.module().validate_review(value,{'emitter'},{'image-top'})
        self.assertEqual(normalized['verdict'],'uncertain')
        self.assertEqual(normalized['findings'][0]['status'],'uncertain')
        self.assertEqual(value['findings'][0]['status'],'issue')

    def test_json_prompts_meet_the_provider_json_object_requirement(self):
        from factoryio_agents.simple_prompts import DESIGN_PROMPT,LAYOUT_PROMPT,PATCH_PROMPT
        for prompt in (DESIGN_PROMPT,LAYOUT_PROMPT,PATCH_PROMPT):self.assertIn('json',prompt.lower())

    def test_resume_reuses_design_and_plan_but_takes_new_images(self):
        client=Mock();client.complete.side_effect=[deepcopy(DESIGN),{'verdict':'uncertain','summary':'Need another view',
            'findings':[{'object_ids':['emitter'],'status':'uncertain','observed':'Occluded','reason':'Cannot see contact',
                'suggestion':'Capture again','image_ids':['image-top']}]}]
        first=self.module().SimpleWorkflow(self.config,self.root/'original',client=client,tools=self.tools())
        first.run('Make a conveyor')
        before=read_json(first.output/'scene-plan.json')
        resumed_client=Mock();resumed_client.complete.return_value={'verdict':'pass','summary':'Visible','findings':[]}
        tools=self.tools();resumed=self.module().SimpleWorkflow(self.config,self.root/'resumed',client=resumed_client,tools=tools)
        result=resumed.resume(first.output)
        self.assertEqual(result['status'],'scene_verified');self.assertEqual(result['model_calls'],1)
        self.assertEqual(resumed_client.complete.call_args.args[0],'vision')
        tools.render.assert_called_once()
        self.assertEqual(read_json(first.output/'scene-plan.json'),before)

    def test_resume_refuses_tampered_layout_before_new_model_work(self):
        client=Mock();client.complete.side_effect=[deepcopy(DESIGN),{'verdict':'pass','summary':'Visible','findings':[]}]
        first=self.module().SimpleWorkflow(self.config,self.root/'original',client=client,tools=self.tools());first.run('Make a conveyor')
        plan=read_json(first.output/'scene-plan.json');plan['objects'][0]['position'][0]+=1;write_json(first.output/'scene-plan.json',plan)
        new=Mock();result=self.module().SimpleWorkflow(self.config,self.root/'resumed',client=new,tools=self.tools()).resume(first.output)
        self.assertEqual(result['status'],'needs_attention');new.complete.assert_not_called()

    def test_absolute_ancillary_objects_can_mix_with_relative_transport_placement(self):
        design=deepcopy(DESIGN);design['objects'][1]['placement']={'kind':'absolute','position':[100,8,80],'yaw_deg':0}
        plan=self.module().design_to_plan(design,self.catalog)
        self.assertEqual(len(plan['objects']),2)
        self.assertEqual(plan['objects'][1]['yaw_deg'],0)

    def test_plc_continuation_reuses_verified_layout_and_never_calls_scene_models(self):
        from unittest.mock import patch
        client=Mock();client.complete.side_effect=[deepcopy(DESIGN),{'verdict':'pass','summary':'Visible','findings':[]}]
        first=self.module().SimpleWorkflow(self.config,self.root/'original',client=client,tools=self.tools());first.run('Make a conveyor')
        new_client=Mock()
        resumed=self.module().SimpleWorkflow(self.config,self.root/'continued',client=new_client,tools=self.tools(),with_plc=True,start_from='plc')
        with patch('factoryio_agents.simple_plc.SimplePLCStage') as stage:
            stage.return_value.run.return_value={'status':'passed','plc_executed':True}
            result=resumed.resume(first.output)
        self.assertEqual(result['status'],'passed');new_client.complete.assert_not_called()
        stage.return_value.run.assert_called_once()
        self.assertEqual(read_json(resumed.output/'scene-000/scene-feedback.json')['evidence_scope'],'archived unchanged layout')

    def test_plc_continuation_rechecks_current_native_geometry_before_model_generation(self):
        from unittest.mock import patch
        client=Mock();client.complete.side_effect=[deepcopy(DESIGN),{'verdict':'pass','summary':'Visible','findings':[]}]
        first=self.module().SimpleWorkflow(self.config,self.root/'original',client=client,tools=self.tools());first.run('Make a conveyor')
        new_client=Mock();tools=self.tools([{'passed':False,'spatial_checks_applied':True,'issues':[{'object_id':'emitter'}]}])
        resumed=self.module().SimpleWorkflow(self.config,self.root/'continued',client=new_client,tools=tools,with_plc=True,start_from='plc')
        with patch('factoryio_agents.simple_plc.SimplePLCStage') as stage:result=resumed.resume(first.output)
        self.assertEqual(result['status'],'needs_attention');stage.assert_not_called();new_client.complete.assert_not_called()
