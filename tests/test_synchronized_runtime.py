"""Lifecycle ordering and state-confirmation tests; no real PLC is used."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from factoryio_agents.contracts import ContractError
from factoryio_agents.synchronized_runtime import SynchronizedRuntime
from factoryio_agents.plc_observation import observable_expressions


class SynchronizedRuntimeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.controller = SynchronizedRuntime.__new__(SynchronizedRuntime)
        c = self.controller
        c.output = Path(tmp.name); c.records = []; c.codesys = {}; c.project = c.output/'source.project'
        c.runtime = Mock()
        c.runtime.wait_running.side_effect = lambda value: {'running':value}

    def test_reset_stops_plc_before_clearing_scene_and_does_not_start_either_end(self):
        c = self.controller; events = []
        c.freeze_factory = lambda: events.append('freeze')
        c.plc_action = lambda action, mode=None: events.append((action,mode))
        c.require_current_scene = lambda empty=False: events.append(('verify_empty',empty))
        c.runtime.control.side_effect = lambda action, value: events.append((action,value))
        with patch('factoryio_agents.synchronized_runtime.time.sleep'):
            receipt = c.reset_both('warm')
        self.assertEqual(events[:3], ['freeze',('reset','warm'),('run',False)])
        self.assertIn(('reset',True), events); self.assertIn(('reset',False), events)
        self.assertEqual(events[-1], ('verify_empty',True))
        self.assertNotIn(('run',True), events)
        self.assertTrue(receipt['operation_passed']); self.assertFalse(receipt['physical_acceptance_passed'])

    def test_cold_is_explicit_and_origin_is_rejected_before_mutation(self):
        c = self.controller
        c.freeze_factory = Mock(); c.plc_action = Mock(); c.require_current_scene = Mock()
        with patch('factoryio_agents.synchronized_runtime.time.sleep'): c.reset_both('cold')
        c.plc_action.assert_called_once_with('reset','cold')
        c.freeze_factory.reset_mock()
        with self.assertRaises(ContractError): c.reset_both('origin')
        c.freeze_factory.assert_not_called()

    def test_plc_run_ack_cannot_certify_stop(self):
        c = self.controller
        response = {'compiled':True, 'source_unchanged':True,
                    'online_probe':{'status':'ok','connected':True,'stop_called':True,'application_state':'run'}}
        with patch('factoryio_agents.synchronized_runtime.run_codesys',return_value=response):
            with self.assertRaisesRegex(ContractError,'state not confirmed'): c.plc_action('stop')

    def test_factory_starts_only_after_plc_run_and_native_connection(self):
        c = self.controller; events = []
        c.require_current_scene = lambda: events.append('check_native') or {'modbus':{'connected':True,'socket_connected':True,'has_error':False}}
        c.reconnect = lambda: events.append('reconnect')
        c.plc_action = lambda action: events.append(('plc',action))
        c.runtime.control.side_effect = lambda action,value: events.append((action,value))
        c.start_both()
        self.assertLess(events.index(('plc','start')), events.index(('run',True)))
        self.assertEqual(events[:4], ['check_native',('plc','start'),'reconnect','check_native'])

    def test_observation_excludes_function_blocks_and_qualifies_local_variables(self):
        program = {'program_name':'PLC_PRG', 'declaration':'PROGRAM PLC_PRG\nVAR\nState:INT; Busy:BOOL; T:TON; E:R_TRIG;\nEND_VAR', 'bindings':[]}
        self.assertEqual(observable_expressions(program), ['Application.PLC_PRG.state','Application.PLC_PRG.busy'])
