"""Adversarial checks for reusable measurement primitives, not a conveyor template."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from factoryio_agents.contracts import ContractError, file_hash, write_json
from factoryio_agents.signal_validation import validate_signal_contract, validate_signal_trace
from factoryio_agents.runtime import execute_model_probes, collect_evidence, ProbeTriggerTimeout


def fixture(duration=2, prefix='machine'):
    task = {'objects':[{'id':prefix,'prefab':'ArbitraryObservedDevice'},
                       {'id':'button','prefab':'StopPushButton'}], 'process':[{'id':'operate'}]}
    names = ['begun','busy','finished','temperature','pressed']
    rule = {'kind':'signal_contract','signals':{n:{'object_id':prefix if n != 'pressed' else 'button',
             'kind':'input','index':i,'meaning':'pressed' if n == 'pressed' else 'raw'}
            for i,n in enumerate(names)}, 'checks':[
        {'id':'order','process_ids':['operate'],'kind':'sequence','steps':[{'signal':'begun','edge':'rising'},
            {'signal':'finished','edge':'rising'}],'min_matches':2},
        {'id':'timing','process_ids':['operate'],'kind':'duration','start':{'signal':'busy','edge':'rising'},
            'end':{'signal':'busy','edge':'falling'},'min_seconds':duration-.15,'max_seconds':duration+.15,
            'min_matches':2,'while':{'busy':True}},
        {'id':'isolation','process_ids':['operate'],'kind':'alternation','start':{'signal':'begun','edge':'rising'},
            'end':{'signal':'finished','edge':'rising'},'min_matches':2},
        {'id':'range','process_ids':['operate'],'kind':'invariant','when':{'busy':True},
            'expect':{'temperature':{'op':'lt','value':50}},'min_samples':2}], 'probes':[]}
    tags = [{'key':n,'object_id':prefix if n != 'pressed' else 'button','kind':'input','index':i,
             'data_type':'float' if n == 'temperature' else 'bool'} for i,n in enumerate(names)]
    contract = {'tags':tags,'component_semantics':[{'tag_key':'pressed','app_version_confirmed':True,
                                                  'semantics':{'pressed_value':False}}]}
    rule['signals']['busy']['kind'] = 'output'; tags[1]['kind'] = 'output'
    rows = []
    def row(t,a,b,c,temp=20):
        values = [a,b,c,temp,True]
        rows.append({'id':'s'+str(len(rows)),'kind':'tag_sample','elapsed_seconds':t,
            'timestamp':'2026-10-11T08:00:00+00:00','tags':[{'id':n,'value':v,
                'isForced':False,'openCircuit':False,'shortCircuit':False} for n,v in zip(names,values)]})
    for offset in (0,duration+2):
        row(offset,False,False,False)
        row(offset+.05,True,True,False)
        row(offset+duration-.05,True,True,False)
        row(offset+duration+.05,False,False,True)
        row(offset+duration+.1,False,False,False)
    return task, rule, contract, {'mode':'live','records':rows}


class SignalValidationTests(unittest.TestCase):
    def checked_probes(self, rule):
        rule['probes'] = [{'id':'test','stage':'after_capture','actions':[
            {'label':'press','object_id':'button','pressed':True,'hold_seconds':.7},
            {'label':'release','object_id':'button','pressed':False,'hold_seconds':.7}]}]
        for label, pressed in [('press',True),('release',False)]:
            rule['checks'].append({'id':label,'process_ids':['operate'],'kind':'probe_state',
                'phase':'test.'+label,'settle_seconds':.1,'expect':{'pressed':pressed,'busy':False},'min_samples':2})
    def test_same_interpreter_handles_different_objects_and_durations(self):
        for duration, name in [(2,'transport'),(.5,'pump'),(4,'lift')]:
            task,rule,io,evidence = fixture(duration,name)
            result = validate_signal_trace(rule,task,evidence,io)
            self.assertTrue(result['passed'],result['issues'])
            self.assertEqual(len(result['checks'][1]['measurements']),2)
            self.assertTrue(result['source_record_ids'])

    def test_short_release_reports_action_value_and_bounds_without_changing_model_output(self):
        task, rule, _, _ = fixture()
        self.checked_probes(rule)
        rule['probes'][0]['actions'][1]['hold_seconds'] = .2
        original = deepcopy(rule)
        with self.assertRaises(ContractError) as caught:
            validate_signal_contract(rule, task)
        message = str(caught.exception)
        self.assertIn('test.release.hold_seconds=0.2', message)
        self.assertIn('[0.4, 3]', message)
        self.assertIn('pressed=false', message)
        self.assertEqual(rule, original)

    def test_press_and_release_use_the_same_inclusive_measurement_bounds(self):
        task, rule, _, _ = fixture()
        self.checked_probes(rule)
        for press, release in ((.4, 3), (3, .4)):
            rule['probes'][0]['actions'][0]['hold_seconds'] = press
            rule['probes'][0]['actions'][1]['hold_seconds'] = release
            self.assertEqual(validate_signal_contract(rule, task), rule)
        rule['probes'][0]['actions'][0]['hold_seconds'] = .2
        with self.assertRaisesRegex(ContractError, 'test.press.hold_seconds=0.2'):
            validate_signal_contract(rule, task)

    def test_nominal_setpoint_does_not_pass_short_actual_hold(self):
        task,rule,io,evidence = fixture(.5)
        rule['checks'][1].update(min_seconds=1.7,max_seconds=2.3)
        self.assertFalse(validate_signal_trace(rule,task,evidence,io)['passed'])

    def test_coarse_samples_cannot_prove_precise_duration(self):
        task,rule,io,evidence = fixture()
        evidence['records'] = [r for r in evidence['records'] if r['id'] not in ('s2','s7')]
        result = validate_signal_trace(rule,task,evidence,io)
        self.assertFalse(result['passed'])
        self.assertIn('duration interval',str(result['issues']))

    def test_forced_faulty_missing_and_unordered_samples_fail(self):
        task,rule,io,original = fixture()
        for field in ('isForced','openCircuit','shortCircuit'):
            evidence = deepcopy(original); evidence['records'][1]['tags'][0][field] = True
            self.assertFalse(validate_signal_trace(rule,task,evidence,io)['passed'])
        evidence = deepcopy(original); evidence['records'][1]['tags'].pop()
        self.assertFalse(validate_signal_trace(rule,task,evidence,io)['passed'])
        evidence = deepcopy(original); evidence['records'][1]['elapsed_seconds'] = 0
        self.assertFalse(validate_signal_trace(rule,task,evidence,io)['passed'])

    def test_no_qualifying_observation_is_not_a_pass(self):
        task,rule,io,evidence = fixture()
        rule['checks'][3]['when'] = {'pressed':True}
        result = validate_signal_trace(rule,task,evidence,io)
        self.assertFalse(result['passed']); self.assertIn('vacuously',str(result['issues']))

    def test_overlapping_starts_are_detected_even_if_sequence_can_complete(self):
        task,rule,io,evidence = fixture()
        evidence['records'][2]['tags'][0]['value'] = False
        evidence['records'].insert(3,deepcopy(evidence['records'][2]))
        extra = evidence['records'][3]; extra['id']='extra'; extra['elapsed_seconds']+=.025
        extra['tags'][0]['value'] = True
        result = validate_signal_trace(rule,task,evidence,io)
        self.assertFalse(result['passed']); self.assertIn('overlap',str(result['issues']))

    def test_unknown_primitive_reference_or_polarity_is_rejected(self):
        task,rule,io,evidence = fixture()
        bad = deepcopy(rule); bad['checks'][0]['steps'][0]['signal']='hallucinated'
        with self.assertRaises(ContractError): validate_signal_contract(bad,task)
        bad = deepcopy(rule); bad['checks'][0]['kind']='custom_python'
        with self.assertRaises(ContractError): validate_signal_contract(bad,task)
        io['component_semantics'] = []
        self.assertFalse(validate_signal_trace(rule,task,evidence,io)['passed'])

    def test_native_probe_must_release_and_receipt_must_match(self):
        task,rule,io,evidence = fixture()
        self.checked_probes(rule)
        bad = deepcopy(rule); bad['probes'][0]['actions'].pop()
        with self.assertRaisesRegex(ContractError,'release'): validate_signal_contract(bad,task)
        result = validate_signal_trace(rule,task,evidence,io)
        self.assertFalse(result['passed']); self.assertIn('action missing',str(result['issues']))

    def test_failed_native_press_is_released_and_partial_evidence_saved(self):
        task,rule,io,evidence = fixture()
        self.checked_probes(rule)
        task['acceptance'] = [rule]
        manifest = {'objects':[{'id':'button','key':'native-key','prefab':'StopPushButton'}]}
        client = Mock(); client.tags.return_value = evidence['records'][0]['tags']; io['tags'] = []
        # Resolve data separately here; this test concerns cleanup, not a fake physics pass.
        resolved = {'pressed':{'key':'pressed','data_type':'bool','active_value':False}}
        actions=[]
        def hook(command,values,timeout):
            actions.append(values['pressed'])
            write_json(Path(values['output']),{'action':'operator_button','ok':True,'data':{'operator_button':{
                'object_key':values['object_key'],'pressed':values['pressed']=='true','native_input':values['pressed']=='false'}}})
        with tempfile.TemporaryDirectory() as tmp, patch('factoryio_agents.runtime.run_hook',side_effect=hook), \
             patch('factoryio_agents.signal_validation.resolve_signals',return_value=resolved), \
             patch('factoryio_agents.runtime.time.monotonic',side_effect=[float(i)/10 for i in range(100)]), \
             patch('factoryio_agents.runtime.time.sleep'):
            with self.assertRaisesRegex(ContractError,'not confirmed'):
                execute_model_probes({'operator_command':['native']},task,manifest,Path(tmp),evidence,client,0,io,'after_capture')
            self.assertTrue((Path(tmp)/'evidence.json').is_file())
        self.assertEqual(actions,['true','false'])

    def test_unmeasured_native_actions_are_rejected_before_execution(self):
        task,rule,io,evidence = fixture(); self.checked_probes(rule)
        rule['checks'] = [c for c in rule['checks'] if c['kind'] != 'probe_state']
        with self.assertRaisesRegex(ContractError,'PLC-output measurement'): validate_signal_contract(rule,task)

    def test_unreached_probe_condition_keeps_measurements_and_fails_without_native_actions(self):
        task,rule,io,evidence = fixture(); self.checked_probes(rule)
        rule['probes'][0].update(wait_for={'busy':True},timeout_seconds=.2)
        task['acceptance'] = [rule]
        manifest = {'objects':[{'id':'button','key':'native-key','prefab':'StopPushButton'}]}
        client = Mock(); client.tags.return_value = evidence['records'][0]['tags']
        with tempfile.TemporaryDirectory() as tmp, \
             patch('factoryio_agents.runtime.reconcile_tags',return_value={'passed':True}), \
             patch('factoryio_agents.runtime.time.monotonic',side_effect=[float(i)/10 for i in range(100)]), \
             patch('factoryio_agents.runtime.time.sleep'), patch('factoryio_agents.runtime.run_hook') as hook:
            with self.assertRaises(ProbeTriggerTimeout):
                execute_model_probes({'operator_command':['native']},task,manifest,Path(tmp),evidence,client,-10,io,'after_capture')
            hook.assert_not_called()
            self.assertTrue((Path(tmp)/'evidence.json').is_file())
        failure = evidence['records'][-1]
        self.assertEqual(failure['reason'],'trigger_unobserved')
        ids = {r['id'] for r in evidence['records'] if r['kind']=='tag_sample'}
        self.assertTrue(set(failure['source_record_ids']) <= ids)
        result = validate_signal_trace(rule,task,evidence,io)
        self.assertFalse(result['passed'])
        self.assertEqual(len(result['checks']),len(rule['checks']),result['issues'])
        self.assertTrue(result['checks'][0]['passed'])  # Other measured checks remain reviewable.
        self.assertFalse(result['checks'][-1]['passed'])
        self.assertIn('probe trigger not measured',str(result['issues']))

    def test_collector_returns_failed_probe_evidence_but_propagates_native_failure(self):
        def timeout(config,task,manifest,output,evidence,*args):
            evidence['records'].append({'id':'unreached','kind':'probe_failure','reason':'trigger_unobserved'})
            raise ProbeTriggerTimeout('fixture condition not observed')
        with tempfile.TemporaryDirectory() as tmp, \
             patch('factoryio_agents.runtime.FactoryIOClient') as factory, \
             patch('factoryio_agents.runtime.time.monotonic',side_effect=[float(i) for i in range(100)]), \
             patch('factoryio_agents.runtime.time.sleep'):
            factory.return_value.tags.return_value = []
            config = {'runtime_seconds':.1,'execute_model_probes':True}
            with patch('factoryio_agents.runtime.execute_model_probes',side_effect=timeout):
                result = collect_evidence(config,Path(tmp),task={'acceptance':[]})
                self.assertIn('finished_at',result)
                self.assertEqual(sum(r['kind']=='probe_failure' for r in result['records']),2)
            with patch('factoryio_agents.runtime.execute_model_probes',side_effect=ContractError('native action not confirmed')):
                with self.assertRaisesRegex(ContractError,'native action'):
                    collect_evidence(config,Path(tmp),task={'acceptance':[]})

    def test_optional_null_trigger_is_unconditional_without_rewriting_model_plan(self):
        task,rule,io,evidence = fixture(); self.checked_probes(rule)
        rule['probes'][0]['wait_for'] = None; original = deepcopy(rule)
        validate_signal_contract(rule,task)
        self.assertEqual(rule,original)

    def test_planning_obeys_inspected_capacity_for_arbitrary_prefabs(self):
        from factoryio_agents.agents import validate_planning_capacity
        catalog = {'templates':[{'prefab':'Pump','tags':[{'kind':'output','data_type':'bool'}]}],
                   'device_io_capacity':{'counts':{'input':1,'output':1},'data_types':['bool'],
                                         'system_bool_inputs_reserved':1}}
        task = {'objects':[{'id':'a','prefab':'Pump'}],'unresolved':[]}
        validate_planning_capacity(task,catalog)
        task['objects'].append({'id':'b','prefab':'Pump'})
        with self.assertRaisesRegex(ContractError,'exceeds inspected'): validate_planning_capacity(task,catalog)


if __name__ == '__main__': unittest.main()
