"""Adversarial checks of the no-download online probe; PLC calls are mocked."""
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

from factoryio_agents import codesys_bridge as bridge


class OnlineProbeTests(unittest.TestCase):
    def setUp(self):
        self.device = Mock(is_device=True)
        self.device.get_name.return_value = "Device"
        self.device.get_simulation_mode.return_value = False
        self.gateway = Mock()
        self.gateway.name = "Gateway-1"
        self.gateway.guid = "local-gateway"
        self.gateway.gateway_driver.name = "TCP/IP"
        self.gateway.config_params = {0: "localhost", 1: 1217}
        self.gateway.find_address_by_ip.return_value = "00F4"
        self.online_app = Mock(is_logged_in=True, application_state="run", operation_state="program_loaded", timeout=60)
        self.context = MagicMock()
        self.context.__enter__.return_value = self.online_app
        self.online = Mock(gateways=[self.gateway])
        self.online.create_online_application.return_value = self.context
        self.delay = patch.object(bridge, "_delay").start()
        self.addCleanup(patch.stopall)

    def probe(self, spec, options=None, sources=None, progress=None):
        with patch.dict(bridge.__dict__, {"online": self.online,
                "CredentialSourceKind": sources if sources is not None else types.SimpleNamespace(CurrentProjectSession="PROJECT_SESSION"),
                "OnlineChangeOption": options if options is not None else types.SimpleNamespace(Keep="KEEP", Never="FULL_DOWNLOAD")}), \
             patch.object(bridge, "_walk", return_value=[(self.device, "Root/Device")]):
            return bridge._online_probe(Mock(), Mock(), spec, progress)

    def assert_no_change(self):
        self.online_app.start.assert_not_called()
        self.online_app.stop.assert_not_called()
        self.online_app.reset.assert_not_called()
        self.online_app.force_prepared_values.assert_not_called()

    def test_capabilities_does_not_scan_login_or_configure_device(self):
        report = self.probe({"connect": False})
        self.assertEqual(report["status"], "ok")
        self.gateway.find_address_by_ip.assert_not_called()
        self.online_app.login.assert_not_called()
        self.device.set_gateway_and_address.assert_not_called()
        self.assert_no_change()

    def test_missing_keep_cannot_fall_back_to_full_download(self):
        report = self.probe({"connect": True}, options=types.SimpleNamespace(Never="FULL_DOWNLOAD"))
        self.assertEqual(report["status"], "error")
        self.gateway.find_address_by_ip.assert_not_called()
        self.online_app.login.assert_not_called()
        self.assert_no_change()

    def test_remote_gateway_cannot_be_used_for_loopback_probe(self):
        self.gateway.config_params[0] = "192.168.1.10"
        report = self.probe({"connect": True})
        self.assertEqual(report["status"], "error")
        self.gateway.find_address_by_ip.assert_not_called()
        self.online_app.login.assert_not_called()
        self.assert_no_change()

    def test_failed_login_does_not_report_connection_or_start_program(self):
        self.online_app.is_logged_in = False
        report = self.probe({"connect": True})
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["connected"])
        self.online_app.login.assert_called_once_with("KEEP", False)
        self.assert_no_change()

    def test_successful_keep_login_only_reads_state_and_logs_out(self):
        report = self.probe({"connect": True})
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["connected"])
        self.assertFalse(report["download_requested"])
        self.assertFalse(report["start_called"])
        self.gateway.find_address_by_ip.assert_called_once_with("127.0.0.1", 11740)
        self.online_app.login.assert_called_once_with("KEEP", False)
        self.online_app.logout.assert_called_once()
        self.assertEqual(self.online.auth_fallback_modes, "PROJECT_SESSION")
        self.assertEqual(self.online_app.timeout, 15)
        self.assert_no_change()

    def test_missing_noninteractive_auth_mode_refuses_scan_and_login(self):
        report = self.probe({"connect": True}, sources=types.SimpleNamespace())
        self.assertEqual(report["status"], "error")
        self.gateway.find_address_by_ip.assert_not_called()
        self.online_app.login.assert_not_called()
        self.assert_no_change()

    def test_scan_failure_preserves_stage_in_progress_without_false_connection(self):
        from copy import deepcopy
        checkpoints = []
        self.gateway.find_address_by_ip.side_effect = RuntimeError("scan failed")
        report = self.probe({"connect": True}, progress=lambda data: checkpoints.append(deepcopy(data)))
        self.assertEqual(report["error_stage"], "scan")
        self.assertEqual([row["stage"] for row in checkpoints], ["inspect", "scan", "failed"])
        self.assertFalse(checkpoints[1]["connected"])
        self.assertEqual(checkpoints[2]["status"], "error")
        self.online_app.login.assert_not_called()
        self.assert_no_change()

    def test_environment_credentials_are_device_scoped_and_cleared(self):
        with patch.dict(bridge.os.environ, {"TEST_PLC_USERNAME": "test-device-user", "TEST_PLC_PASSWORD": "test-device-secret"}):
            report = self.probe({"connect": True, "credential_env": {
                "username_env": "TEST_PLC_USERNAME", "password_env": "TEST_PLC_PASSWORD"}})
        self.assertTrue(report["connected"])
        self.assertTrue(report["script_credentials_cleared"])
        self.online.set_specific_credentials.assert_called_once_with(self.device, "test-device-user", "test-device-secret")
        self.online.clear_all_credentials.assert_called_once()
        self.assertNotIn("test-device-secret", str(report))
        self.assert_no_change()

    def test_missing_credentials_refuse_login_without_prompt(self):
        with patch.dict(bridge.os.environ, {}, clear=True):
            report = self.probe({"connect": True, "credential_env": {
                "username_env": "TEST_PLC_USERNAME", "password_env": "TEST_PLC_PASSWORD"}})
        self.assertEqual(report["status"], "error")
        self.gateway.find_address_by_ip.assert_not_called()
        self.online_app.login.assert_not_called()
        self.online.set_specific_credentials.assert_not_called()
        self.assert_no_change()

    def test_device_error_cannot_echo_configured_secret_to_report(self):
        self.online_app.login.side_effect = RuntimeError("denied for test-device-user using test-device-secret")
        with patch.dict(bridge.os.environ, {"TEST_PLC_USERNAME": "test-device-user", "TEST_PLC_PASSWORD": "test-device-secret"}):
            report = self.probe({"connect": True, "credential_env": {
                "username_env": "TEST_PLC_USERNAME", "password_env": "TEST_PLC_PASSWORD"}})
        self.assertEqual(report["error_stage"], "login")
        self.assertNotIn("test-device-secret", str(report))
        self.assertNotIn("test-device-user", str(report))
        self.online.clear_all_credentials.assert_called_once()
        self.assert_no_change()

    def test_download_session_uses_same_resolved_target_and_verifies_run(self):
        with patch.dict(bridge.__dict__, {"online": self.online,
                "CredentialSourceKind": types.SimpleNamespace(CurrentProjectSession="PROJECT_SESSION"),
                "OnlineChangeOption": types.SimpleNamespace(Keep="KEEP", Never="FULL_DOWNLOAD")}), \
             patch.object(bridge, "_walk", return_value=[(self.device, "Root/Device")]):
            report = bridge._online_session(Mock(), Mock(), {"connect": True}, download=True)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["download_requested"])
        self.assertTrue(report["start_called"])
        self.online_app.login.assert_called_once_with("FULL_DOWNLOAD", False)
        self.online_app.start.assert_called_once()
        self.online_app.stop.assert_not_called()

    def test_resolved_target_is_bound_before_online_application_is_created(self):
        events = []
        self.device.set_gateway_and_address.side_effect = lambda *args: events.append("bind")
        self.online.create_online_application.side_effect = lambda *args: (events.append("create") or self.context)
        report = self.probe({"connect": True})
        self.assertEqual(report["status"], "ok")
        self.assertEqual(events, ["bind", "create"])

    def test_ide_simulation_is_disabled_in_copy_before_external_login(self):
        self.device.get_simulation_mode.side_effect = [True, False]
        events = []
        self.device.set_simulation_mode.side_effect = lambda value: events.append(("simulation", value))
        self.online_app.login.side_effect = lambda *args: events.append(("login", args[0]))
        report = self.probe({"connect": True})
        self.assertEqual(report["simulation_mode_before"], "True")
        self.assertEqual(report["simulation_mode_after"], "False")
        self.assertEqual(events, [("simulation", False), ("login", "KEEP")])
        self.assert_no_change()

    def test_simulation_that_cannot_be_disabled_refuses_external_login(self):
        self.device.get_simulation_mode.return_value = True
        report = self.probe({"connect": True})
        self.assertEqual(report["status"], "error")
        self.online.create_online_application.assert_not_called()
        self.online_app.login.assert_not_called()
        self.assert_no_change()

    def test_monitor_subscription_waits_for_message_loop_before_second_read(self):
        events = []
        self.delay.side_effect = lambda ms: events.append(("delay", ms))
        self.online_app.read_value.side_effect = lambda name: (events.append(("read", name)) or "INT#30")
        report = self.probe({"connect": True, "expressions": ["PLC_PRG.State"]})
        self.assertEqual(report["values"], {"PLC_PRG.State": "INT#30"})
        self.assertEqual(events, [("delay", 500), ("read", "PLC_PRG.State"), ("delay", 1000), ("read", "PLC_PRG.State")])
        self.assert_no_change()

    def test_invalid_monitor_expression_refuses_login(self):
        report = self.probe({"connect": True, "expressions": ["PLC_PRG.State := 0"]})
        self.assertEqual(report["status"], "error")
        self.gateway.find_address_by_ip.assert_not_called()
        self.online_app.login.assert_not_called()
        self.assert_no_change()


if __name__ == "__main__":
    unittest.main()
