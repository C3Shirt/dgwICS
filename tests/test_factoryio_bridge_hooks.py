"""Hook failure tests with explicitly mocked bridge replies, never live passes."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

folder = Path(__file__).resolve().parents[1] / "tools/factoryio_bridge"
sys.path.insert(0, str(folder))
spec = importlib.util.spec_from_file_location("bridge_hooks", folder / "workflow_hook.py")
hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hooks)


class HookFailureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.scene = self.root / "scene.factoryio"
        self.scene.write_text('<FactoryIO><Object><Proxy Key="A"><FinePosition>0</FinePosition>'
                              '<FineRotation>0</FineRotation></Proxy></Object></FactoryIO>')
        self.sha = hooks.digest(self.scene)
        self.request = self.root / "load-request.json"
        self.request.write_text(json.dumps({"schema_version": 1, "request_id": uuid.uuid4().hex,
                                "scene_path": str(self.scene), "scene_sha256": self.sha,
                                "versions": {"scene": self.sha}}))
        (self.root / "manifest.json").write_text(json.dumps({"scene_sha256": self.sha,
            "scene_path": str(self.scene), "objects": [{"key": "A", "prefab": "Belt",
            "position": [1, 2, 3], "rotation": [0, 0, 0, 1]}]}))
        self.receipt = self.root / "receipt.json"
        self.data = {"scene_loading": False, "scene_path": str(self.scene),
                     "loaded_scene_sha256": self.sha,
                     "objects": [{"key": "A", "prefab": "Belt", "position_grid": [1, 2, 3],
                                  "rotation": [0, 0, 0, 1], "fine_position": 0, "fine_rotation": 0}]}

    def test_existing_receipt_cannot_be_reused(self):
        self.receipt.write_text('{}')
        with patch.object(hooks, "call") as call:
            with self.assertRaises(FileExistsError):
                hooks.load(self.root, self.request, self.receipt)
            call.assert_not_called()

    def test_matching_file_path_cannot_hide_different_geometry(self):
        self.data["objects"][0]["position_grid"][1] = 10
        with patch.object(hooks, "call", return_value={"data": self.data}):
            with self.assertRaisesRegex(RuntimeError, "live objects differ"):
                hooks.load(self.root, self.request, self.receipt)
        self.assertFalse(self.receipt.exists())

    def test_request_changed_during_load_cannot_get_confirmation(self):
        def mutate(*args, **kwargs):
            request = json.loads(self.request.read_text())
            request["request_id"] = uuid.uuid4().hex
            self.request.write_text(json.dumps(request))
            return {"data": self.data}
        with patch.object(hooks, "call", side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError, "request version differs"):
                hooks.load(self.root, self.request, self.receipt)
        self.assertFalse(self.receipt.exists())

    def test_capture_cannot_overwrite_previous_evidence(self):
        output = self.root / "frame.png"
        output.write_bytes(b"previous evidence")
        with patch.object(hooks, "call") as call:
            with self.assertRaises(FileExistsError):
                hooks.capture(self.root, output)
            call.assert_not_called()
        self.assertEqual(output.read_bytes(), b"previous evidence")

    def test_capture_frames_scene_and_retains_camera_receipt(self):
        image = self.root/'native.png'; image.write_bytes(b'mocked native PNG')
        response = {'data':{'image_path':str(image), 'image_sha256':hooks.digest(image)}}
        camera = {'data':{'camera_fit':{'view':'top','target_world':[1,2,3]}}}
        output = self.root/'frame.png'
        with patch.object(hooks, 'call', side_effect=[camera, response]) as call:
            hooks.capture(self.root, output, 'top')
        self.assertEqual(call.call_args_list[0].args[1], 'camera_fit')
        self.assertEqual(call.call_args_list[0].kwargs['view'], 'top')
        self.assertEqual(json.loads((self.root/'frame.bridge.json').read_text())['camera_receipt'], camera)
        self.assertEqual(output.read_bytes(), image.read_bytes())

    def connection_request(self):
        self.request.write_text(json.dumps({"schema_version": 1, "request_id": uuid.uuid4().hex,
            "scene_path": str(self.scene), "scene_sha256": self.sha, "versions": {"scene": self.sha},
            "host": "127.0.0.1", "port": 502, "unit_id": 1}))
        return {"data": {"driver": "ModbusTCPClient", "scene_path": str(self.scene), "scene_loading": False,
            "app_version": "2.5.8", "modbus": {"connected": True, "host": "127.0.0.1", "port": 502, "unit_id": 1}},
            "request_id":uuid.uuid4().hex,"completed_at": "2026-10-09T00:00:00Z"}

    def test_connection_receipt_uses_observed_driver_endpoint(self):
        response = self.connection_request()
        with patch.object(hooks, "call", return_value=response), patch.object(hooks.time,'sleep'):
            hooks.connect(self.root, self.request, self.receipt)
        self.assertTrue(json.loads(self.receipt.read_text())["connection_confirmed"])

    def test_connection_request_success_cannot_hide_disconnected_or_wrong_endpoint(self):
        for field, value in (("connected", False), ("port", 503), ("unit_id", 2)):
            response = self.connection_request(); response["data"]["modbus"][field] = value
            with patch.object(hooks, "call", return_value=response), patch.object(hooks.time,'sleep'), self.assertRaises(RuntimeError):
                hooks.connect(self.root, self.request, self.receipt)
            self.assertFalse(self.receipt.exists())

    def test_socket_success_does_not_hide_later_io_disconnect(self):
        response = self.connection_request()
        failed = self.connection_request(); failed['data']['modbus']['connected'] = False
        with patch.object(hooks,'call',side_effect=[response,response,failed]), patch.object(hooks.time,'sleep'), \
             self.assertRaisesRegex(RuntimeError,'remain stable'):
            hooks.connect(self.root,self.request,self.receipt)
        self.assertFalse(self.receipt.exists())

    def test_remote_connection_is_rejected_before_bridge_call(self):
        self.connection_request()
        request = json.loads(self.request.read_text()); request["host"] = "192.0.2.1"
        self.request.write_text(json.dumps(request))
        with patch.object(hooks, "call") as call, self.assertRaises(ValueError):
            hooks.connect(self.root, self.request, self.receipt)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
