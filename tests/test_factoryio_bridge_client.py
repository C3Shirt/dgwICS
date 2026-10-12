"""IPC failure gates. These fixtures never stand in for a live Factory I/O pass."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

path = Path(__file__).resolve().parents[1] / "tools/factoryio_bridge/client.py"
spec = importlib.util.spec_from_file_location("experimental_bridge_client", path)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class BridgeClientTests(unittest.TestCase):
    def test_missing_bridge_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(RuntimeError, "has not initialized"):
                client.call(Path(d), "status")

    def test_timeout_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "requests").mkdir(); (root / "responses").mkdir()
            with self.assertRaises(TimeoutError): client.call(root, "status", timeout=0.06)

    def respond(self, root, change):
        def worker():
            for _ in range(100):
                files = list((root / "requests").glob("*.json"))
                if files:
                    request = json.loads(files[0].read_text(encoding="utf-8"))
                    response = {"schema_version": 1, "protocol": "factoryio-agent-file-bridge/1",
                                "request_id": request["request_id"], "action": request["action"], "ok": True}
                    change(response)
                    temporary = root / "responses/response.tmp"
                    temporary.write_text(json.dumps(response), encoding="utf-8")
                    temporary.rename(root / "responses" / files[0].name)
                    return
                time.sleep(0.005)
        thread = threading.Thread(target=worker)
        thread.start()
        return thread

    def test_response_from_another_request_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "requests").mkdir(); (root / "responses").mkdir()
            thread = self.respond(root, lambda r: r.update(request_id="0" * 32))
            try:
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    client.call(root, "status", timeout=2)
            finally: thread.join()

    def test_changed_capture_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "requests").mkdir(); (root / "responses").mkdir(); (root / "images").mkdir()
            image = root / "images/frame.png"
            image.write_bytes(b"changed file")
            thread = self.respond(root, lambda r: r.update(data={"image_path": str(image), "image_sha256": "0" * 64}))
            try:
                with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                    client.call(root, "capture", timeout=2, size=1)
            finally: thread.join()

    def test_reported_capture_outside_image_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "requests").mkdir(); (root / "responses").mkdir(); (root / "images").mkdir()
            thread = self.respond(root, lambda r: r.update(data={"image_path": str(root / "outside.png"), "image_sha256": "0" * 64}))
            try:
                with self.assertRaisesRegex(RuntimeError, "escaped"):
                    client.call(root, "capture", timeout=2, size=1)
            finally: thread.join()


if __name__ == "__main__": unittest.main()
