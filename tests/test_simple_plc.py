"""Minimal PLC loop checks use synthetic observations, not live acceptance claims."""
from copy import deepcopy
import unittest


class SimplePLCTests(unittest.TestCase):
    def test_elevated_start_preserves_the_actual_helper_failure(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock, patch
        from factoryio_agents.simple_plc import start_service_with_uac
        from factoryio_agents.contracts import ContractError
        with TemporaryDirectory() as tmp:
            directory=Path(tmp)
            def launch(*args,**kwargs):
                (directory/'runtime-start-elevated.json').write_text(json.dumps({
                    'service_running':False,'status':'error','error':'Injected script policy failure'}))
                return Mock(pid=123,poll=Mock(return_value=2))
            with patch('subprocess.Popen',side_effect=launch):
                with self.assertRaisesRegex(ContractError,'Injected script policy failure'):
                    start_service_with_uac({},directory,Mock())

    def test_elevated_start_reports_launcher_exit_when_no_receipt_exists(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock, patch
        from factoryio_agents.simple_plc import start_service_with_uac
        from factoryio_agents.contracts import ContractError
        with TemporaryDirectory() as tmp:
            with patch('subprocess.Popen',return_value=Mock(pid=123,poll=Mock(return_value=5))):
                with self.assertRaisesRegex(ContractError,'launcher exit code 5'):
                    start_service_with_uac({},Path(tmp),Mock())

    def test_simple_model_fragment_uses_tool_owned_io_without_a_model_handoff(self):
        from factoryio_agents.simple_plc import assemble_logic
        from factoryio_agents.plc_scaffold import io_scaffold
        from factoryio_agents.contracts import ContractError
        contract={'schema_version':1,'driver':'ModbusTCPClient','transport_confirmed':True,
            'tags':[{'key':'i','id':'entry/tag/0','object_id':'entry','kind':'input','data_type':'bool'},
                    {'key':'q','id':'belt/tag/0','object_id':'belt','kind':'output','data_type':'bool'}],
            'mappings':[{'tag_key':'i'},{'tag_key':'q'}],
            'bindings':[{'tag_key':'i','variable':'Application.SceneIO.I','plc_type':'BOOL'},
                        {'tag_key':'q','variable':'Application.SceneIO.Q','plc_type':'BOOL'}]}
        config={'bindings':contract['bindings'],'verified_device_bindings':[{**b,'bit_size':'1'} for b in contract['bindings']]}
        scaffold=io_scaffold(config,contract)
        fragment={'declaration':'PROGRAM PLC_PRG\nVAR\nEND_VAR','implementation':'SceneIO.Q := SceneIO.I;',
            'unresolved':[],'notes':[]}
        assembled=assemble_logic(fragment,scaffold,contract)
        self.assertEqual(assembled['implementation'],fragment['implementation'])
        self.assertEqual(assembled['bindings'],contract['bindings'])
        self.assertEqual(fragment.keys(),{'declaration','implementation','unresolved','notes'})
        fragment['implementation']+='\nSceneIO.I := FALSE;'
        with self.assertRaisesRegex(ContractError,'writes a physical input'):assemble_logic(fragment,scaffold,contract)

    def test_offline_compile_never_deploys_or_measures_runtime(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import Mock
        from factoryio_agents.plc_scaffold import io_scaffold
        from factoryio_agents.simple_plc import SimplePLCStage
        contract={'schema_version':1,'driver':'ModbusTCPClient','transport_confirmed':True,
            'tags':[{'key':'q','id':'belt/tag/0','object_id':'belt','kind':'output','data_type':'bool'}],
            'mappings':[{'tag_key':'q'}],'bindings':[{'tag_key':'q','variable':'Application.SceneIO.Q','plc_type':'BOOL'}]}
        config={'bindings':contract['bindings'],'verified_device_bindings':[{**contract['bindings'][0],'bit_size':'1'}]}
        scaffold=io_scaffold(config,contract)
        with TemporaryDirectory() as tmp:
            root=Path(tmp);directory=root/'scene-000';directory.mkdir()
            w=SimpleNamespace(output=root,state={},max_plc_repairs=0,plc_mode='compile',trace=Mock(),
                training=SimpleNamespace(directory=root/'training-data',last_candidate_id='injected'))
            w.call=Mock(return_value={'declaration':'PROGRAM PLC_PRG\nVAR\nEND_VAR',
                'implementation':'SceneIO.Q := TRUE;','unresolved':[],'notes':['offline injected test']})
            def finish(status,**details):w.state.update(status=status,**details);return w.state
            w.finish=finish
            tools=Mock();tools.prepare.return_value=(config,scaffold,{'conveyor':scaffold['signals'][0]})
            tools.compile.return_value={'compiled':True,'started':False,'bindings_verified':True,
                'task_program_verified':True,'transport_verified':True}
            result=SimplePLCStage(w,tools).run('Test compile',{'objects':[{'id':'belt','prefab':'BeltConveyor4M','function':'Transport'}]},
                {'io_contract':contract,'manifest':{'scene_sha256':'injected'}},directory)
            self.assertEqual(result['status'],'plc_compiled');self.assertFalse(result['plc_executed'])
            self.assertFalse(result['live_pipeline_passed']);tools.measure.assert_not_called()
            self.assertEqual(w.call.call_count,1)


class GenericRuntimeTests(unittest.TestCase):
    def test_all_channels_are_observed_without_role_or_component_cardinality(self):
        from factoryio_agents.simple_runtime import runtime_signals
        design={'objects':[{'id':'a','function':'Independent axis'}, {'id':'b','function':'Independent axis'},
                           {'id':'vessel','function':'Store liquid'}]}
        signals=[{'tag_key':'a-drive','object_id':'a','kind':'output','data_type':'bool'},
                 {'tag_key':'a-reverse','object_id':'a','kind':'output','data_type':'bool'},
                 {'tag_key':'b-drive','object_id':'b','kind':'output','data_type':'bool'},
                 {'tag_key':'level','object_id':'vessel','kind':'input','data_type':'float'}]
        observed=runtime_signals(design,{'signals':signals})
        self.assertEqual(set(observed),{'a-drive','a-reverse','b-drive','level'})
        self.assertEqual(observed['level']['function'],'Store liquid')

    def test_observation_without_buttons_or_transport_is_allowed(self):
        from factoryio_agents.simple_runtime import validate_runtime_plan
        plan={'steps':[{'action':'observe','seconds':180}],
              'checks':[{'id':'level','description':'Valve closes when the requested liquid level is reached.'}]}
        self.assertEqual(validate_runtime_plan(plan,{},30),plan)

    def test_buttons_are_selected_by_explicit_identity_among_multiple_controls(self):
        from factoryio_agents.simple_runtime import validate_runtime_plan
        plan={'steps':[{'action':'press_button','object_id':'station_b_enable'},{'action':'observe'}],
              'checks':[{'id':'enable','description':'Only station B starts.'}]}
        actual=validate_runtime_plan(plan,{'station_a_enable':{},'station_b_enable':{}},45)
        self.assertEqual(actual['steps'][0]['object_id'],'station_b_enable')
        self.assertEqual(actual['steps'][1]['seconds'],45)

    def test_unknown_buttons_and_output_forcing_are_not_available_operator_actions(self):
        from factoryio_agents.simple_runtime import validate_runtime_plan
        from factoryio_agents.contracts import ContractError
        for step in ({'action':'press_button','object_id':'invented'}, {'action':'write_output','tag_key':'motor'}):
            with self.assertRaises(ContractError):
                validate_runtime_plan({'steps':[step,{'action':'observe'}],
                    'checks':[{'id':'task','description':'Original task'}]}, {},30)

    def row(self,i):
        return {'id':'sample-'+str(i),'kind':'runtime_sample','timestamp':str(i),'elapsed_seconds':i,'scope':'step-0',
                'values':{'motor':False,'level':i},'normalized_values':{},'item_count':4,
                'forced_signals':[],'connection_ok':True,'scene_running':True}

    def test_events_after_sixty_transitions_are_retained(self):
        from factoryio_agents.simple_runtime import summarize_evidence
        rows=[self.row(i) for i in range(100)]
        rows[-1]['connection_ok']=False
        compact=summarize_evidence(rows)
        self.assertEqual(len(compact),100)
        self.assertEqual(compact[-1]['id'],'sample-99')
        self.assertFalse(compact[-1]['connection_ok'])
        values={}
        for row in compact:values.update(row['values'])
        self.assertEqual(values,rows[-1]['values'])

    def test_unchanged_tags_do_not_hide_item_count_and_force_changes(self):
        from factoryio_agents.simple_runtime import summarize_evidence
        first=self.row(0);second=deepcopy(first);second.update(id='sample-1',elapsed_seconds=1,item_count=5)
        third=deepcopy(second);third.update(id='sample-2',elapsed_seconds=2,forced_signals=['motor'])
        compact=summarize_evidence([first,second,third])
        self.assertEqual(len(compact),3)
        self.assertEqual(compact[-1]['forced_signals'],['motor'])

    def test_sample_allows_stopped_actuators_multiple_items_and_numeric_tags(self):
        from pathlib import Path
        from unittest.mock import Mock,patch
        from factoryio_agents.simple_runtime import RuntimeRun
        observer=RuntimeRun.__new__(RuntimeRun)
        observer.plc_dir=Path('injected/scene-000/plc-000');observer.scope='step-0';observer.started=0
        observer.records=[];observer.normalization=[];observer.observing=True;observer.errors=[]
        observer.contract={};observer.signals={
            'run':{'data_type':'bool','system_field':'RunInputKey'},'a':{'data_type':'bool'},
            'b':{'data_type':'bool'},'level':{'data_type':'float'}}
        tags=[{'id':key,'value':value,'isForced':False} for key,value in [('run',True),('a',False),('b',False),('level',12.5)]]
        observer.client=Mock();observer.client.tags.return_value=tags
        observer.lifecycle=Mock();observer.lifecycle.native_call.return_value={'data':{
            'scene_path':str(observer.plc_dir.parent/'scene.factoryio'),'item_count':4,'frame':1,
            'modbus':{'connected':True,'socket_connected':True,'has_error':False}}}
        with patch('factoryio_agents.simple_runtime.reconcile_tags',return_value={'passed':True}):row=observer.sample()
        self.assertEqual(row['values']['level'],12.5);self.assertEqual(row['item_count'],4)
        self.assertEqual(observer.errors,[]);self.assertEqual(row['normalized_values'],{})

    def test_sampler_precedes_start_and_operator_actions_without_early_process_pass(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock
        from factoryio_agents.simple_runtime import RuntimeRun
        with TemporaryDirectory() as tmp:
            observer=RuntimeRun.__new__(RuntimeRun)
            observer.plc_dir=Path(tmp);observer.output=Path(tmp)/'runtime';observer.output.mkdir()
            observer.records=[];observer.factoryio={'sample_interval_seconds':.001};observer.scope='baseline'
            observer.started=0;observer.observing=False;observer.errors=[];observer.trace=Mock()
            observer.lifecycle=Mock();observer.lifecycle.records=[];state={'started':False}
            def sample():
                row=self.row(len(observer.records));row['scope']=observer.scope;row['scene_running']=state['started']
                return row
            observer.sample=sample;observer.capture=Mock()
            def start():
                self.assertTrue(observer.records);self.assertFalse(observer.records[0]['scene_running'])
                state['started']=True
            observer.lifecycle.start_both.side_effect=start
            def press(object_id,seconds):
                self.assertEqual(object_id,'chosen_enable');self.assertTrue(state['started'])
                self.assertTrue(observer.records)
            observer.button=press
            plan={'steps':[{'action':'press_button','object_id':'chosen_enable'},{'action':'observe','seconds':.005}],
                  'checks':[{'id':'process','description':'Multiple workpieces move as requested.'}]}
            result=observer.run({'operator_buttons':{'chosen_enable':{}}},plan,keep_running=True)
            self.assertFalse(result['passed']);self.assertTrue(result['infrastructure']['passed'])
            self.assertEqual(result['process_verdict'],'pending_visual_review')
            observer.lifecycle.stop_both.assert_not_called()

    def test_visual_pass_requires_every_check_and_original_requirement_coverage(self):
        from factoryio_agents.simple_plc import runtime_review
        from factoryio_agents.contracts import ContractError
        checks=[{'id':'transfers','description':'Ten transfers'},{'id':'blockage','description':'Backpressure under blockage'}]
        reply={'verdict':'pass','summary':'Observed','findings':[],'coverage_complete':True,
               'check_results':[{'check_id':c['id'],'status':'pass','reason':'Observed','evidence_ids':['sample-0']} for c in checks]}
        self.assertEqual(runtime_review(reply,set(),{'sample-0'},checks),reply)
        for change in ({'coverage_complete':False},{'check_results':reply['check_results'][:1]}):
            with self.assertRaises(ContractError):runtime_review({**reply,**change},set(),{'sample-0'},checks)
        reply['check_results'][1]['status']='uncertain'
        with self.assertRaises(ContractError):runtime_review(reply,set(),{'sample-0'},checks)

    def test_compile_preparation_does_not_require_runtime_equipment_or_a_runtime_plan(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from factoryio_agents.contracts import write_json
        from factoryio_agents.simple_plc import NativePLCTools
        contract={'tags':[{'key':'a','id':'a/tag/0','object_id':'a','kind':'output','data_type':'bool'},
                          {'key':'b','id':'b/tag/0','object_id':'b','kind':'output','data_type':'bool'}],
                  'mappings':[]}
        bindings=[{'tag_key':key,'variable':'Application.SceneIO.'+key,'plc_type':'BOOL'} for key in ('a','b')]
        effective={'bindings':bindings,'transport_confirmed':True,'verified_device_bindings':[{**b,'bit_size':'1'} for b in bindings]}
        with TemporaryDirectory() as tmp:
            directory=Path(tmp);write_json(directory/'load-receipt.json',{'app_version':'injected','native_geometry':{'objects':[]}})
            workflow=SimpleNamespace(plc_mode='compile',config={'factoryio':{},'codesys':{},'protocol':{}},state={},trace=Mock())
            rendered={'io_contract':contract,'manifest':{'objects':[]}}
            design={'objects':[{'id':'a','function':'Axis A'},{'id':'b','function':'Axis B'}]}
            with patch('factoryio_agents.simple_plc.verified_bindings',return_value=effective),patch('factoryio_agents.simple_plc.attach_component_semantics'),patch('factoryio_agents.geometry_tools.attach_button_semantics'):
                _,scaffold,signals=NativePLCTools(workflow).prepare(design,rendered,directory)
            self.assertEqual(set(signals),{'a','b'});self.assertEqual(scaffold['operator_buttons'],{})


    def test_plc_stage_does_not_accept_unexecuted_requirements_or_failed_infrastructure(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import Mock
        from factoryio_agents.plc_scaffold import io_scaffold
        from factoryio_agents.simple_plc import SimplePLCStage
        bindings=[{'tag_key':key,'variable':'Application.SceneIO.'+key,'plc_type':'BOOL'} for key in ('a','b')]
        contract={'schema_version':1,'driver':'ModbusTCPClient','transport_confirmed':True,
                  'tags':[{'key':key,'id':key+'/tag/0','object_id':key,'kind':'output','data_type':'bool'} for key in ('a','b')],
                  'mappings':[{'tag_key':key} for key in ('a','b')],'bindings':bindings}
        config={'bindings':bindings,'verified_device_bindings':[{**b,'bit_size':'1'} for b in bindings]}
        scaffold=io_scaffold(config,contract);scaffold['operator_buttons']={}
        plan={'steps':[{'action':'observe','seconds':.01}],
              'checks':[{'id':'cycles','description':'Ten transfers'}, {'id':'blocked','description':'Downstream blockage prevents new upstream feed'}]}
        for infrastructure_ok in (True,False):
            with self.subTest(infrastructure_ok=infrastructure_ok),TemporaryDirectory() as tmp:
                root=Path(tmp);directory=root/'scene-000';directory.mkdir();image=root/'injected.png';image.write_bytes(b'injected test image')
                w=SimpleNamespace(output=root,state={'live_pipeline_passed':False},max_plc_repairs=0,plc_mode='run',
                    runtime_seconds=.01,trace=Mock(),raw_model=Mock(),
                    training=SimpleNamespace(directory=root/'training-data',last_candidate_id='injected'))
                reply={'verdict':'uncertain' if infrastructure_ok else 'pass','summary':'Synthetic evidence only','findings':[],
                       'coverage_complete':not infrastructure_ok,'check_results':[
                       {'check_id':c['id'],'status':'uncertain' if infrastructure_ok else 'pass',
                        'reason':'This test has no executed blockage evidence.' if infrastructure_ok else 'Injected review',
                        'evidence_ids':['image-0']} for c in plan['checks']]}
                w.call=Mock(side_effect=[{'declaration':'PROGRAM PLC_PRG\nVAR\nEND_VAR','implementation':'SceneIO.a := TRUE;',
                                         'unresolved':[],'notes':['injected test only'],'runtime_plan':plan},reply])
                def finish(status,**details):w.state.update(status=status,**details);return w.state
                w.finish=finish;tools=Mock();tools.prepare.return_value=(config,scaffold,{s['tag_key']:s for s in scaffold['signals']})
                tools.compile.return_value={'compiled':True,'started':True,'bindings_verified':True,'task_program_verified':True,'transport_verified':True}
                tools.measure.return_value=({'infrastructure':{'passed':infrastructure_ok},'passed':False,'left_running':True},
                    {'records':[{'id':'image-0','kind':'image','path':str(image),'timestamp':'injected'}]})
                requirement='Ten handoffs and downstream blockage backpressure.'
                result=SimplePLCStage(w,tools).run(requirement,{'objects':[{'id':key,'prefab':'BeltConveyor4M','function':'Independent section'} for key in ('a','b')]},
                    {'io_contract':deepcopy(contract),'manifest':{'scene_sha256':'injected'}},directory)
                self.assertEqual(result['status'],'needs_attention');self.assertFalse(result['live_pipeline_passed'])
                self.assertFalse(result['acceptance']['left_running']);tools.stop.assert_called()
                self.assertEqual(w.call.call_args.args[2]['requirement'],requirement)
                self.assertEqual(tools.measure.call_args.args[-1]['checks'],plan['checks'])
                if not infrastructure_ok:self.assertEqual(result['target'],'tools')
