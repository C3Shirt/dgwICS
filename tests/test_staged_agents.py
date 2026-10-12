"""Subtask checks use mocked replies only; they do not assert live software success."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock

from factoryio_agents.agents import PlannerAgent, SceneAgent, VisionAgent, manifest_for_review, complete_role
from factoryio_agents.contracts import ContractError, file_hash, canonical_hash, write_json, read_json, validate_task
from factoryio_agents.coordination import make_handoff
from factoryio_agents.prompts import PROMPTS
from factoryio_agents.training import RecordingClient, TrainingRecorder
from factoryio_agents.llm import ModelServiceError
from factoryio_agents.diagnostics import summarize_run
from factoryio_agents.scene import build_catalog, build_scene, SceneError
from test_scene import FIXTURE
from test_coordination import source_fixture, task_fixture


class StagedAgentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        source = self.root / 'source.factoryio'; source.write_text(FIXTURE, encoding='utf-8')
        self.catalog = build_catalog([source])
        t = self.catalog['templates'][0]
        self.task = {'task_id': 'mock', 'objects': [{'id':'sensor', 'prefab':t['prefab']}], 'process':[]}
        self.layout = {'scene_id':'mock', 'base_source_id':t['source_id'], 'objects':[
            {'id':'sensor', 'template_id':t['template_id'], 'position':[1,2,3], 'yaw_deg':90}]}

    def test_layout_then_configuration_receive_only_relevant_templates(self):
        configured = deepcopy(self.layout); configured['objects'][0]['tag_names'] = {'0':'entry'}
        client = Mock(); client.complete.side_effect = [self.layout, configured]
        returned = SceneAgent(client, staged=True).plan(self.task, self.catalog)
        self.assertEqual(returned, configured)
        layout, config = [c.args[2] for c in client.complete.call_args_list]
        self.assertEqual(layout['scene_stage'], 'layout')
        self.assertNotIn('component_context', layout['catalog']['templates'][0])
        self.assertEqual(config['approved_layout'], self.layout)
        self.assertEqual(config['scene_stage'], 'configuration_io')
        self.assertTrue(all(t['prefab']==self.task['objects'][0]['prefab'] for t in config['catalog']['templates']))

    def test_configuration_cannot_move_a_previously_approved_object(self):
        moved = deepcopy(self.layout); moved['objects'][0]['position'][0] += 1
        client = Mock(); client.complete.side_effect = [self.layout, moved, self.layout]
        result = SceneAgent(client, staged=True).plan(self.task, self.catalog)
        self.assertEqual(result, self.layout)
        retry = client.complete.call_args.args[2]
        self.assertIn('changed approved layout', retry['stage_rejection']['error'])

    def test_reviewed_reference_is_retained_in_both_realization_subtasks(self):
        from test_layout_design import proposal_fixture
        from factoryio_agents.layout_design import compose_approved_layout
        task={**self.task,'requirement':'Detect a box at the material path.',
              'objects':[{**self.task['objects'][0],'purpose':'detect boxes'}]}
        approved=compose_approved_layout(task,proposal_fixture(task),
            {'verdict':'approved','summary':'Suitable detector placement.','findings':[]})
        client=Mock(); client.complete.side_effect=[self.layout,self.layout]
        SceneAgent(client,staged=True).plan(task,self.catalog,layout_reference=approved)
        for call in client.complete.call_args_list:
            self.assertEqual(call.args[2]['layout_reference'],approved)

    def test_invalid_layout_is_rejected_before_component_stage(self):
        bad = deepcopy(self.layout); bad['objects'][0]['yaw_deg']=45
        client = Mock(); client.complete.return_value = bad
        with self.assertRaises(ContractError): SceneAgent(client, staged=True).plan(self.task, self.catalog)
        self.assertEqual(client.complete.call_count, 3)
        self.assertTrue(all(c.args[2]['scene_stage']=='layout' for c in client.complete.call_args_list))

    def test_setting_repair_reuses_layout_without_another_geometry_call(self):
        client = Mock(); client.complete.return_value = self.layout
        SceneAgent(client, staged=True).plan(self.task, self.catalog, previous=self.layout,
            feedback={'phase':'scene_contract','diagnostics':{'error':'Unsafe or unsupported setting'}})
        self.assertEqual(client.complete.call_count,1)
        self.assertEqual(client.complete.call_args.args[2]['scene_stage'],'configuration_io')

    def test_invalid_attribute_is_retried_inside_configuration_subtask(self):
        invalid=deepcopy(self.layout); invalid['objects'][0]['settings']=[
            {'path':'Sensor','attribute':'UseForcedValue','value':False}]
        client=Mock(); client.complete.side_effect=[self.layout, invalid, self.layout]
        SceneAgent(client, staged=True).plan(self.task,self.catalog)
        self.assertEqual([c.args[2]['scene_stage'] for c in client.complete.call_args_list],
                         ['layout','configuration_io','configuration_io'])
        self.assertIn('unsupported setting',client.complete.call_args.args[2]['stage_rejection']['error'])

    def test_review_view_keeps_signal_identity_geometry_and_component_settings(self):
        obj = deepcopy(self.catalog['templates'][0]); obj.update(id='sensor', key='native-key')
        manifest = {'scene_id':'mock', 'objects':[obj]}; original = deepcopy(manifest)
        view = manifest_for_review(manifest)
        self.assertEqual(manifest, original)
        self.assertNotIn('configuration', view['objects'][0])
        self.assertEqual(view['objects'][0]['tags'], obj['tags'])
        self.assertEqual(view['objects'][0]['position'], obj['position'])
        self.assertIn('review_view', view)

    def test_automatic_mapping_is_deterministic_and_drops_inherited_system_addresses(self):
        t = self.catalog['templates'][1]
        plan = deepcopy(self.layout)
        plan['objects'].append({'id':'belt','template_id':t['template_id'],'position':[3,2,1],'yaw_deg':0})
        original = deepcopy(plan)
        a = build_scene(plan, self.catalog, self.root/'a.factoryio', auto_bool_mappings=True)
        plan['objects'].reverse()
        b = build_scene(plan, self.catalog, self.root/'b.factoryio', auto_bool_mappings=True)
        mapping_a = {m['tag_key']:m['channel'] for m in a['io_contract']['mappings']}
        self.assertEqual(mapping_a, {m['tag_key']:m['channel'] for m in b['io_contract']['mappings']})
        self.assertEqual(set(mapping_a.values()), {'BitInput0','BitInput1','BitOutput0'})
        self.assertEqual(a['io_contract']['channel_counts']['BitInputCount'], 2)
        self.assertEqual(a['io_contract']['channel_counts']['BitOutputCount'], 1)
        self.assertFalse(a['manifest']['mapping_allocation']['device_binding_verified'])
        self.assertEqual(original['objects'], list(reversed(plan['objects'])))

    def test_automatic_mapping_does_not_silently_correct_model_addresses(self):
        plan = deepcopy(self.layout); plan['mappings']=[{'channel':'BitOutput0','object_id':'sensor','tag_index':0}]
        with self.assertRaisesRegex(SceneError, 'omit model-authored'):
            build_scene(plan, self.catalog, self.root/'invalid.factoryio', auto_bool_mappings=True)

    def test_process_tool_releases_template_output_overrides_and_enforces_one_item(self):
        native=ET.fromstring(source_fixture()); native.find('Object/Emitter').set('UpTo','5')
        native.find('Object/OperatingMode/GroupIO/BinaryOutput').set('UseForcedValue','True')
        source=self.root/'emitter-source.factoryio'; source.write_bytes(ET.tostring(native))
        original=source.read_bytes(); catalog=build_catalog([source]); task=task_fixture()
        plan={'scene_id':'mock','base_source_id':catalog['sources'][0]['id'],'objects':[
            {'id':id_, 'template_id':t['template_id']} for id_,t in zip(('emitter','conveyor'),catalog['templates'])]}
        result=build_scene(plan,catalog,self.root/'process.factoryio',auto_bool_mappings=True,
                           execution_contract=task['execution_contract'])
        xml=ET.fromstring((self.root/'process.factoryio').read_bytes())
        self.assertEqual(xml.find('Object/Emitter').get('UpTo'),'1')
        self.assertTrue(all(n.get('UseForcedValue')=='False' for n in xml.findall('Object/OperatingMode/GroupIO/BinaryOutput')))
        self.assertEqual(source.read_bytes(),original)
        receipt=result['manifest']['process_constraint_application']
        self.assertFalse(receipt['physical_input_forcing'])
        self.assertTrue(any(c.get('previous')=='True' and c.get('value')=='False' for c in receipt['changes']))

    def test_vision_retries_bad_top_level_format_with_same_evidence(self):
        image = self.root/'image.png'; image.write_bytes(b'mock image')
        evidence = {'versions':{}, 'records':[{'id':'img','kind':'image','path':str(image),'sha256':file_hash(image)}]}
        client = Mock(); client.complete.side_effect = [ContractError('vision model response must be object'),
            {'checks':[]}]
        returned = VisionAgent(client).review(self.task, {'objects':[]}, {'tags':[]}, evidence, {}, 'scene')
        self.assertEqual(returned['verdict'], 'inconclusive')  # No approved reference or scene inventory in this routing fixture.
        self.assertIn('format_feedback', client.complete.call_args.args[2])
        self.assertEqual(client.complete.call_args.args[3], [image])

    def test_upstream_metadata_is_projected_after_original_version_checks(self):
        task={**self.task,'handoff_id':'upstream'}; original=deepcopy(task)
        path=self.root/'task.json'; write_json(path,task)
        handoff=make_handoff('orchestrator','scene','authoring',{'task':canonical_hash(task)},[path],'plan')
        client=Mock(); client.complete.return_value={'handoff_id':handoff['id']}
        complete_role(client,'scene',PROMPTS['scene'],{'task':task},handoff)
        wire=client.complete.call_args.args[2]
        self.assertNotIn('handoff_id',wire['task'])
        self.assertEqual(wire['handoff']['id'],handoff['id'])
        self.assertEqual(task,original)
        self.assertEqual(read_json(path),original)

    def test_three_planning_subtasks_have_verifiable_assembly_provenance(self):
        task=task_fixture(); task['handoff_id']='mock-current'; task['scenario_type']='conveyor'
        spec={k:v for k,v in task.items() if k not in ('acceptance','execution_contract')}
        spec['objects']=[{'id':t['prefab'].lower(),'purpose':'mock purpose','prefab':t['prefab']} for t in self.catalog['templates']]
        # This fixture's execution requires an Emitter, so use its dedicated catalog.
        source=self.root/'planning-source.factoryio'; source.write_text(source_fixture(),encoding='utf-8')
        catalog=build_catalog([source]); spec['objects']=task['objects']
        client=Mock(); client.complete.side_effect=[spec,{'handoff_id':'mock-current','execution_contract':task['execution_contract']},
            {'handoff_id':'mock-current','acceptance':task['acceptance'],'verification_tasks':[]}]
        recorder=TrainingRecorder(self.root/'training-data',source_kind='injected_client')
        result=PlannerAgent(RecordingClient(client,recorder),staged=True).plan(task['requirement'],catalog,task['protocol'])
        write_json(self.root/'task.json',result)
        receipt=read_json(self.root/'training-data/derived/task-assembly.json')
        self.assertEqual(len(receipt['sources']),3)
        self.assertEqual(receipt['artifact_sha256'],canonical_hash(result))
        report=summarize_run(self.root,{'status':'planning'})
        row=report['artifact_authorship'][0]
        self.assertTrue(row['matches_recorded_candidate'])
        self.assertEqual(row['assembly_mode'],'three_model_fragments')
        self.assertFalse(row['matches_live_provider_candidate'])
        result['control_rules'].append('manual change'); write_json(self.root/'task.json',result)
        self.assertTrue(summarize_run(self.root,{'status':'planning'})['manual_override_detected'])

    def test_spec_validation_does_not_remove_complete_task_acceptance_requirement(self):
        task=task_fixture(); task.pop('acceptance'); task.pop('execution_contract')
        validate_task(task,specification_only=True)
        with self.assertRaisesRegex(ContractError,'acceptance'): validate_task(task)

    def test_five_planning_fragments_preserve_model_ownership_and_fixed_namespace(self):
        task = task_fixture(); task.update(handoff_id='mock-current',scenario_type='mock')
        spec = {k:v for k,v in task.items() if k not in ('acceptance','execution_contract')}
        source = self.root/'five-source.factoryio'; source.write_text(source_fixture(),encoding='utf-8')
        catalog = build_catalog([source])
        signals = {'motor':{'object_id':'conveyor','kind':'output','index':0,'meaning':'raw'}}
        checks = [{'id':'cycle','process_ids':['feed','exit'],'kind':'sequence','steps':[
            {'signal':'motor','edge':'rising'},{'signal':'motor','edge':'falling'}],'min_matches':1}]
        client = Mock(); client.complete.side_effect = [spec,
            {'handoff_id':'mock-current','execution_contract':task['execution_contract']},
            {'handoff_id':'mock-current','signals':signals},{'handoff_id':'mock-current','probes':[]},
            {'handoff_id':'mock-current','checks':checks,'verification_tasks':[]}]
        recorder = TrainingRecorder(self.root/'training-data',source_kind='injected_client')
        result = PlannerAgent(RecordingClient(client,recorder),staged=True,staged_measurements=True).plan(
            task['requirement'],catalog,task['protocol'])
        write_json(self.root/'task.json',result)
        self.assertEqual(client.complete.call_args.args[2]['approved_signals'],signals)
        self.assertEqual(result['acceptance'][0]['checks'],checks)
        proof = read_json(self.root/'training-data/derived/task-assembly.json')
        self.assertEqual(len(proof['sources']),5)
        audit = summarize_run(self.root,{'status':'planning'})['artifact_authorship'][0]
        self.assertEqual(audit['assembly_mode'],'five_model_fragments')
        self.assertTrue(audit['matches_recorded_candidate'])
        self.assertFalse(audit['matches_live_provider_candidate'])
        result['acceptance'][0]['checks'][0]['min_matches']=5; write_json(self.root/'task.json',result)
        self.assertTrue(summarize_run(self.root,{'status':'planning'})['manual_override_detected'])

    def button_planning_parts(self):
        task = task_fixture()
        task.update(handoff_id='mock-current',scenario_type='mock')
        task['objects'].append({'id':'button','prefab':'StopPushButton','purpose':'operator stop'})
        spec = {k:v for k,v in task.items() if k not in ('acceptance','execution_contract')}
        source = self.root/'button-planning-source.factoryio'
        source.write_text(source_fixture(),encoding='utf-8')
        button_xml = ET.fromstring(FIXTURE)
        button_xml.find('Object/Proxy').set('PrefabName','StopPushButton')
        button_source = self.root/'button-source.factoryio'
        button_source.write_bytes(ET.tostring(button_xml))
        catalog = build_catalog([source,button_source])
        signals = {'motor':{'object_id':'conveyor','kind':'output','index':0,'meaning':'raw'},
                   'button_pressed':{'object_id':'button','kind':'input','index':0,'meaning':'pressed'}}
        probes = [{'id':'stop_test','stage':'after_capture','actions':[
            {'label':'press','object_id':'button','pressed':True,'hold_seconds':.8},
            {'label':'release','object_id':'button','pressed':False,'hold_seconds':.7}]}]
        checks = [{'id':'cycle','process_ids':['feed','exit'],'kind':'sequence','steps':[
            {'signal':'motor','edge':'rising'},{'signal':'motor','edge':'falling'}],'min_matches':1}]
        for label, pressed in [('press',True),('release',False)]:
            checks.append({'id':label,'process_ids':['exit'],'kind':'probe_state',
                'phase':'stop_test.'+label,'settle_seconds':.1,
                'expect':{'button_pressed':pressed,'motor':False},'min_samples':2})
        parts = [spec,{'handoff_id':'mock-current','execution_contract':task['execution_contract']},
                 {'handoff_id':'mock-current','signals':signals},
                 {'handoff_id':'mock-current','probes':probes},
                 {'handoff_id':'mock-current','checks':checks,'verification_tasks':[]}]
        return task, catalog, parts

    def test_planner_receives_probe_limits_and_repairs_only_the_rejected_subtask(self):
        task, catalog, parts = self.button_planning_parts()
        invalid = deepcopy(parts[3]); invalid['probes'][0]['actions'][1]['hold_seconds'] = .2
        client = Mock(); client.complete.side_effect = parts[:3]+[invalid]+parts[3:]
        result = PlannerAgent(client,staged=True,staged_measurements=True).plan(
            task['requirement'],catalog,task['protocol'])
        calls = [c.args[2] for c in client.complete.call_args_list]
        self.assertEqual([p['planning_stage'] for p in calls],
            ['spec','execution','measurement_signals','measurement_probes','measurement_probes','measurement_checks'])
        limits = calls[3]['native_probe_constraints']['hold_seconds']
        self.assertEqual((limits['minimum'],limits['maximum']),(.4,3))
        self.assertIn('release',limits['applies_to'])
        rejection = calls[4]['stage_rejection']
        self.assertIn('stop_test.release.hold_seconds=0.2',rejection['error'])
        self.assertIn('[0.4, 3]',rejection['error'])
        self.assertEqual(rejection['previous_response']['probes'][0]['actions'][1]['hold_seconds'],.2)
        self.assertEqual(result['acceptance'][0]['probes'],parts[3]['probes'])
        self.assertEqual(invalid['probes'][0]['actions'][1]['hold_seconds'],.2)

    def test_failed_probe_stage_preserves_the_three_accepted_planning_fragments(self):
        task, catalog, parts = self.button_planning_parts()
        recorder = TrainingRecorder(self.root/'training-data',source_kind='injected_client')
        first = Mock(); first.complete.side_effect = parts[:3]+[ModelServiceError('fixture interrupted')]
        with self.assertRaises(ModelServiceError):
            PlannerAgent(RecordingClient(first,recorder),staged=True,staged_measurements=True).plan(
                task['requirement'],catalog,task['protocol'])
        second = Mock(); second.complete.side_effect = parts[3:]
        result = PlannerAgent(RecordingClient(second,recorder),staged=True,staged_measurements=True).plan(
            task['requirement'],catalog,task['protocol'])
        self.assertEqual([c.args[2]['planning_stage'] for c in second.complete.call_args_list],
                         ['measurement_probes','measurement_checks'])
        receipt = read_json(self.root/'training-data/derived/task-assembly.json')
        self.assertEqual(len(receipt['sources']),5)
        self.assertEqual(receipt['artifact_sha256'],canonical_hash(result))


if __name__ == '__main__': unittest.main()
