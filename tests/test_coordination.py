"""Policy/coordination tests. All model replies and runtime adapters here are mocked."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from factoryio_agents.agents import PlannerAgent, VisionAgent, catalog_for_model, complete_role
from factoryio_agents.contracts import ContractError, canonical_hash, file_hash, read_json, write_json
from factoryio_agents.coordination import (artifact_digest, attach_component_semantics, check_process_scene,
    make_handoff, select_review_evidence, model_review_evidence, validate_diagnoses, validate_execution_contract)
from factoryio_agents.demo import demo_program
from factoryio_agents.prompts import PROMPTS, VISION_PROMPTS, policy_manifest
from factoryio_agents.vision_prompts import build_layout_prompt
from factoryio_agents.vision_layout import layout_task_hash
from factoryio_agents.scene import build_catalog, build_scene
from factoryio_agents.runtime import FactoryIOClient
from factoryio_agents.workflow import Workflow
from test_scene import FIXTURE
from test_contracts_runtime import PROTOCOL, VERSIONS


def task_fixture():
    return {"schema_version": 1, "task_id": "paced-test", "requirement": "mock single-item conveyor",
        "scenario_type": "conveyor", "objects": [{"id": "emitter", "prefab": "Emitter", "purpose": "feed"},
            {"id": "conveyor", "prefab": "RollerConveyor6M", "purpose": "move"}],
        "process": [{"id": "feed", "description": "emit one"}, {"id": "exit", "description": "confirm exit"}],
        "control_rules": ["one item at a time"], "assumptions": [], "unresolved": [], "protocol": PROTOCOL,
        "acceptance": [{"kind": "event_sequence", "events": ["emitted", "exited"]}],
        "execution_contract": {"tracking_mode": "single_item", "max_in_flight": 1,
            "feed": {"object_id": "emitter", "control": "plc", "items_per_enable": 1},
            "startup": ["explicit operator start"], "shutdown": ["emit off"], "steps": [
                {"process_id": id_, "preconditions": ["guard"], "commands": ["command"],
                 "completion": ["measured transition"], "timeout_seconds": 10} for id_ in ("feed", "exit")]}}


def source_fixture():
    root = ET.fromstring(FIXTURE)
    emitter = root.findall("Object")[0]
    emitter.find("Proxy").set("PrefabName", "Emitter")
    emitter.find("OperatingMode").set("Description", "$C_Emitter_OM_Default_NAME")
    tag = emitter.find("OperatingMode/GroupIO/BinaryInput")
    tag.tag = "BinaryOutput"; tag.set("Name", "Emitter")
    emitter.remove(emitter.find("Sensor"))
    ET.SubElement(emitter, "Emitter", MinTimeToEmit="1", MaxTimeToEmit="2", UpTo="1",
                  RandomPartPosition="False", RandomPartOrientation="False")
    driver = root.find("Drivers/ModbusTCPClient")
    driver.remove(driver.find("BitInput0"))
    ET.SubElement(driver, "BitOutput1", PointIOKey=tag.get("Key"))
    driver.find("PointIOCount").set("BitOutputCount", "2")
    root.find("Drivers").remove(root.find("Drivers/OtherDriver"))
    return ET.tostring(root, encoding="unicode")


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        source = self.root / "source.factoryio"; source.write_text(source_fixture(), encoding="utf-8")
        self.catalog = build_catalog([source]); self.task = task_fixture()
        self.plan = {"scene_id": self.task["task_id"], "base_source_id": self.catalog["sources"][0]["id"],
            "objects": [{"id": id_, "template_id": t["template_id"]} for id_, t in
                        zip(("emitter", "conveyor"), self.catalog["templates"])]}
        self.built = build_scene(self.plan, self.catalog, self.root / "scene.factoryio")

    def test_process_contract_rejects_unenforced_single_item_and_missing_step(self):
        validate_execution_contract(self.task)
        for change in (lambda c: c.update(max_in_flight=2), lambda c: c["feed"].update(control="scene"),
                       lambda c: c["steps"].pop(), lambda c: c["steps"][0].update(timeout_seconds=0)):
            bad = deepcopy(self.task); change(bad["execution_contract"])
            with self.assertRaises(ContractError): validate_execution_contract(bad)

    def test_scene_gate_rejects_continuous_forced_and_unmapped_feeding(self):
        manifest, contract = self.built["manifest"], self.built["io_contract"]
        self.assertTrue(check_process_scene(self.task, manifest, contract)["passed"])
        for mutation in ("continuous", "forced", "unmapped"):
            m, c = deepcopy(manifest), deepcopy(contract)
            emitter = m["objects"][0]
            if mutation == "continuous":
                next(n for n in emitter["configuration"]["nodes"] if n["element"] == "Emitter")["attributes"]["UpTo"] = "0"
            elif mutation == "forced": emitter["tags"][0]["use_forced_value"] = True
            else: c["mappings"] = [r for r in c["mappings"] if r["tag_key"] != emitter["tags"][0]["key"]]
            self.assertFalse(check_process_scene(self.task, m, c)["passed"], mutation)

    def test_scene_model_sees_actual_emitter_limits_and_operating_mode(self):
        template = catalog_for_model(self.catalog)["templates"][0]
        self.assertEqual(template["component_context"]["operating_mode"], "$C_Emitter_OM_Default_NAME")
        self.assertEqual(template["component_context"]["settings"][0]["attributes"]["UpTo"], "1")

    def test_semantics_are_bound_to_keys_and_version_not_renamed_labels(self):
        m, c = self.built["manifest"], self.built["io_contract"]
        m["objects"][0]["tags"][0]["name"] = "another label"
        attach_component_semantics(m, c, "2.5.8")
        row = c["component_semantics"][0]
        self.assertEqual(row["tag_key"], m["objects"][0]["tags"][0]["key"])
        self.assertTrue(row["app_version_confirmed"])
        attach_component_semantics(m, c, "2.6.0")
        self.assertFalse(c["component_semantics"][0]["app_version_confirmed"])
        m["objects"][0]["configuration"]["nodes"][1]["attributes"]["Description"] = "unknown mode"
        attach_component_semantics(m, c, "2.5.8")
        self.assertEqual(c["component_semantics"], [])

    def test_handoff_rejects_wrong_reply_and_changed_input_before_or_during_call(self):
        input_path = self.root / "task.json"; write_json(input_path, self.task)
        packet = make_handoff("orchestrator", "scene", "authoring", VERSIONS, [input_path], "plan")
        client = Mock(); client.complete.return_value = {"handoff_id": "old"}
        with self.assertRaisesRegex(ContractError, "another handoff"):
            complete_role(client, "scene", PROMPTS["scene"], {}, packet)
        client.complete.side_effect = lambda *args: (write_json(input_path, {"changed": True}) or {"handoff_id": packet["id"]})
        with self.assertRaisesRegex(ContractError, "during model call"):
            complete_role(client, "scene", PROMPTS["scene"], {}, packet)
        client.reset_mock()
        with self.assertRaisesRegex(ContractError, "before model call"):
            complete_role(client, "scene", PROMPTS["scene"], {}, packet)
        client.complete.assert_not_called()

    def test_new_notes_or_handoff_id_do_not_count_as_a_plc_repair(self):
        program = demo_program(self.built["io_contract"])
        changed = {**program, "handoff_id": "new", "notes": ["fixed now"], "unresolved": []}
        self.assertEqual(artifact_digest(program), artifact_digest(changed))

    def test_handoff_rejects_changed_payload_even_when_file_hash_is_correct(self):
        input_path = self.root / "task.json"; write_json(input_path, self.task)
        packet = make_handoff("orchestrator", "scene", "authoring", {**VERSIONS, "task": canonical_hash(self.task)}, [input_path], "plan")
        client = Mock()
        changed = {**self.task, "requirement": "other requirement"}
        with self.assertRaisesRegex(ContractError, "another artifact version"):
            complete_role(client, "scene", PROMPTS["scene"], {"task": changed}, packet)
        client.complete.assert_not_called()

    def test_planner_requires_execution_contract_for_actual_model_calls(self):
        bad = deepcopy(self.task); bad.pop("execution_contract")
        client = Mock(); client.complete.return_value = bad
        with self.assertRaisesRegex(ContractError, "execution_contract"):
            PlannerAgent(client).plan("test", self.catalog, PROTOCOL)

    def test_planner_reviews_invented_blocker_with_original_requirement(self):
        initial = deepcopy(self.task); initial['unresolved'] = ['optional production statistics unspecified']
        corrected = deepcopy(self.task); corrected['unresolved'] = []
        client = Mock(); client.complete.side_effect = [initial, corrected]
        task = PlannerAgent(client).plan('single conveyor, no production statistics', self.catalog, PROTOCOL)
        self.assertEqual(task['unresolved'], [])
        self.assertEqual(client.complete.call_count, 2)
        payload = client.complete.call_args.args[2]
        self.assertEqual(payload['feedback']['phase'], 'planning_relevance')
        self.assertEqual(payload['requirement'], 'single conveyor, no production statistics')
        self.assertEqual(payload['previous_task'], initial)

    def test_planner_relevance_review_does_not_erase_persistent_critical_gap(self):
        task = deepcopy(self.task); task['unresolved'] = ['required measurement capability unavailable']
        client = Mock(); client.complete.return_value = task
        returned = PlannerAgent(client).plan('test', self.catalog, PROTOCOL)
        self.assertEqual(client.complete.call_count, 3)
        self.assertEqual(returned['unresolved'], task['unresolved'])

    def test_reordered_sorter_groups_cannot_assign_incorrect_output_semantics(self):
        sorter = {"id": "sorter", "prefab": "PopUpWheelSorter", "configuration": {"nodes": [
            {"element": "ComponentIO", "attributes": {"CurrentOperatingMode": "0"}},
            {"element": "OperatingMode", "attributes": {"Description": "$C_PopUpWheelSorter_OM_SingleBinary_NAME"},
             "children": [{"element": "GroupIO", "attributes": {"Description": group}, "children": [
                 {"element": "BinaryOutput", "attributes": {}}]} for group in ("Right", "Left", "Forward")]}]},
            "tags": [{"index": i, "key": str(i), "kind": "output", "data_type": "bool"} for i in range(3)]}
        manifest, contract = {"objects": [sorter]}, {}
        attach_component_semantics(manifest, contract, "2.5.8")
        self.assertEqual(contract["component_semantics"], [])

    def test_short_sensor_pulse_survives_review_selection(self):
        samples = [{"id": f"s-{i}", "kind": "tag_sample", "tags": [{"id": "s", "name": "sensor",
            "value": i == 51, "isForced": False}]} for i in range(200)]
        chosen = select_review_evidence({"records": samples})
        ids = {r["id"] for r in chosen["records"]}
        self.assertTrue({"s-50", "s-51", "s-52"} <= ids)
        self.assertLessEqual(len(ids), 40)
        self.assertEqual(chosen["review_selection"]["omitted_transition_samples"], 0)

    def test_item_sources_and_late_fault_are_retained_over_soft_budget(self):
        samples = [{"id": f"s-{i}", "kind": "tag_sample", "tags": [{"id": "f", "name": "fault",
            "value": i == 98, "isForced": False}]} for i in range(100)]
        event = {"id": "item", "kind": "item_event", "source_record_ids": [f"s-{i}" for i in range(70)]}
        chosen = select_review_evidence({"records": samples + [event]})
        ids = {r["id"] for r in chosen["records"]}
        self.assertTrue(set(event["source_record_ids"]) <= ids)
        self.assertTrue({"s-97", "s-98", "s-99"} <= ids)
        self.assertGreater(chosen["review_selection"]["selected_samples"], 40)

    def test_model_signal_view_preserves_metrics_faults_pulses_and_original_evidence(self):
        rows=[{'id':f's-{i}','kind':'tag_sample','elapsed_seconds':i*.1,'tags':[
            {'id':'sensor','name':'sensor with repeated metadata','value':i==51,'isForced':i==98,
             'openCircuit':False,'shortCircuit':False,'type':'Bool','static_metadata':'x'*400}]} for i in range(100)]
        refs=[r['id'] for r in rows]
        acceptance={'id':'acceptance','kind':'acceptance_result','source_record_ids':refs,
            'result':{'passed':False,'rules':[{'kind':'signal_contract','passed':False,'checks':[
                {'id':'hold','passed':False,'issues':['outside bounds'],'source_record_ids':refs,
                 'measurements':[{'duration_bounds_seconds':[.4,.6],'evaluated_hold_record_ids':refs}]}]}]}}
        original={'records':rows+[acceptance]}; before=deepcopy(original)
        chosen=model_review_evidence(original)
        self.assertEqual(original,before)
        ids={r['id'] for r in chosen['records']}
        self.assertTrue({'s-50','s-51','s-52','s-97','s-98','s-99'}<=ids)
        self.assertLessEqual(chosen['review_selection']['selected_samples'],40)
        result=next(r for r in chosen['records'] if r['kind']=='acceptance_result')['result']
        self.assertFalse(result['passed'])
        check=result['rules'][0]['checks'][0]
        self.assertEqual(check['measurements'][0]['duration_bounds_seconds'],[.4,.6])
        self.assertEqual(check['reference_list_projection']['source_record_ids']['original_count'],100)
        for row in chosen['records']:
            if row['kind']=='tag_sample':
                raw=next(r for r in rows if r['id']==row['id'])['tags'][0]
                vector=row['tag_rows'][0]
                self.assertEqual(chosen['tag_columns'][vector[0]],raw['id'])
                self.assertEqual(vector[1:],[raw[k] for k in ('value','isForced','openCircuit','shortCircuit')])

    def test_compact_view_still_retains_all_item_event_provenance(self):
        rows=[{'id':str(i),'kind':'tag_sample','tags':[]} for i in range(80)]
        event={'id':'item','kind':'item_event','source_record_ids':[str(i) for i in range(70)]}
        chosen=model_review_evidence({'records':rows+[event]})
        self.assertTrue(set(event['source_record_ids'])<={r['id'] for r in chosen['records']})

    def test_picture_only_supported_logic_and_hypothesis_repairs_are_rejected(self):
        keys = [t["key"] for t in self.built["io_contract"]["tags"]]
        finding = {"target": "plc", "evidence_ids": ["image"], "diagnosis": {
            "process_step": "feed", "signal_keys": [keys[0]], "causal_status": "supported", "hypothesis": "emit off",
            "alternatives": ["driver disconnected"], "discriminating_check": "read Emit", "expected_result": "false",
            "repair_scope": ["feed state"], "retest": ["one-item cycle"]}}
        feedback = {"verdict": "repair", "findings": [finding]}
        with self.assertRaisesRegex(ContractError, "measured/PLC evidence"):
            validate_diagnoses(feedback, self.task, self.built["io_contract"], [{"id": "image", "kind": "image"}])
        finding["diagnosis"]["causal_status"] = "hypothesis"
        with self.assertRaisesRegex(ContractError, "investigation"):
            validate_diagnoses(feedback, self.task, self.built["io_contract"], [{"id": "image", "kind": "image"}])

    def test_running_wait_rejects_forced_or_missing_state(self):
        runtime = FactoryIOClient({})
        for rows in ([], [{"id": "r", "name": "FACTORY I/O (Running)", "kind": "Input", "value": True, "isForced": True}]):
            with patch.object(runtime, "tags", return_value=rows), self.assertRaises(ContractError):
                runtime.wait_running(True)

    def test_running_wait_observes_input_after_accepted_write(self):
        runtime = FactoryIOClient({})
        rows = lambda v: [{"id": "r", "name": "FACTORY I/O (Running)", "kind": "Input", "value": v, "isForced": False}]
        with patch.object(runtime, "tags", side_effect=[rows(False), rows(True)]), patch("factoryio_agents.runtime.time.sleep"):
            self.assertTrue(runtime.wait_running(True)["running"])

    def test_four_real_role_wrappers_exchange_policy_handoffs_with_mocked_services(self):
        calls = []
        def complete(role, prompt, payload, images=None):
            calls.append((role, payload))
            from factoryio_agents.layout_prompts import LAYOUT_PROPOSAL_PROMPT, LAYOUT_REVIEW_PROMPT
            from test_layout_design import proposal_fixture
            expected=build_layout_prompt(payload['layout_review']) if role=='vision' and payload['phase']=='scene' else (
                VISION_PROMPTS[payload['phase']] if role=='vision' else LAYOUT_PROPOSAL_PROMPT
                if payload.get('scene_stage')=='layout_proposal' else LAYOUT_REVIEW_PROMPT
                if payload.get('planning_stage')=='layout_review' else PROMPTS[role])
            self.assertEqual(prompt,expected)
            packet = payload.get('handoff')
            if payload.get('scene_stage')=='layout_proposal': result=proposal_fixture(self.task)
            elif payload.get('planning_stage')=='layout_review':
                result={'verdict':'approved','summary':'Mocked design review.','findings':[]}
            elif role == "planner":
                result = deepcopy(self.task)
            elif role == "scene": result = deepcopy(self.plan)
            elif role == "plc": result = demo_program(payload["io_contract"])
            elif payload['phase']=='scene':
                return {'checks':[{'object_ids':[o['id']],'related_object_ids':[],'status':'ok',
                    'observed':'Explicitly mocked geometry','expected':'Explicitly mocked expectation',
                    'reason':'Mocked agreement','evidence_images':['frame'],'suggestion':'Keep placement'}
                    for o in payload['layout_review']['objects']]}
            else: result = {"versions": payload["versions"], "phase": payload["phase"], "verdict": "pass", "findings": []}
            return {**result, "handoff_id": packet["id"]}
        client = Mock(); client.complete.side_effect = complete
        config = {"llm": {}, "protocol": PROTOCOL, "scene_sources": [self.catalog["sources"][0]["path"]],
            "codesys": {"deploy": True}, "factoryio": {"runtime_control": True, "load_command": ["load"],
            "connect_command": ["connect"]}, "max_scene_repairs": 1, "max_plc_repairs": 1}
        def hook(command, substitutions, timeout):
            request = read_json(Path(substitutions["request"]))
            receipt = {**request, "app_version": "2.5.8"}
            if command == ["load"]: receipt.update(software_load_confirmed=True, confirmation_method="mocked")
            else: receipt.update(connection_confirmed=True, driver="ModbusTCPClient",
                modbus={"connected": True, **{k: request[k] for k in ("host", "port", "unit_id")}})
            write_json(Path(substitutions["confirmation"]), receipt)
            return {"returncode": 0}
        def capture(config, output, versions, io, **kwargs):
            output.mkdir(parents=True)
            frame = output / "frame.png"; frame.write_bytes(b"mock-image-never-sent-to-external-model")
            return {"versions": versions, "mode": "live", "reconciliation": {"passed": True}, "records": [
                {"id": "frame", "kind": "image", "path": str(frame), "sha256": file_hash(frame)}]}
        with patch("factoryio_agents.workflow.run_hook", side_effect=hook), patch(
            "factoryio_agents.workflow.collect_evidence", side_effect=capture), patch(
            "factoryio_agents.plc.run_codesys", return_value={"compiled": True, "started": True}), patch(
            "factoryio_agents.workflow.validate_events", return_value={"passed": True}), patch(
            "factoryio_agents.geometry_tools.loaded_geometry", return_value={"passed":True,
                "scope":"explicitly_mocked_geometry","spatial_checks_applied":True,"issues":[]}), patch(
            "factoryio_agents.workflow.FactoryIOClient") as runtime:
            runtime.return_value.wait_running.side_effect = lambda v: {"running": v, "isForced": False}
            result = Workflow(config, self.root / "run", client).run("mock test only")
        self.assertEqual(result["status"], "passed")  # MOCKED services; not a live acceptance result.
        self.assertEqual([r for r, _ in calls], ["planner", "scene", "planner", "scene", "vision", "plc", "vision"])
        self.assertTrue(all(p['handoff']['policy']==policy_manifest() for _,p in calls if p.get('handoff')))
        layout_payload=calls[4][1]
        self.assertNotIn('handoff',layout_payload)
        self.assertNotIn('manifest',layout_payload)
        self.assertTrue(calls[5][1]["io_contract"]["component_semantics"][0]["app_version_confirmed"])


if __name__ == "__main__": unittest.main()
