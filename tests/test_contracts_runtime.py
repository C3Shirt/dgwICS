"""Failure and provenance tests for contracts, live tag reconciliation and acceptance."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import Mock, patch

from factoryio_agents.agents import VisionAgent
from factoryio_agents.contracts import ContractError, file_hash, validate_feedback, validate_task
from factoryio_agents.demo import demo_task
from factoryio_agents.runtime import FactoryIOClient, reconcile_tags
from factoryio_agents.validation import events_from_single_item_trace, validate_events


PROTOCOL = {"name": "modbus_tcp", "host": "127.0.0.1", "port": 502, "unit_id": 1,
            "factoryio_role": "client", "plc_role": "server"}
VERSIONS = {"task": "t", "scene": "s", "io": "i", "plc": "p", "config": "c"}


class ContractTests(unittest.TestCase):
    def test_planner_cannot_hide_invalid_protocol_or_overlapping_weight_bins(self):
        task = demo_task(PROTOCOL)
        self.assertEqual(validate_task(task), task)
        for mutator in (lambda t: t["protocol"].update(port=0),
                        lambda t: t["acceptance"][0]["bins"][1].update(lower_kg=2),
                        lambda t: t["acceptance"][0]["bins"][0].update(upper_kg=float("nan"))):
            bad = deepcopy(task); mutator(bad)
            with self.assertRaises(ContractError): validate_task(bad)

    def test_stale_wrong_target_and_invented_evidence_feedback_are_rejected(self):
        feedback = {"versions": VERSIONS, "phase": "scene", "verdict": "repair", "findings": [
            {"category": "geometry", "target": "scene", "object_ids": ["scale"], "observed": "gap",
             "expected": "connected", "evidence_ids": ["frame"], "confidence": 0.8}]}
        manifest = {"objects": [{"id": "scale"}]}
        self.assertEqual(validate_feedback(feedback, VERSIONS, manifest, {"frame"}), feedback)
        for mutator in (lambda f: f.update(versions={}), lambda f: f["findings"][0].update(target="plc"),
                        lambda f: f["findings"][0].update(evidence_ids=["invented"])):
            bad = deepcopy(feedback); mutator(bad)
            with self.assertRaises(ContractError): validate_feedback(bad, VERSIONS, manifest, {"frame"})

    def test_vision_refuses_changed_images_before_model_call(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "frame.png"; path.write_bytes(b"captured")
            record = {"id": "frame", "kind": "image", "path": str(path), "sha256": file_hash(path)}
            path.write_bytes(b"different")
            class NoCall:
                def complete(self, *args, **kwargs): raise AssertionError("model must not be called")
            with self.assertRaises(ContractError):
                VisionAgent(NoCall()).review({}, {"objects": []}, {}, {"versions": VERSIONS, "records": [record]},
                                            VERSIONS, "scene")


class RuntimeTests(unittest.TestCase):
    def test_running_timeout_retains_observed_system_state_before_cleanup(self):
        client = FactoryIOClient({})
        rows = [{'id':'running','name':'FACTORY I/O (Running)','kind':'Input','value':False,'isForced':False},
                {'id':'run','name':'FACTORY I/O (Run)','kind':'Output','value':True,'isForced':False}]
        client.tags = Mock(return_value=rows)
        with patch('factoryio_agents.runtime.time.monotonic',side_effect=[0,6]):
            with self.assertRaises(ContractError) as caught: client.wait_running(True)
        self.assertEqual(caught.exception.diagnostics['system_tags'],rows)
        self.assertTrue(caught.exception.diagnostics['expected_running'])
        client.tags.assert_called_once()

    def test_reconcile_uses_keys_and_direction_not_ambiguous_names_or_scene_addresses(self):
        contract = {"tags": [{"key": "1", "name": "Motor", "object_id": "motor", "kind": "output", "data_type": "bool"},
                             {"key": "2", "name": "RunInput", "object_id": None, "kind": "input", "data_type": "bool"}]}
        actual = [{"id": "1", "name": "Motor", "kind": "Output", "type": "Bit", "address": 99},
                  {"id": "2", "name": "FACTORY I/O (Running)", "kind": "Input", "type": "Bit"}]
        self.assertTrue(reconcile_tags(contract, actual)["passed"])
        self.assertFalse(reconcile_tags(contract, actual[:1])["passed"])
        wrong = deepcopy(actual); wrong[0]["kind"] = "Input"
        self.assertFalse(reconcile_tags(contract, wrong)["passed"])

    def test_http_200_with_tag_error_is_not_success(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps([{"id": "1", "code": 406}]).encode())
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            client = FactoryIOClient({"web_api_url": f"http://127.0.0.1:{server.server_port}"})
            with self.assertRaises(ContractError): client.tags()
        finally: server.shutdown(); server.server_close(); thread.join()


class AcceptanceTests(unittest.TestCase):
    def events(self):
        rows = []
        for i, (weight, exit_) in enumerate(((1, "left"), (3, "front"), (6, "right"))):
            rows += [{"kind": "item_event", "event": "weighed", "item_id": str(i), "weight_kg": weight,
                      "elapsed_seconds": i * 10, "measurement_verified": True},
                     {"kind": "item_event", "event": "exited", "item_id": str(i), "exit": exit_,
                      "elapsed_seconds": i * 10 + 5, "tracking_verified": True}]
        samples = []
        for i, event in enumerate(rows):
            event["timestamp"] = "2026-10-08T00:00:00+00:00"
            event["source_record_ids"] = [f"raw-{i}"]
            samples.append({"id": f"raw-{i}", "kind": "tag_sample", "tags": []})
        return {"mode": "live", "versions": VERSIONS, "records": rows + samples}

    def test_boundaries_route_to_next_bin_and_offline_never_claims_live_pass(self):
        evidence = self.events()
        self.assertTrue(validate_events(demo_task(PROTOCOL), evidence, VERSIONS)["passed"])
        evidence["mode"] = "offline_demo"
        result = validate_events(demo_task(PROTOCOL), evidence, VERSIONS)
        self.assertTrue(result["checks_passed"]); self.assertFalse(result["passed"])

    def test_wrong_exit_duplicate_count_timeout_and_unverified_measurement_fail(self):
        mutations = [lambda e: e["records"][1].update(exit="right"),
            lambda e: e["records"].append(deepcopy(e["records"][1])),
            lambda e: e["records"][1].update(elapsed_seconds=50),
            lambda e: e["records"][0].update(measurement_verified=False)]
        for mutate in mutations:
            evidence = self.events(); mutate(evidence)
            self.assertFalse(validate_events(demo_task(PROTOCOL), evidence, VERSIONS)["passed"])

    def test_event_sequence_requires_time_and_measured_source_provenance(self):
        task = demo_task(PROTOCOL); task["acceptance"] = [{"kind": "event_sequence", "events": ["start", "finish"]}]
        evidence = {"mode": "live", "versions": VERSIONS, "records": [
            {"kind": "item_event", "event": "start"}, {"kind": "item_event", "event": "finish"}]}
        self.assertFalse(validate_events(task, evidence, VERSIONS)["passed"])
        for i, event in enumerate(evidence["records"]):
            event.update(elapsed_seconds=i, timestamp="2026-10-08T00:00:00+00:00",
                         source_record_ids=["observed"], provenance_verified=True)
        evidence["records"].append({"id": "observed", "kind": "tag_sample", "tags": []})
        self.assertTrue(validate_events(task, evidence, VERSIONS)["passed"])

    def test_simultaneous_exit_edges_are_preserved_as_unverified_conflict(self):
        config = {"single_item_confirmed": True, "calibration_confirmed": True,
            "weight_tag_key": "w", "at_scale_tag_key": "s", "kg_per_raw_unit": 1, "stable_samples": 2,
            "exits": [{"id": "left", "tag_key": "l"}, {"id": "front", "tag_key": "f"}]}
        rows = []
        for i, values in enumerate(((0, False, False, False), (1, True, False, False),
                                   (1, True, False, False), (0, False, True, True))):
            rows.append({"id": f"sample-{i}", "kind": "tag_sample", "elapsed_seconds": i,
                "timestamp": "2026-10-08T00:00:00+00:00", "tags": [
                    {"id": k, "value": v, "isForced": False} for k, v in zip(("w", "s", "l", "f"), values)]})
        events = events_from_single_item_trace({"records": rows}, config)
        exits = [event for event in events if event["event"] == "exited"]
        self.assertEqual(len(exits), 2)
        self.assertTrue(all(event["tracking_verified"] is False for event in exits))
        self.assertFalse(validate_events(demo_task(PROTOCOL),
            {"mode": "live", "versions": VERSIONS, "records": rows + events}, VERSIONS)["passed"])

    def test_raw_tags_and_images_do_not_prove_item_routing(self):
        evidence = {"mode": "live", "versions": VERSIONS, "records": [{"kind": "tag_sample", "tags": []}]}
        self.assertFalse(validate_events(demo_task(PROTOCOL), evidence, VERSIONS)["passed"])

    def test_single_item_decoder_requires_calibration_and_stable_measurements(self):
        config = {"single_item_confirmed": True, "calibration_confirmed": True, "weight_tag_key": "weight",
                  "at_scale_tag_key": "scale", "exits": [{"id": "left", "tag_key": "exit"}],
                  "kg_per_raw_unit": 2, "stable_samples": 3}
        records = []
        for i, (weight, scale, exit_) in enumerate(((0, False, False), (1, True, False),
            (2, True, False), (2, True, False), (2, True, False), (0, False, True))):
            records.append({"id": f"sample-{i}", "kind": "tag_sample", "elapsed_seconds": i,
                            "timestamp": "2026-10-08T00:00:00+00:00", "tags": [
                {"id": "weight", "value": weight}, {"id": "scale", "value": scale}, {"id": "exit", "value": exit_}]})
        for row in records:
            for tag in row["tags"]: tag["isForced"] = False
        events = events_from_single_item_trace({"records": records}, config)
        self.assertEqual([e["event"] for e in events], ["weighed", "exited"])
        self.assertEqual(events[0]["weight_kg"], 4)
        records[2]["tags"][0]["isForced"] = True
        with self.assertRaises(ContractError): events_from_single_item_trace({"records": records}, config)
        records[2]["tags"][0].pop("isForced")
        with self.assertRaises(ContractError): events_from_single_item_trace({"records": records}, config)
        config["calibration_confirmed"] = False
        with self.assertRaises(ContractError): events_from_single_item_trace({"records": records}, config)


if __name__ == "__main__": unittest.main()
