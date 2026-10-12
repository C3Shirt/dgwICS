"""Minimal PLC loop checks use synthetic observations, not live acceptance claims."""
from copy import deepcopy
import unittest


def sample(t, count=0, entry=False, exit=False, conveyor=True, feed=False, scope='cycle'):
    return {'timestamp':str(t),'elapsed_seconds':t,'scope':scope,'item_count':count,
        'values':{'running':True,'entry':entry,'exit':exit,'conveyor':conveyor,'feed':feed,'remove':True},
        'forced_signals':[],'connection_ok':True}


def cycle(offset=0):
    return [sample(offset),sample(offset+.1,1,entry=True),sample(offset+.3,1),
            sample(offset+1,1,exit=True),sample(offset+1.2)]


class SimplePLCTests(unittest.TestCase):
    def evaluate(self, samples, cycles=2):
        from factoryio_agents.simple_runtime import evaluate_transfer
        return evaluate_transfer(samples,required_cycles=cycles)

    def test_continuous_remover_can_pass_two_ordered_single_box_cycles(self):
        result=self.evaluate(cycle()+cycle(2))
        self.assertTrue(result['passed']);self.assertEqual(result['completed_cycles'],2)
        self.assertEqual(result['observed_max_items'],1)

    def test_exit_before_entry_and_missing_removal_are_observed_failures(self):
        wrong=[sample(0),sample(.1,1,exit=True),sample(.4)]
        self.assertFalse(self.evaluate(wrong,1)['passed'])
        stuck=cycle()[:-1]+[sample(2,1),sample(3,1)]
        self.assertFalse(self.evaluate(stuck,1)['passed'])

    def test_multiple_boxes_and_stopped_belt_cannot_pass_even_with_ordered_edges(self):
        rows=cycle()+cycle(2);rows[2]['item_count']=2;rows[3]['values']['conveyor']=False
        result=self.evaluate(rows)
        self.assertFalse(result['passed'])
        self.assertIn('multiple_items',{r['code'] for r in result['issues']})
        self.assertIn('conveyor_stopped',{r['code'] for r in result['issues']})

    def test_forced_inputs_and_missing_connection_invalidate_runtime_evidence(self):
        rows=cycle()+cycle(2);rows[2]['forced_signals']=['entry'];rows[3]['connection_ok']=False
        self.assertFalse(self.evaluate(rows)['passed'])

    def test_feed_must_disable_after_entry_until_item_is_removed(self):
        rows=cycle()+cycle(2);rows[2]['values']['feed']=True
        rows.insert(3,sample(.7,1,feed=True))
        result=self.evaluate(rows)
        self.assertIn('feed_not_closed',{r['code'] for r in result['issues']})

    def test_other_scopes_cannot_fabricate_cycle_completion(self):
        rows=cycle()+cycle(2)
        for r in rows[5:]:r['scope']='operator_stop'
        self.assertFalse(self.evaluate(rows)['passed'])

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
