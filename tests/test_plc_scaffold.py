"""Linkage/attribution checks; these mocked programs do not prove plant behavior."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from factoryio_agents.agents import PLCAgent
from factoryio_agents.contracts import ContractError, write_json
from factoryio_agents.diagnostics import summarize_run
from factoryio_agents.plc_scaffold import io_scaffold, assemble_program
from factoryio_agents.plc import check_assignment_targets
from factoryio_agents.response_schema import review_schema
from factoryio_agents.training import TrainingRecorder, RecordingClient


class PLCScaffoldTests(unittest.TestCase):
    def setUp(self):
        self.contract = {'schema_version':1,'driver':'ModbusTCPClient','transport_confirmed':True,
            'tags':[{'key':'sensor','id':'entry/tag/0','object_id':'entry','kind':'input','data_type':'bool'},
                    {'key':'motor','id':'belt/tag/0','object_id':'belt','kind':'output','data_type':'bool'}],
            'mappings':[{'tag_key':'sensor'},{'tag_key':'motor'}],
            'bindings':[{'tag_key':'sensor','variable':'Application.SceneIO.I_entry','plc_type':'BOOL'},
                        {'tag_key':'motor','variable':'Application.SceneIO.Q_belt','plc_type':'BOOL'}]}
        self.config = {'program_name':'PLC_PRG','application_name':'Application',
            'bindings':deepcopy(self.contract['bindings']), 'verified_device_bindings':[
                {**b,'bit_size':'1'} for b in self.contract['bindings']]}
        self.fragment = {'schema_version':1,'handoff_id':'mock','program_name':'PLC_PRG',
            'declaration':'PROGRAM PLC_PRG\nVAR\n enabled : BOOL;\nEND_VAR',
            'implementation':'enabled := SceneIO.I_entry;\nSceneIO.Q_belt := enabled;',
            'unresolved':[],'notes':['mock linkage only']}

    def test_tool_declarations_match_verified_bindings_without_changing_logic(self):
        scaffold = io_scaffold(self.config,self.contract)
        program = assemble_program(self.fragment,scaffold,self.contract)
        self.assertEqual(program['implementation'],self.fragment['implementation'])
        self.assertEqual(program['bindings'],self.config['bindings'])
        self.assertEqual(program['globals'][0]['name'],'SceneIO')
        self.assertIn('I_entry : BOOL;',program['globals'][0]['declaration'])
        self.assertNotIn('globals',self.fragment)

    def test_undeclared_assignment_is_retried_before_real_compilation(self):
        invalid = deepcopy(self.fragment)
        invalid['implementation']='missing := SceneIO.I_entry;\nSceneIO.Q_belt := missing;'
        client=Mock(); client.complete.side_effect=[invalid,self.fragment]
        result=PLCAgent(client,tool_io=True).program({},self.contract,binding_config=self.config)
        self.assertEqual(result['implementation'],self.fragment['implementation'])
        self.assertIn('undeclared assignment targets: missing',client.complete.call_args.args[2]['stage_rejection']['error'])

    def test_truncated_provider_output_is_not_retried_with_identical_budget(self):
        from factoryio_agents.llm import ModelOutputError
        client=Mock();client.complete.side_effect=ModelOutputError('plc output truncated')
        with self.assertRaises(ModelOutputError):
            PLCAgent(client,tool_io=True).program({},self.contract,binding_config=self.config)
        self.assertEqual(client.complete.call_count,1)

    def test_assignment_check_ignores_literals_comments_and_timer_named_parameters(self):
        program=assemble_program(self.fragment,io_scaffold(self.config,self.contract),self.contract)
        program['declaration']='PROGRAM PLC_PRG\nVAR\n enabled : BOOL;\n delay : TON;\nEND_VAR'
        program['implementation'] += '\ndelay(\n IN := enabled,\n PT := T#2S);\n(* nonexistent := TRUE; *)'
        check_assignment_targets(program)
        program['implementation'] += '\nIF enabled THEN undeclared := FALSE; END_IF;'
        with self.assertRaisesRegex(ContractError,'undeclared assignment targets: undeclared'):
            check_assignment_targets(program)

    def test_mismatched_verified_variable_is_a_tool_configuration_failure(self):
        self.config['verified_device_bindings'][0]['variable'] = 'Application.Other.Signal'
        with self.assertRaisesRegex(ContractError,'not a verified'): io_scaffold(self.config,self.contract)

    def test_optional_inputs_can_be_unused(self):
        self.fragment['implementation'] = '(* SceneIO.I_entry *)\nSceneIO.Q_belt := enabled;'
        program=assemble_program(self.fragment,io_scaffold(self.config,self.contract),self.contract)
        self.assertEqual(program['implementation'],self.fragment['implementation'])

    def test_output_usage_is_not_restricted_to_direct_assignment(self):
        self.fragment['implementation'] = 'enabled := SceneIO.I_entry AND SceneIO.Q_belt;'
        self.assertEqual(assemble_program(self.fragment,io_scaffold(self.config,self.contract),self.contract)['implementation'],self.fragment['implementation'])

    def test_model_cannot_replace_tool_io_or_write_physical_input(self):
        value = {**self.fragment,'globals':[]}
        with self.assertRaisesRegex(ContractError,'tools own'): assemble_program(value,io_scaffold(self.config,self.contract),self.contract)
        self.fragment['implementation'] += '\nSceneIO.I_entry := FALSE;'
        with self.assertRaisesRegex(ContractError,'writes a physical input'): assemble_program(self.fragment,io_scaffold(self.config,self.contract),self.contract)

    def test_function_block_outputs_cannot_write_physical_inputs(self):
        self.fragment['declaration']='PROGRAM PLC_PRG\nVAR\n delay : TON;\nEND_VAR'
        self.fragment['implementation']='delay(IN := TRUE, PT := T#1S, Q => SceneIO.I_entry);'
        with self.assertRaisesRegex(ContractError,'writes a physical input'):
            assemble_program(self.fragment,io_scaffold(self.config,self.contract),self.contract)
        self.fragment['implementation']='delay(IN := SceneIO.I_entry, PT := T#1S, Q => SceneIO.Q_belt);'
        self.assertEqual(assemble_program(self.fragment,io_scaffold(self.config,self.contract),self.contract)['implementation'],self.fragment['implementation'])

    def test_invalid_logic_is_retried_with_exact_error_and_original_scaffold(self):
        invalid = deepcopy(self.fragment);invalid['implementation'] = 'SceneIO.I_entry := TRUE;'
        client = Mock();client.complete.side_effect = [invalid,self.fragment]
        result = PLCAgent(client,tool_io=True).program({},self.contract,binding_config=self.config)
        self.assertEqual(result['implementation'],self.fragment['implementation'])
        retry = client.complete.call_args.args[2]
        self.assertIn('writes a physical input',retry['stage_rejection']['error'])
        self.assertEqual(retry['io_scaffold']['bindings'],self.config['bindings'])

    def test_assembled_program_replays_candidate_and_detects_manual_logic_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);recorder=TrainingRecorder(root/'training-data',source_kind='injected_client')
            client=Mock();client.complete.return_value=self.fragment
            program=PLCAgent(RecordingClient(client,recorder),tool_io=True).program({},self.contract,binding_config=self.config)
            path=root/'scene-000/plc-000/program.json';write_json(path,program)
            report=summarize_run(root,{'status':'failed'})
            self.assertTrue(report['artifact_authorship'][0]['matches_recorded_candidate'])
            self.assertFalse(report['artifact_authorship'][0]['matches_live_provider_candidate'])
            self.assertEqual(report['artifact_authorship'][0]['assembly_mode'],'model_logic_with_tool_io')
            program['implementation'] += '\nSceneIO.Q_belt := FALSE;';write_json(path,program)
            self.assertTrue(summarize_run(root,{'status':'failed'})['manual_override_detected'])

    def test_visual_schema_limits_identity_and_process_to_this_handoff(self):
        schema=review_schema({'task':{'process':[{'id':'transport'}]},'manifest':{'objects':[{'id':'belt'}]},
            'io_contract':{'tags':[{'key':'motor'}]},'evidence':{'records':[{'id':'view-top'}]},
            'versions':{'scene':'hash','plc':None},'phase':'scene','handoff':{'id':'handoff'}})
        finding=schema['properties']['findings']['items']['properties']
        self.assertEqual(finding['object_ids']['items']['enum'],['belt'])
        self.assertEqual(finding['diagnosis']['properties']['process_step']['enum'],['scene_setup','transport'])
        self.assertEqual(schema['properties']['versions']['properties']['plc'],{'type':'null'})
        self.assertEqual(schema['properties']['verdict']['enum'],['pass','repair','inconclusive'])

    def test_verified_active_low_input_is_normalized_by_tool_not_model(self):
        self.config['normalize_bool_inputs']=True
        self.contract['component_semantics']=[{'tag_key':'sensor','app_version_confirmed':True,'evidence':['native-receipt'],
            'semantics':{'function':'operator_button','pressed_value':False,'released_value':True}}]
        self.fragment['implementation']='enabled := SceneInputs.I_entry;\nSceneIO.Q_belt := enabled;'
        scaffold=io_scaffold(self.config,self.contract)
        program=assemble_program(self.fragment,scaffold,self.contract)
        self.assertIn('SceneInputs.I_entry := NOT SceneIO.I_entry;',program['implementation'])
        self.assertTrue(program['implementation'].endswith(self.fragment['implementation']))
        self.assertEqual(scaffold['signals'][0]['normalized_meaning'],'pressed')
        self.assertEqual(program['bindings'],self.config['bindings'])

    def test_unknown_polarity_remains_raw_without_an_invented_alias(self):
        self.config['normalize_bool_inputs']=True
        scaffold=io_scaffold(self.config,self.contract)
        self.assertEqual(scaffold['normalization'],[])
        self.assertEqual(scaffold['signals'][0]['st_reference'],'SceneIO.I_entry')
        self.assertEqual(scaffold['signals'][0]['polarity'],'unconfirmed_raw_value')

    def test_normalized_logic_cannot_access_raw_input_or_write_normalized_copy(self):
        self.config['normalize_bool_inputs']=True
        self.contract['component_semantics']=[{'tag_key':'sensor','app_version_confirmed':True,'evidence':['native-receipt'],
            'semantics':{'occupied_value':True}}]
        scaffold=io_scaffold(self.config,self.contract)
        self.fragment['implementation']='enabled := SceneInputs.I_entry OR SceneIO.I_entry;\nSceneIO.Q_belt := enabled;'
        with self.assertRaisesRegex(ContractError,'reinterpret raw polarity'):assemble_program(self.fragment,scaffold,self.contract)
        self.fragment['implementation']='SceneInputs.I_entry := FALSE;\nSceneIO.Q_belt := TRUE;'
        with self.assertRaisesRegex(ContractError,'writes a physical input'):assemble_program(self.fragment,scaffold,self.contract)

    def test_blocker_review_is_bounded_and_cannot_erase_actual_missing_requirements(self):
        self.fragment['unresolved']=['A required actuator is unavailable']
        client=Mock();client.complete.return_value=self.fragment
        program=PLCAgent(client,tool_io=True).program({},self.contract,binding_config=self.config)
        self.assertEqual(client.complete.call_count,3)
        self.assertEqual(program['unresolved'],self.fragment['unresolved'])
        self.assertIn('Keep actual unsupported requirements unresolved',client.complete.call_args.args[2]['stage_rejection']['instruction'])


if __name__ == '__main__': unittest.main()
