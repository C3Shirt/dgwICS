"""Leaving a simulator running requires fresh process evidence, not a Run ack."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factoryio_agents.contracts import ContractError, canonical_hash, read_json
from factoryio_agents.live_handoff import operating_projection, start_live_handoff


def task_fixture():
    return {'acceptance': [{'kind':'signal_contract', 'signals':{}, 'probes':[
        {'id':'boot', 'stage':'startup', 'actions':[{'label':'press'}]},
        {'id':'halt', 'stage':'after_capture', 'actions':[{'label':'press'}]}],
        'checks':[{'id':'boot-check', 'kind':'probe_state', 'phase':'boot.press'},
                  {'id':'motion', 'kind':'sequence'},
                  {'id':'halt-check', 'kind':'probe_state', 'phase':'halt.press'}]}]}


class LiveHandoffTests(unittest.TestCase):
    def test_projection_preserves_model_task_and_all_process_checks(self):
        task = task_fixture(); before = canonical_hash(task)
        scope, omitted = operating_projection(task)
        self.assertEqual(canonical_hash(task), before)
        self.assertEqual(omitted, ['halt-check'])
        self.assertEqual([c['id'] for c in scope['acceptance'][0]['checks']], ['boot-check','motion'])
        self.assertEqual([p['id'] for p in scope['acceptance'][0]['probes']], ['boot'])

    def test_lamp_only_contract_cannot_leave_scene_running(self):
        task = task_fixture(); task['acceptance'][0]['checks'].pop(1)
        with self.assertRaises(ContractError): operating_projection(task)

    def invoke(self, acceptance, *, error=None, paused=False):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        output = Path(tmp.name)
        evidence = {'mode':'live', 'reconciliation':{'passed':True}, 'records':[]}
        with (patch('factoryio_agents.live_handoff.FactoryIOClient') as factory,
              patch('factoryio_agents.live_handoff.collect_evidence', return_value=evidence) as collect,
              patch('factoryio_agents.live_handoff.validate_events', return_value=acceptance)):
            runtime = factory.return_value
            runtime.wait_running.side_effect = lambda expected: {'running':expected, 'isForced':False}
            runtime.tags.return_value = [{'name':'FACTORY I/O (Paused)', 'kind':'Input',
                                         'value':paused, 'isForced':False}]
            if error: collect.side_effect = error
            try:
                value = start_live_handoff({}, task_fixture(), {}, {}, {}, output)
            except BaseException as exc:
                value = exc
        return value, runtime, output

    def test_process_pass_leaves_running_and_records_current_running_input(self):
        value, runtime, output = self.invoke({'passed':True, 'rules':[], 'issues':[]})
        self.assertTrue(value[0]['left_running'])
        self.assertEqual(runtime.control.call_args_list[-1].args, ('run',True))
        self.assertTrue(read_json(output/'receipt.json')['running_at_handoff']['running'])

    def test_stalled_restart_stops_and_retains_failed_measurements(self):
        value, runtime, output = self.invoke({'passed':False, 'rules':[], 'issues':['no complete measured match']})
        self.assertFalse(value[0]['left_running'])
        self.assertEqual(runtime.control.call_args_list[-1].args, ('run',False))
        self.assertFalse(read_json(output/'receipt.json')['passed'])

    def test_capture_error_or_paused_scene_stops_instead_of_handing_off(self):
        for options in ({'error':ContractError('API lost')}, {'paused':True}):
            with self.subTest(options=options):
                value, runtime, output = self.invoke({'passed':True}, **options)
                self.assertIsInstance(value, ContractError)
                self.assertEqual(runtime.control.call_args_list[-1].args, ('run',False))
                self.assertFalse(read_json(output/'receipt.json')['left_running'])
