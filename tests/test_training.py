"""Observable trajectories and human-review gates; no real providers or simulators are called."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import urllib.error

from factoryio_agents.contracts import ContractError, file_hash, read_json, write_json
from factoryio_agents.llm import ModelClient
from factoryio_agents.training import RecordingClient, Redactor, TrainingRecorder, json_text
from factoryio_agents.training_export import export_reviewed, REQUIRED_GATES
from factoryio_agents.workflow import Workflow


class FakeProvider:
    def __init__(self, value):
        self.value = value
        self.last_images = []
    def complete(self, role, system, payload, images=None):
        self.last_images = images or []
        if isinstance(self.value, Exception):
            raise self.value
        return self.value
    def exchange_metadata(self):
        return {"raw_content": json_text(self.value) if not isinstance(self.value, Exception) else "broken JSON",
                "requested_model": "unit-test-provider", "http_status": 200,
                "Authorization": "Bearer never-save-this", "reasoning_content": "never-save-internal-trace"}


class TrainingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw = self.root / "raw"
        self.recorder = TrainingRecorder(self.raw, scenario_id="unit-test", lineage_group="family-original")
        self.evidence = self.root / "evidence.json"
        write_json(self.evidence, {"fixture": True, "result": "passed"})
        self.document = {"schema_version": 1, "purpose": "human_training_review", "candidate_reviews": [], "preference_reviews": []}

    def candidate(self, value=None, payload=None, *, role="planner", images=None, recorder=None):
        recorder = recorder or self.recorder
        provider = FakeProvider(value or {"answer": "first"})
        RecordingClient(provider, recorder).complete(role, "system", payload or {"requirement": "sort"}, images)
        return read_json(recorder.directory / "candidates" / (recorder.last_candidate_id + ".json")), provider

    def human(self):
        return {"reviewer_type": "human", "reviewer_id": "unit-test-reviewer", "reviewed_at": "2026-10-10T00:00:00Z",
                "rationale": "Controlled fixture exercises exporter; this is not project training gold.",
                "privacy_review_passed": True, "source_use_permitted": True}

    def review(self, record, accepted=True, **overrides):
        gates = {key: {"path": str(self.evidence), "sha256": file_hash(self.evidence), "result": "passed"}
                 for key in REQUIRED_GATES[record["role"]]}
        if not accepted:
            gates = {"observed_failure": {"path": str(self.evidence), "sha256": file_hash(self.evidence), "result": "failed"}}
        value = {**self.human(), "candidate_id": record["candidate_id"], "prompt_sha256": record["prompt_sha256"],
                 "response_sha256": record["response_sha256"], "lineage_group": "family-original", "split": "train",
                 "status": "accepted" if accepted else "rejected", "gates": gates, **overrides}
        self.document["candidate_reviews"].append(value)
        return value

    def pair(self, chosen, rejected, **overrides):
        value = {**self.human(), "pair_id": "fixture-pair", "chosen_id": chosen["candidate_id"],
                 "rejected_id": rejected["candidate_id"], "same_evaluation_conditions": True,
                 "evaluation_context": {"path": str(self.evidence), "sha256": file_hash(self.evidence)}, **overrides}
        self.document["preference_reviews"].append(value)
        return value

    def export(self):
        review_path = self.root / "review.json"
        write_json(review_path, self.document)
        return export_reviewed([self.raw], review_path, self.root / "export")

    def test_raw_capture_never_auto_accepts_and_never_saves_headers_or_internal_reasoning(self):
        record, _ = self.candidate()
        self.assertFalse(record["quality"]["sft_eligible"])
        self.assertEqual(record["quality"]["review_status"], "pending")
        saved = json_text(record)
        self.assertNotIn("never-save", saved)
        self.assertEqual(record["output_origin"], "provider_content")

    def captured_visual_candidate(self):
        from factoryio_agents.coordination import make_handoff
        from factoryio_agents.prompts import policy_manifest
        source=self.root/'task.json'; write_json(source,{'requirement':'Inspect a conveyor.'})
        handoff=make_handoff('orchestrator','vision','scene_review',{'task':'fixture-task'},[source],
                            'scene-feedback.json',[{'actual_position_grid':[91,92,93]}])
        context={'handoff':handoff,'versions':handoff['versions'],'policy':policy_manifest(),'task_id':'fixture-task'}
        with self.recorder.capture_context(context):
            record,_=self.candidate(role='vision',payload={'phase':'scene','layout_review':{'scene_goal':'Inspect images'}})
        return record

    def test_external_audit_context_preserves_export_without_entering_model_text(self):
        record=self.captured_visual_candidate()
        self.assertNotIn('actual_position_grid',json.dumps(record['messages']))
        self.assertTrue(record['input_artifacts'])
        self.review(record)
        self.assertEqual(self.export()['sft_rows'],1)  # Explicitly human-reviewed unit fixture only.
        next_record,_=self.candidate()
        self.assertNotIn('recording_context',next_record)

    def test_modified_external_audit_context_blocks_training_export(self):
        record=self.captured_visual_candidate(); self.review(record)
        path=self.raw/'candidates'/(record['candidate_id']+'.json')
        record['recording_context']['handoff']['inputs']=[]
        write_json(path,record)
        with self.assertRaisesRegex(ContractError,'recording context changed'):
            self.export()

    def test_known_and_embedded_credentials_redacted_but_item_identity_preserved(self):
        redactor = Redactor(["fixture-device-password"])
        value = redactor.clean({"API_Key": "example", "item_token": "ITEM-1", "text":
            '{"PLC_password":"abc","secret":"xyz"} fixture-device-password Bearer abc.def'})
        self.assertEqual(value["item_token"], "ITEM-1")
        self.assertNotIn("abc", json_text(value))
        self.assertNotIn("xyz", json_text(value))
        self.assertNotIn("fixture-device-password", json_text(value))
        self.assertGreater(redactor.count, 0)

    def test_invalid_provider_json_is_retained_before_parser_error(self):
        client = ModelClient({"base_url": "https://fixture.invalid/v1", "models": {"planner": "fixture"}})
        response = io.StringIO(json_text({"choices": [{"message": {"content": "not valid JSON"}}]}))
        client.opener = Mock(); client.opener.open.return_value = response
        with self.assertRaises(ContractError):
            RecordingClient(client, self.recorder).complete("planner", "system", {"requirement": "sort"})
        record = read_json(next((self.raw / "candidates").glob("*.json")))
        self.assertEqual(record["response_text"], "not valid JSON")
        self.assertEqual(record["status"], "call_error")
        self.assertIsNone(record["structured_output"])

    def test_nested_credentials_in_raw_json_and_fenced_json_are_redacted(self):
        for text in ('{"credentials":{"token":"opaque-fixture-value"}}',
                     '```json\n{"credentials":{"token":"opaque-fixture-value"}}\n```'):
            redactor = Redactor()
            cleaned = redactor.clean(text)
            self.assertNotIn("opaque-fixture-value", cleaned)
            self.assertGreater(redactor.count, 0)

    def test_http_failure_has_status_but_no_fake_completion(self):
        client = ModelClient({"base_url": "https://fixture.invalid/v1", "models": {"planner": "fixture"}})
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError("fixture", 401, "fixture", {}, None)
        with self.assertRaises(ContractError):
            RecordingClient(client, self.recorder).complete("planner", "system", {})
        record = read_json(next((self.raw / "candidates").glob("*.json")))
        self.assertEqual(record["model_metadata"]["http_status"], 401)
        self.assertIsNone(record["response_text"])

    def test_nonfinite_json_reply_is_retained_as_invalid_candidate(self):
        client = ModelClient({"base_url": "https://fixture.invalid/v1", "models": {"planner": "fixture"}})
        client.opener = Mock()
        client.opener.open.return_value = io.StringIO(json_text({"choices": [{"message": {"content": '{"value": NaN}'}}]}))
        with self.assertRaises(ContractError):
            RecordingClient(client, self.recorder).complete("planner", "system", {})
        record = read_json(next((self.raw / "candidates").glob("*.json")))
        self.assertEqual(record["response_text"], '{"value": NaN}')
        self.assertEqual(record["status"], "call_error")

    def test_real_agent_prepare_path_records_handoffs_without_exporting_fixture_gold(self):
        from test_coordination import source_fixture, task_fixture
        from test_contracts_runtime import PROTOCOL
        from factoryio_agents.scene import build_catalog
        source = self.root / "source.factoryio"; source.write_text(source_fixture(), encoding="utf-8")
        catalog = build_catalog([source])
        task = task_fixture()
        plan = {"scene_id": task["task_id"], "base_source_id": catalog["sources"][0]["id"],
                "objects": [{"id": key, "template_id": template["template_id"]}
                            for key, template in zip(("emitter", "conveyor"), catalog["templates"])]}
        class PreparedClient:
            def complete(self, role, system, payload):
                from test_layout_design import proposal_fixture
                if payload.get('scene_stage')=='layout_proposal': result=proposal_fixture(task)
                elif payload.get('planning_stage')=='layout_review':
                    result={'verdict':'approved','summary':'Mocked design review.','findings':[]}
                else: result=task if role=='planner' else plan
                return {**deepcopy(result), "handoff_id": payload["handoff"]["id"]}
        workflow = Workflow({"llm": {}, "scene_sources": [str(source)], "protocol": PROTOCOL,
                             "codesys": {}, "factoryio": {}}, self.root / "workflow", PreparedClient())
        self.assertEqual(workflow.prepare("offline fixture")["status"], "awaiting_scene_review")
        records = [read_json(path) for path in (workflow.training.directory / "candidates").glob("*.json")]
        self.assertEqual({record["role"] for record in records}, {"planner", "scene"})
        self.assertTrue(all(record["handoff_id"] and record["input_artifacts"] for record in records))
        self.assertTrue(all(record["source_kind"] == "injected_client" for record in records))

    def test_changed_image_snapshot_cannot_pass_export_hash_check(self):
        image = self.root / "view.png"; image.write_bytes(b"fixture")
        record, _ = self.candidate(role="vision", images=[image]); self.review(record)
        (self.raw / record["images"][0]["path"]).write_bytes(b"modified")
        with self.assertRaisesRegex(ContractError, "hash changed"):
            self.export()

    def test_image_bytes_used_by_provider_are_frozen_and_exportable(self):
        image = self.root / "view.png"; image.write_bytes(b"controlled-test-image")
        record, provider = self.candidate(role="vision", images=[image])
        self.assertNotEqual(provider.last_images[0], image)
        image.write_bytes(b"changed-original")
        self.review(record)
        self.assertEqual(self.export()["sft_rows"], 1)
        row = json.loads((self.root / "export" / "sft-train.jsonl").read_text())
        self.assertEqual((self.root / "export" / row["images"][0]).read_bytes(), b"controlled-test-image")

    def test_unreviewed_raw_and_injected_workflow_clients_do_not_become_gold(self):
        config = {"llm": {}, "training": {"scenario_id": "fixture"}}
        workflow = Workflow(config, self.root / "workflow", FakeProvider({"a": 1}))
        workflow.planner.client.complete("planner", "system", {})
        record = read_json(next((workflow.training.directory / "candidates").glob("*.json")))
        self.assertEqual(record["source_kind"], "injected_client")
        self.candidate()
        self.assertEqual(self.export()["sft_rows"], 0)

    def test_training_can_be_disabled(self):
        workflow = Workflow({"llm": {}, "training": {"enabled": False}}, self.root / "workflow", FakeProvider({"a": 1}))
        self.assertIsNone(workflow.training)
        self.assertFalse(workflow.state["training_collection"]["enabled"])

    def test_reviewed_sft_and_same_prompt_dpo_export(self):
        chosen, _ = self.candidate({"answer": "correct"})
        rejected, _ = self.candidate({"answer": "incorrect"})
        self.review(chosen); self.review(rejected, False); self.pair(chosen, rejected)
        result = self.export()
        self.assertEqual((result["sft_rows"], result["dpo_pairs"]), (1, 1))
        row = json.loads((self.root / "export" / "dpo-train.jsonl").read_text())
        self.assertEqual(row["prompt"], chosen["messages"])
        self.assertIn("correct", row["chosen"][0]["content"])

    def test_repair_feedback_changes_prompt_and_cannot_make_dpo_pair(self):
        rejected, _ = self.candidate({"answer": "incorrect"})
        chosen, _ = self.candidate({"answer": "correct"}, {"requirement": "sort", "feedback": "fix position"})
        self.review(chosen); self.review(rejected, False); self.pair(chosen, rejected)
        with self.assertRaisesRegex(ContractError, "same input context"):
            self.export()
        self.assertFalse((self.root / "export").exists())

    def test_even_json_field_order_is_part_of_exact_input_identity(self):
        first, _ = self.candidate(payload={"a": 1, "b": 2})
        second, _ = self.candidate(payload={"b": 2, "a": 1})
        self.assertNotEqual(first["prompt_sha256"], second["prompt_sha256"])

    def test_same_lineage_cannot_cross_splits(self):
        first, _ = self.candidate(); second, _ = self.candidate({"answer": "second"})
        self.review(first); self.review(second, split="test")
        with self.assertRaisesRegex(ContractError, "crosses dataset splits"):
            self.export()

    def test_fixture_cannot_be_reviewed_into_gold(self):
        recorder = TrainingRecorder(self.raw, source_kind="fixture")
        record, _ = self.candidate(recorder=recorder)
        self.review(record)
        with self.assertRaisesRegex(ContractError, "fixtures"):
            self.export()

    def test_model_self_review_is_rejected(self):
        record, _ = self.candidate(); self.review(record, reviewer_type="model")
        with self.assertRaisesRegex(ContractError, "human reviewer"):
            self.export()

    def test_stale_review_hash_is_rejected(self):
        record, _ = self.candidate(); self.review(record, response_sha256="old")
        with self.assertRaisesRegex(ContractError, "stale"):
            self.export()

    def test_changed_raw_output_is_rejected(self):
        record, _ = self.candidate(); self.review(record)
        record["response_text"] = "changed"
        write_json(self.raw / "candidates" / (record["candidate_id"] + ".json"), record)
        with self.assertRaisesRegex(ContractError, "response changed"):
            self.export()

    def test_changed_evidence_file_is_rejected(self):
        record, _ = self.candidate(); self.review(record)
        self.evidence.write_text("changed")
        with self.assertRaisesRegex(ContractError, "hash changed"):
            self.export()

    def test_changed_input_artifact_is_rejected(self):
        input_path = self.root / "task.json"; input_path.write_text("original")
        record, _ = self.candidate(payload={"handoff": {"inputs": [{"path": str(input_path), "sha256": file_hash(input_path)}]}})
        self.review(record); input_path.write_text("modified")
        with self.assertRaisesRegex(ContractError, "hash changed"):
            self.export()

    def test_plc_compile_alone_is_insufficient_for_full_program_sft(self):
        record, _ = self.candidate(role="plc")
        review = self.review(record)
        review["gates"] = {"plc_compile": review["gates"]["plc_compile"]}
        with self.assertRaisesRegex(ContractError, "role gates"):
            self.export()

    def test_redacted_response_cannot_be_exported_under_original_hash(self):
        record, _ = self.candidate({"password": "fixture"}); self.review(record)
        with self.assertRaisesRegex(ContractError, "redacted"):
            self.export()

    def test_identical_answers_are_not_preference_pairs(self):
        first, _ = self.candidate(); second, _ = self.candidate()
        self.review(first); self.review(second); self.pair(first, second)
        with self.assertRaisesRegex(ContractError, "completions must differ"):
            self.export()

    def test_preference_requires_comparable_evaluation_conditions(self):
        first, _ = self.candidate(); second, _ = self.candidate({"answer": "second"})
        self.review(first); self.review(second, False); self.pair(first, second, same_evaluation_conditions=False)
        with self.assertRaisesRegex(ContractError, "matching evaluation conditions"):
            self.export()

    def test_output_directory_is_never_overwritten(self):
        self.candidate(); self.export()
        with self.assertRaisesRegex(ContractError, "already exists"):
            self.export()


if __name__ == "__main__":
    unittest.main()
