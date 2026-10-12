"""Real bridge control logic with a fake ScriptEngine online target."""
import contextlib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factoryio_agents import codesys_bridge as bridge


class OnlineTarget:
    def __init__(self, state):
        self.application_state = state; self.is_logged_in = True; self.operation_state = 'loaded'
        self.events = []; self.timeout = 60
    def login(self, *args): self.events.append('login')
    def logout(self): self.events.append('logout')
    def stop(self):
        if self.application_state == 'stop': raise RuntimeError('already stopped')
        self.events.append('stop'); self.application_state = 'stop'
    def start(self):
        if self.application_state == 'run': raise RuntimeError('already running')
        self.events.append('start'); self.application_state = 'run'
    def reset(self, mode, force):
        self.events.append(('reset',mode,force)); self.application_state = 'stop'


class ScriptLifecycleTests(unittest.TestCase):
    def invoke(self, state, action, mode=None):
        target = OnlineTarget(state)
        gateway = SimpleNamespace(name='local',guid='g',config_params={'0':'127.0.0.1'},
            gateway_driver=SimpleNamespace(name='TCP/IP'),find_address_by_ip=lambda *args:'verified-address')
        device = Mock(); device.get_simulation_mode.return_value = False
        api = SimpleNamespace(gateways=[gateway],create_online_application=lambda app:contextlib.nullcontext(target))
        names = {'online':api,'OnlineChangeOption':SimpleNamespace(Keep=1,Never=2),
            'CredentialSourceKind':SimpleNamespace(CurrentProjectSession=1),
            'ResetOption':SimpleNamespace(Warm=10,Cold=20,Original=30)}
        spec = {'connect':True,'host':'127.0.0.1','port':11740,'device_name':'Device','action':action}
        if mode: spec['reset_mode'] = mode
        with (patch.dict(bridge.__dict__,names), patch.object(bridge,'_walk',return_value=[(device,'Device')]),
              patch.object(bridge,'_name',return_value='Device'), patch.object(bridge,'_flag',return_value=True),
              patch.object(bridge,'_delay')):
            result = bridge._online_session(Mock(),Mock(),spec)
        return target, result

    def test_warm_and_cold_call_correct_enum_after_stopping(self):
        for mode, enum in [('warm',10),('cold',20)]:
            with self.subTest(mode=mode):
                target,result = self.invoke('run','reset',mode)
                self.assertEqual(target.events,['login','stop',('reset',enum,False),'logout'])
                self.assertEqual(result['application_state'],'stop'); self.assertTrue(result['reset_called'])
                self.assertFalse(result['download_requested'])

    def test_already_stopped_reset_still_calls_reset_without_second_stop(self):
        target,result = self.invoke('stop','reset','warm')
        self.assertEqual(result['status'],'ok'); self.assertTrue(result['stop_already_satisfied'])
        self.assertEqual(target.events,['login',('reset',10,False),'logout'])

    def test_stop_and_start_are_idempotent_without_spoofing_method_calls(self):
        for state,action in [('stop','stop'),('run','start')]:
            with self.subTest(action=action):
                target,result = self.invoke(state,action)
                self.assertEqual(result['status'],'ok'); self.assertTrue(result[action+'_already_satisfied'])
                self.assertFalse(result.get(action+'_called',False))
                self.assertEqual(target.events,['login','logout'])

    def test_origin_is_rejected_before_stop_or_reset(self):
        target,result = self.invoke('run','reset','origin')
        self.assertEqual(result['status'],'error'); self.assertEqual(target.events,['login'])
