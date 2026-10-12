"""PLC contract and runner tests. CODESYS/PLC execution is mocked explicitly."""

import ast
from copy import deepcopy
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from factoryio_agents.plc import PLCError, export_program, run_codesys, transport_fingerprint, validate_device_bindings, validate_program


PROGRAM = {"schema_version": 1, "program_name": "PLC_PRG",
           "declaration": "PROGRAM PLC_PRG\nVAR\n Sensor: BOOL;\n Motor: BOOL;\n Weight: REAL;\n Count: DINT;\nEND_VAR",
           "implementation": "Motor := Sensor; Count := Count + 1;",
           "globals": [{"name": "GVL", "declaration": "VAR_GLOBAL\n Ready: BOOL;\nEND_VAR"}],
           "bindings": [{"tag_key": "sensor", "variable": "PLC_PRG.Sensor", "plc_type": "BOOL"},
                        {"tag_key": "motor", "variable": "PLC_PRG.Motor", "plc_type": "BOOL"}],
           "notes": [], "unresolved": []}
CONTRACT = {"schema_version": 1, "transport_confirmed": False, "driver": "ModbusTCPClient",
            "protocol": {"name": "modbus_tcp", "factoryio_role": "client", "plc_role": "server",
                         "host": "127.0.0.1", "port": 502, "unit_id": 1},
            "driver_properties": {"ReadCoils": "False", "ReadHoldingRegisters": "False", "ScaleFactor": "100"},
            "channel_counts": {"BitInputCount": 32, "BitOutputCount": 32},
            "channel_offsets": {"BitInputOffset": 0, "BitOutputOffset": 0},
            "tags": [{"key": "sensor", "object_id": "s", "data_type": "bool", "direction": "Input", "kind": "input"},
                     {"key": "motor", "object_id": "m", "data_type": "bool", "direction": "Output", "kind": "output"},
                     {"key": "weight", "object_id": "w", "data_type": "float", "direction": "Input", "kind": "input"},
                     {"key": "system-run", "object_id": None, "data_type": "bool", "direction": "Input", "kind": "input"}],
            "mappings": [{"tag_key": "sensor", "channel": "BitInput0", "channel_family": "Bit", "channel_index": 0,
                          "offset": 0, "type": "BinaryInput", "direction": "Input", "kind": "input", "data_type": "bool"},
                         {"tag_key": "motor", "channel": "BitOutput0", "channel_family": "Bit", "channel_index": 0,
                          "offset": 0, "type": "BinaryOutput", "direction": "Output", "kind": "output", "data_type": "bool"},
                         {"tag_key": "system-run", "channel": "BitInput9", "channel_family": "Bit", "channel_index": 9,
                          "offset": 0, "type": "BinaryInput", "direction": "Input", "kind": "input", "data_type": "bool"}]}
DEVICE_BINDINGS = [
    {"tag_key": "sensor", "variable": "Application.PLC_PRG.Sensor", "device_path": "<unnamed>/Device/Ethernet/Server",
     "connector_id": "1", "manual_iec_address": "%IX20.0", "channel_type": "Input", "bit_size": "1"},
    {"tag_key": "motor", "variable": "Application.PLC_PRG.Motor", "device_path": "<unnamed>/Device/Ethernet/Server",
     "connector_id": "1", "manual_iec_address": "%QX20.0", "channel_type": "Output", "bit_size": "1"}]


def inspection_fixture():
    rows = []
    for item in DEVICE_BINDINGS:
        rows.append({"device_path": item["device_path"], "connector_id": item["connector_id"],
                     "channel_type": item["channel_type"], "bit_size": item["bit_size"], "is_mappable_io": True,
                     "mapping": {"variable": item["variable"], "manual_iec_address": item["manual_iec_address"],
                                 "maps_to_existing_variable": True}})
    return {"diagnostics": [], "device_mappings": rows,
            "object_tree": [{"name": "Application", "is_application": True, "path": "Root/Device/Application"}],
            "tasks": [{"path": "Root/Device/Application/Tasks/MainTask", "pou_calls": ["PLC_PRG"]}]}


class PLCTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "original.project"
        self.source.write_bytes(b"test baseline; not a real CODESYS project")
        self.source_bytes = self.source.read_bytes()
        self.executable = self.directory / "fake-CODESYS.exe"
        self.executable.touch()
        self.output = self.directory / "output"
        self.config = {"executable": str(self.executable), "template_project": str(self.source),
                       "profile": "CODESYS V3.5 SP22 Patch 3", "transport_confirmed": False,
                       "bindings": [], "timeout_seconds": 5}

    def test_export_and_missing_transport_report(self):
        report = export_program(PROGRAM, CONTRACT, self.output)
        self.assertTrue(report["passed"])
        self.assertFalse(report["deployment_ready"])
        self.assertEqual(report["optional_system_tag_keys"], ["system-run"])
        for name in ("program.json", "PLC_PRG.declaration.st", "PLC_PRG.implementation.st", "GVL.declaration.st", "binding-report.json"):
            self.assertTrue((self.output / name).is_file())
        self.assertEqual(json.loads((self.output / "program.json").read_text()), PROGRAM)

    def test_missing_binding_blocks_deploy_but_allows_export(self):
        program = deepcopy(PROGRAM); program["bindings"].pop()
        report = export_program(program, CONTRACT, self.output)
        self.assertEqual(report["missing_tag_keys"], ["motor"])
        self.assertTrue(report["passed"])
        self.assertFalse(report["deployment_ready"])

    def test_forced_object_does_not_require_binding(self):
        program, contract = deepcopy(PROGRAM), deepcopy(CONTRACT)
        program["bindings"].pop()
        contract["tags"][1]["forced"] = {"enabled": True}
        self.assertEqual(validate_program(program, contract)["missing_tag_keys"], [])

    def test_bound_unmapped_object_tag_has_no_transport_path(self):
        program, contract = deepcopy(PROGRAM), deepcopy(CONTRACT)
        contract["transport_confirmed"] = True
        program["bindings"].append({"tag_key": "weight", "variable": "PLC_PRG.Weight", "plc_type": "REAL"})
        report = validate_program(program, contract)
        self.assertTrue(report["passed"])
        self.assertFalse(report["deployment_ready"])
        self.assertEqual(report["unmapped_binding_tag_keys"], ["weight"])
        self.assertTrue(any("no Factory I/O driver mapping" in u for u in report["unresolved"]))

    def test_verified_device_binding_schema_normalizes_width(self):
        value = deepcopy(DEVICE_BINDINGS); value[0]["bit_size"] = 1
        normalized = validate_device_bindings(value, PROGRAM["bindings"], CONTRACT, "Application")
        self.assertEqual(normalized[0]["bit_size"], "1")

    def test_verified_bindings_reject_missing_duplicate_inconsistent_evidence(self):
        variants = [[], DEVICE_BINDINGS[:1], DEVICE_BINDINGS + [DEVICE_BINDINGS[0]]]
        for field, value in (("variable", "Application.PLC_PRG.Missing"), ("bit_size", "16"),
                             ("channel_type", "Output"), ("manual_iec_address", ""), ("device_path", "Root/../Device")):
            rows = deepcopy(DEVICE_BINDINGS); rows[0][field] = value; variants.append(rows)
        rows = deepcopy(DEVICE_BINDINGS); rows[1].pop("connector_id"); variants.append(rows)
        for value in variants:
            with self.subTest(value=value):
                with self.assertRaises(PLCError):
                    validate_device_bindings(value, PROGRAM["bindings"], CONTRACT, "Application")

    def test_schema_header_and_names_rejected(self):
        for field, value in (("schema_version", True), ("program_name", "../bad"), ("program_name", "CON"),
                             ("declaration", "PROGRAM SomethingElse\nVAR Motor: BOOL; END_VAR")):
            with self.subTest(field=field, value=value):
                program = deepcopy(PROGRAM); program[field] = value
                with self.assertRaises(PLCError): validate_program(program, CONTRACT)

    def test_duplicate_global_case_insensitive(self):
        program = deepcopy(PROGRAM); program["globals"].append({"name": "gvl", "declaration": "VAR_GLOBAL X: BOOL; END_VAR"})
        with self.assertRaises(PLCError): validate_program(program, CONTRACT)

    def test_unclosed_declaration_block_is_rejected(self):
        program = deepcopy(PROGRAM); program["globals"][0]["declaration"] = "VAR_GLOBAL Ready: BOOL;"
        with self.assertRaises(PLCError): validate_program(program, CONTRACT)

    def test_aliases_cannot_bind_same_symbol_to_multiple_tags(self):
        program = deepcopy(PROGRAM); program["bindings"][1]["variable"] = "Application.PLC_PRG.Sensor"
        with self.assertRaises(PLCError): validate_program(program, CONTRACT)

    def test_malformed_duplicate_and_unknown_bindings_rejected(self):
        variants = [{"tag_key": "missing", "variable": "PLC_PRG.Sensor", "plc_type": "BOOL"},
                    {"tag_key": "sensor", "variable": "PLC_PRG.Sensor", "plc_type": "INT"},
                    {"tag_key": "sensor", "variable": "PLC_PRG.Sensor", "plc_type": "BOOL", "extra": True},
                    {"tag_key": "sensor", "variable": "PLC_PRG.Sensor[0]", "plc_type": "BOOL"},
                    {"tag_key": "sensor", "variable": "PLC_PRG.Missing", "plc_type": "BOOL"}]
        for binding in variants:
            with self.subTest(binding=binding):
                program = deepcopy(PROGRAM); program["bindings"][0] = binding
                with self.assertRaises(PLCError): validate_program(program, CONTRACT)
        program = deepcopy(PROGRAM); program["bindings"].append(deepcopy(program["bindings"][0]))
        with self.assertRaises(PLCError): validate_program(program, CONTRACT)
        program = deepcopy(PROGRAM); program["bindings"][1]["variable"] = "PLC_PRG.Sensor"
        with self.assertRaises(PLCError): validate_program(program, CONTRACT)

    def test_binding_type_must_match_declared_scalar(self):
        program = deepcopy(PROGRAM); program["declaration"] = program["declaration"].replace("Sensor: BOOL", "Sensor: INT")
        with self.assertRaises(PLCError): validate_program(program, CONTRACT)

    def test_comment_does_not_invent_declaration(self):
        program = deepcopy(PROGRAM)
        program["declaration"] = "PROGRAM PLC_PRG\nVAR\n (* Sensor: BOOL; (* nested *) *)\n Motor: BOOL; END_VAR"
        with self.assertRaises(PLCError): validate_program(program, CONTRACT)

    def test_qualified_global_and_application_binding(self):
        program = deepcopy(PROGRAM); program["bindings"][0]["variable"] = "Application.GVL.Ready"
        self.assertTrue(validate_program(program, CONTRACT)["passed"])

    def test_export_detaches_hardlink_without_changing_source(self):
        self.output.mkdir()
        os.link(self.source, self.output / "program.json")
        export_program(PROGRAM, CONTRACT, self.output)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        self.assertFalse(os.path.samefile(self.source, self.output / "program.json"))

    def _process(self, changes=None, returncode=0):
        def start(command, **kwargs):
            request = json.loads(Path(kwargs["env"]["FACTORYIO_CODESYS_REQUEST"]).read_text())
            response = {"schema_version": 1, "request_id": request["request_id"],
                        "project_path": request["project_path"], "source_sha256": request["source_sha256"],
                        "project_sha256": request["source_sha256"], "compiled": True, "started": False,
                        "build_completed": True, "messages_verified": True, "messages": [],
                        "error_count": 0, "warning_count": 0, "diagnostics": []}
            if changes: changes(request, response)
            Path(request["result_path"]).write_text(json.dumps(response), encoding="utf-8")
            process = Mock(returncode=returncode)
            process.communicate.return_value = (b"headless test", b"")
            return process
        return start

    def test_runner_copies_baseline_and_passes_expected_cli_env(self):
        with patch("factoryio_agents.plc.subprocess.Popen", side_effect=self._process()) as popen:
            result = run_codesys(self.config, self.output)
        self.assertTrue(result["compiled"])
        self.assertFalse(result["started"])
        self.assertTrue(result["source_unchanged"])
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        project = Path(result["project_path"])
        self.assertEqual(project.parent, (self.output / "project").resolve())
        self.assertFalse(os.path.samefile(self.source, project))
        args, kwargs = popen.call_args
        if os.name == "nt":
            self.assertIn('--profile="CODESYS V3.5 SP22 Patch 3"', args[0])
            self.assertIn('--runscript="', args[0])
            self.assertNotIn('"--profile=', args[0])
        else:
            self.assertEqual(args[0][1:3], ["--noUI", "--profile=CODESYS V3.5 SP22 Patch 3"])
        self.assertNotIn("--scriptargs", str(args[0]))
        self.assertFalse(kwargs["shell"])
        bridge_snapshot = self.output / "codesys_bridge.py"
        self.assertTrue(bridge_snapshot.is_file())
        self.assertEqual(result["bridge_sha256"], hashlib.sha256(bridge_snapshot.read_bytes()).hexdigest())
        self.assertIn(str(bridge_snapshot.resolve()), str(args[0]))
        self.assertEqual(kwargs["env"]["FACTORYIO_CODESYS_REQUEST"], str((self.output / "request.json").resolve()))

    def test_existing_hardlinked_project_rejected(self):
        project = self.output / "project"; project.mkdir(parents=True)
        os.link(self.source, project / self.source.name)
        with patch("factoryio_agents.plc.subprocess.Popen") as popen:
            result = run_codesys(self.config, self.output)
        self.assertFalse(result["compiled"]); popen.assert_not_called()
        self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def test_missing_bridge_result_is_not_success(self):
        process = Mock(returncode=0); process.communicate.return_value = (b"", b"")
        with patch("factoryio_agents.plc.subprocess.Popen", return_value=process):
            result = run_codesys(self.config, self.output)
        self.assertFalse(result["compiled"])
        self.assertIn("no fresh", result["diagnostics"][-1]["message"])

    def test_stale_missing_counts_and_inconsistent_error_results_fail(self):
        variants = [{"request_id": "old"}, {"error_count": None}, {"error_count": 1},
                    {"messages_verified": False}, {"build_completed": False}, {"project_sha256": "wrong"},
                    {"messages": [{"severity": "error", "text": "actual error"}]}]
        for index, variant in enumerate(variants):
            with self.subTest(variant=variant):
                with patch("factoryio_agents.plc.subprocess.Popen", side_effect=self._process(lambda req, res: res.update(variant))):
                    result = run_codesys(self.config, self.directory / ("run-" + str(index)))
                self.assertFalse(result["compiled"])

    def test_nonzero_exit_still_requires_verified_result(self):
        with patch("factoryio_agents.plc.subprocess.Popen", side_effect=self._process(returncode=1)):
            self.assertFalse(run_codesys(self.config, self.output)["compiled"])

    def test_deploy_blocked_request_still_compiles_only(self):
        def inspect_request(request, response):
            self.assertFalse(request["deploy"])
            response.update({"started": True, "application_state": "run"})
        with patch("factoryio_agents.plc.subprocess.Popen", side_effect=self._process(inspect_request)):
            result = run_codesys(self.config, self.output, PROGRAM, CONTRACT, deploy=True)
        self.assertTrue(result["compiled"])
        self.assertFalse(result["started"])
        self.assertTrue(result["deployment_blockers"])

    def test_confirmed_config_requires_matching_bindings(self):
        config, contract = deepcopy(self.config), deepcopy(CONTRACT)
        config["transport_confirmed"] = contract["transport_confirmed"] = True
        config["bindings"] = deepcopy(PROGRAM["bindings"])
        config["verified_device_bindings"] = deepcopy(DEVICE_BINDINGS)
        config["verified_template_sha256"] = hashlib.sha256(self.source_bytes).hexdigest()
        config["verified_transport_sha256"] = transport_fingerprint(contract)
        def inspect_request(request, response):
            self.assertTrue(request["deploy"])
            self.assertEqual(request["verified_device_bindings"], DEVICE_BINDINGS)
            self.assertEqual(request["transport_sha256"], config["verified_transport_sha256"])
            response.update({"started": True, "application_state": "run", "bindings_verified": True, "task_program_verified": True})
        with patch("factoryio_agents.plc.subprocess.Popen", side_effect=self._process(inspect_request)):
            result = run_codesys(config, self.output, PROGRAM, contract, deploy=True)
        self.assertTrue(result["compiled"]); self.assertTrue(result["started"])
        self.assertEqual(result["deployment_blockers"], [])
        self.assertTrue(result["transport_verified"])

    def test_transport_assertion_alone_cannot_authorize_deployment(self):
        config, contract = deepcopy(self.config), deepcopy(CONTRACT)
        config["transport_confirmed"] = contract["transport_confirmed"] = True
        config["bindings"] = deepcopy(PROGRAM["bindings"])
        def check_request(request, response):
            self.assertFalse(request["deploy"])
        with patch("factoryio_agents.plc.subprocess.Popen", side_effect=self._process(check_request)):
            result = run_codesys(config, self.output, PROGRAM, contract, deploy=True)
        self.assertTrue(result["compiled"]); self.assertFalse(result["started"])
        self.assertTrue(any("verified_device_bindings" in blocker for blocker in result["deployment_blockers"]))

    def test_commissioning_retains_pending_observations_without_waiving_transport(self):
        program = deepcopy(PROGRAM)
        program["unresolved"] = ["Physical weight calibration pending"]
        def check_request(request, response):
            self.assertFalse(request["deploy"])
        with patch("factoryio_agents.plc.subprocess.Popen", side_effect=self._process(check_request)):
            result = run_codesys(self.config, self.output, program, CONTRACT, deploy=True, commissioning=True)
        self.assertFalse(result["started"])
        self.assertFalse(result["physical_acceptance_passed"])
        self.assertEqual(result["commissioning_pending"], program["unresolved"])
        self.assertTrue(any("transport" in text.lower() for text in result["deployment_blockers"]))

    def test_wrong_baseline_sha_or_task_expectation_blocks_deployment(self):
        contract = deepcopy(CONTRACT); contract["transport_confirmed"] = True
        for index, extra in enumerate(({"verified_template_sha256": "0" * 64}, {"expected_task_program": "OtherProgram"})):
            config = deepcopy(self.config)
            config.update({"transport_confirmed": True, "bindings": deepcopy(PROGRAM["bindings"]),
                           "verified_device_bindings": deepcopy(DEVICE_BINDINGS),
                           "verified_template_sha256": hashlib.sha256(self.source_bytes).hexdigest(),
                           "verified_transport_sha256": transport_fingerprint(contract)})
            config.update(extra)
            with patch("factoryio_agents.plc.subprocess.Popen", side_effect=self._process()):
                result = run_codesys(config, self.directory / ("gate-" + str(index)), PROGRAM, contract, deploy=True)
            self.assertTrue(result["compiled"]); self.assertFalse(result["started"])
            self.assertTrue(result["deployment_blockers"])

    def test_started_requires_both_bridge_verification_flags(self):
        contract = deepcopy(CONTRACT); contract["transport_confirmed"] = True
        config = deepcopy(self.config)
        config.update({"transport_confirmed": True, "bindings": deepcopy(PROGRAM["bindings"]),
                       "verified_device_bindings": deepcopy(DEVICE_BINDINGS),
                       "verified_template_sha256": hashlib.sha256(self.source_bytes).hexdigest(),
                       "verified_transport_sha256": transport_fingerprint(contract)})
        for index, flags in enumerate(({}, {"bindings_verified": True}, {"task_program_verified": True})):
            def changes(request, response):
                response.update({"started": True, "application_state": "run"}); response.update(flags)
            with patch("factoryio_agents.plc.subprocess.Popen", side_effect=self._process(changes)):
                result = run_codesys(config, self.directory / ("flags-" + str(index)), PROGRAM, contract, deploy=True)
            self.assertTrue(result["compiled"]); self.assertFalse(result["started"])

    def test_transport_evidence_invalidated_by_slot_offset_scaling_endpoint(self):
        contract = deepcopy(CONTRACT); contract["transport_confirmed"] = True
        config = deepcopy(self.config)
        config.update({"transport_confirmed": True, "bindings": deepcopy(PROGRAM["bindings"]),
                       "verified_device_bindings": deepcopy(DEVICE_BINDINGS),
                       "verified_template_sha256": hashlib.sha256(self.source_bytes).hexdigest(),
                       "verified_transport_sha256": transport_fingerprint(contract)})
        variants = []
        changed = deepcopy(contract); changed["mappings"][0].update({"channel": "BitInput1", "channel_index": 1}); variants.append(changed)
        changed = deepcopy(contract); changed["channel_offsets"]["BitInputOffset"] = 1
        for mapping in changed["mappings"]:
            if mapping["direction"] == "Input": mapping["offset"] = 1
        variants.append(changed)
        changed = deepcopy(contract); changed["driver_properties"]["ScaleFactor"] = "1000"; variants.append(changed)
        for field, value in (("host", "127.0.0.2"), ("port", 503), ("unit_id", 2)):
            changed = deepcopy(contract); changed["protocol"][field] = value; variants.append(changed)
        for index, changed in enumerate(variants):
            with self.subTest(index=index):
                self.assertNotEqual(transport_fingerprint(changed), config["verified_transport_sha256"])
                def check_request(request, response):
                    self.assertFalse(request["deploy"])
                    response.update({"started": True, "application_state": "run", "bindings_verified": True, "task_program_verified": True})
                with patch("factoryio_agents.plc.subprocess.Popen", side_effect=self._process(check_request)):
                    result = run_codesys(config, self.directory / ("transport-" + str(index)), PROGRAM, changed, deploy=True)
                self.assertTrue(result["compiled"]); self.assertFalse(result["started"]); self.assertFalse(result["transport_verified"])
                self.assertEqual(result["transport_sha256"], transport_fingerprint(changed))

    def test_scene_geometry_and_confirmation_do_not_change_transport_fingerprint(self):
        changed = deepcopy(CONTRACT)
        changed.update({"scene_sha256": "changed scene hash", "transport_confirmed": True,
                        "software_load_confirmed": True, "position": [99, 50, 12]})
        changed["tags"][0].update({"position": [1, 2, 3], "name": "different label", "object_id": "new object label"})
        changed["mappings"].reverse(); changed["tags"].reverse()
        self.assertEqual(transport_fingerprint(changed), transport_fingerprint(CONTRACT))

    def test_missing_protocol_or_mapping_metadata_allows_compile_only(self):
        config, contract = deepcopy(self.config), deepcopy(CONTRACT)
        config.update({"transport_confirmed": True, "bindings": deepcopy(PROGRAM["bindings"]),
                       "verified_device_bindings": deepcopy(DEVICE_BINDINGS),
                       "verified_template_sha256": hashlib.sha256(self.source_bytes).hexdigest(),
                       "verified_transport_sha256": transport_fingerprint(contract)})
        contract["transport_confirmed"] = True
        variants = []
        for field in ("protocol", "driver_properties", "channel_counts", "channel_offsets"):
            changed = deepcopy(contract); changed.pop(field); variants.append(changed)
        changed = deepcopy(contract); changed["mappings"][0].pop("offset"); variants.append(changed)
        changed = deepcopy(contract); changed["protocol"].pop("unit_id"); variants.append(changed)
        for index, changed in enumerate(variants):
            with self.subTest(index=index):
                with self.assertRaises(PLCError): transport_fingerprint(changed)
                with patch("factoryio_agents.plc.subprocess.Popen", side_effect=self._process()):
                    result = run_codesys(config, self.directory / ("missing-transport-" + str(index)), PROGRAM, changed, deploy=True)
                self.assertTrue(result["compiled"]); self.assertFalse(result["started"])
                self.assertIsNone(result["transport_sha256"])

    def test_timeout_terminates_only_spawned_process(self):
        process = Mock(returncode=-1)
        process.communicate.side_effect = [subprocess.TimeoutExpired("fake", 5), (b"", b"")]
        with patch("factoryio_agents.plc.subprocess.Popen", return_value=process):
            result = run_codesys(self.config, self.output)
        self.assertFalse(result["compiled"])
        process.terminate.assert_called_once(); process.kill.assert_not_called()
        self.assertEqual(result["diagnostics"][-1]["code"], "timeout")


class NumericWireTests(unittest.TestCase):
    def setUp(self):
        self.contract = deepcopy(CONTRACT)
        self.contract["channel_counts"]["NumericInputCount"] = 1
        self.contract["channel_offsets"]["NumericInputOffset"] = 0
        self.contract["mappings"].append({"tag_key": "weight", "channel": "NumericInput0", "channel_family": "Numeric", "channel_index": 0,
                                          "offset": 0, "type": "AnalogueInput", "direction": "Input", "kind": "input", "data_type": "float"})
        self.contract["wire_encodings"] = [{"tag_key": "weight", "format": "modbus_positive16", "scale_factor": 100, "logical_min": 0, "logical_max": 10}]
        self.program = deepcopy(PROGRAM)
        self.program["declaration"] = self.program["declaration"].replace("Weight: REAL", "Weight: WORD")
        self.program["bindings"].append({"tag_key": "weight", "variable": "PLC_PRG.Weight", "plc_type": "WORD"})

    def test_float_tag_can_bind_explicit_numeric_word_transport(self):
        result = validate_program(self.program, self.contract)
        self.assertFalse(result["missing_tag_keys"])
        self.assertFalse(result["deployment_ready"])
        self.assertEqual(result["wire_encodings"], self.contract["wire_encodings"])

    def test_missing_wire_descriptor_prevents_word_binding(self):
        self.contract.pop("wire_encodings")
        with self.assertRaises(PLCError): validate_program(self.program, self.contract)

    def test_logical_real_cannot_bind_to_numeric_word_descriptor(self):
        self.program["declaration"] = self.program["declaration"].replace("Weight: WORD", "Weight: REAL")
        self.program["bindings"][-1]["plc_type"] = "REAL"
        with self.assertRaises(PLCError): validate_program(self.program, self.contract)

    def test_negative_overflow_and_inconsistent_scaling_rejected(self):
        for field, value in (("logical_min", -1), ("logical_max", 328), ("scale_factor", 10), ("scale_factor", True), ("logical_max", float("nan"))):
            contract = deepcopy(self.contract); contract["wire_encodings"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(PLCError): validate_program(self.program, contract)

    def test_wire_domain_change_invalidates_transport_evidence(self):
        changed = deepcopy(self.contract); changed["wire_encodings"][0]["logical_max"] = 9
        self.assertNotEqual(transport_fingerprint(self.contract), transport_fingerprint(changed))

    def test_missing_encoding_is_unresolved_even_when_transport_flag_is_true(self):
        self.contract.pop("wire_encodings"); self.contract["transport_confirmed"] = True
        self.program["declaration"] = self.program["declaration"].replace("Weight: WORD", "Weight: REAL")
        self.program["bindings"][-1]["plc_type"] = "REAL"
        self.assertFalse(validate_program(self.program, self.contract)["deployment_ready"])


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parents[1] / "factoryio_agents" / "codesys_bridge.py"
        spec = importlib.util.spec_from_file_location("codesys_bridge_test", self.path)
        self.bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.bridge)

    def test_bridge_avoids_python3_only_syntax(self):
        tree = ast.parse(self.path.read_text(encoding="utf-8"))
        forbidden = (ast.JoinedStr, ast.AnnAssign, ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith, ast.NamedExpr)
        self.assertFalse(any(isinstance(node, forbidden) for node in ast.walk(tree)))
        for node in ast.walk(tree):
            if isinstance(node, ast.arguments):
                self.assertFalse(node.kwonlyargs or node.posonlyargs)
        # This is a syntax-subset guard, not an actual IronPython execution test.

    def test_build_category_and_numeric_error_severity_are_checked(self):
        system = Mock()
        system.get_message_categories.return_value = ["other"]
        message = Mock(severity=2, text="compile failure", position_text="line 3", prefix="C", number=1)
        system.get_message_objects.side_effect = lambda category: [message] if category == self.bridge._BUILD_CATEGORY else []
        records, failures, verified = self.bridge._messages(system)
        self.assertTrue(verified); self.assertEqual(failures, [])
        self.assertEqual(records[0]["severity"], "error")
        system.get_message_objects.assert_any_call(self.bridge._BUILD_CATEGORY)

    def test_unclassified_messages_prevent_compile_certification(self):
        system = Mock()
        system.get_message_categories.return_value = []
        system.get_message_objects.return_value = ["legacy string error"]
        records, failures, verified = self.bridge._messages(system)
        self.assertFalse(verified); self.assertEqual(records[0]["severity"], "unknown")

    def test_real_guid_conversion_helper(self):
        fake_guid = Mock(return_value="converted-guid")
        with patch.dict("sys.modules", {"System": types.SimpleNamespace(Guid=fake_guid)}):
            self.assertEqual(self.bridge._category_guid(self.bridge._BUILD_CATEGORY), "converted-guid")
        fake_guid.assert_called_once_with(self.bridge._BUILD_CATEGORY)

    def test_actual_device_mapping_matches_verified_evidence(self):
        report = self.bridge._verify_device_bindings(inspection_fixture(), DEVICE_BINDINGS, PROGRAM["bindings"], "Application")
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["matched"]), 2)

    def test_register_array_parent_is_distinct_from_matching_scalar(self):
        fixture = inspection_fixture()
        parent = deepcopy(fixture["device_mappings"][0]); parent["bit_size"] = "160"; parent["mapping"]["variable"] = ""
        fixture["device_mappings"].append(parent)
        self.assertTrue(self.bridge._verify_device_bindings(fixture, DEVICE_BINDINGS, PROGRAM["bindings"], "Application")["passed"])

    def test_device_patch_requires_exact_source_hash_before_mutation(self):
        patch_ = {"source_sha256": "another", "device_path": "root/server", "connector_id": "1",
                  "parameters": [], "bindings": [], "clear_existing_mappings": True}
        project = Mock()
        with self.assertRaisesRegex(RuntimeError, "another source hash"):
            self.bridge._apply_device_patch(project, {"source_sha256": "actual", "device_patch": patch_, "deploy": False})
        project.export_native.assert_not_called()

    def test_device_preparation_cannot_deploy_in_same_run(self):
        with self.assertRaisesRegex(RuntimeError, "cannot deploy"):
            self.bridge._apply_device_patch(Mock(), {"deploy": True, "device_patch": {}})

    def test_actual_mapping_missing_ambiguous_or_changed_fails(self):
        variants = []
        for field, value in (("channel_type", "Output"), ("bit_size", "16"), ("connector_id", "2")):
            fixture = inspection_fixture(); fixture["device_mappings"][0][field] = value; variants.append(fixture)
        fixture = inspection_fixture(); fixture["device_mappings"][0]["mapping"]["variable"] = "Application.PLC_PRG.Other"; variants.append(fixture)
        fixture = inspection_fixture(); fixture["device_mappings"].append(deepcopy(fixture["device_mappings"][0])); variants.append(fixture)
        fixture = inspection_fixture(); fixture["diagnostics"] = [{"code": "device_inspection"}]; variants.append(fixture)
        for fixture in variants:
            with self.subTest(fixture=fixture):
                self.assertFalse(self.bridge._verify_device_bindings(fixture, DEVICE_BINDINGS, PROGRAM["bindings"], "Application")["passed"])

    def test_task_call_must_be_in_selected_application(self):
        fixture = inspection_fixture()
        self.assertTrue(self.bridge._verify_task_program(fixture, "Application", "PLC_PRG")["passed"])
        fixture["tasks"][0]["pou_calls"] = ["Other"]
        self.assertFalse(self.bridge._verify_task_program(fixture, "Application", "PLC_PRG")["passed"])
        fixture["tasks"][0]["pou_calls"] = ["PLC_PRG"]
        fixture["tasks"][0]["path"] = "Root/OtherApplication/Tasks/MainTask"
        self.assertFalse(self.bridge._verify_task_program(fixture, "Application", "PLC_PRG")["passed"])

    def _run_main(self, current):
        request = {"request_id": "a" * 32, "project_path": "copied.project", "source_sha256": "b" * 64,
                   "verified_template_sha256": "b" * 64, "inspection_path": "inspection.json", "result_path": "result.json",
                   "application_name": "Application", "expected_task_program": "PLC_PRG", "program": PROGRAM,
                   "verified_device_bindings": DEVICE_BINDINGS, "deploy": True, "deployment_ready": True, "transport_confirmed": True,
                   "deployment_online": {"connect": True}}
        project, system, online = Mock(), Mock(ui_present=False), Mock()
        online_application = Mock(is_logged_in=True, application_state="run", operation_state="program_loaded")
        online_context = Mock(); online_context.__enter__ = Mock(return_value=online_application); online_context.__exit__ = Mock(return_value=False)
        online.create_online_application.return_value = online_context
        artifacts = {}
        session = Mock(return_value={"status": "ok", "start_called": True, "application_state": "run"})
        with patch.dict(self.bridge.__dict__, {"system": system, "projects": Mock(open=Mock(return_value=project)),
                                              "online": online, "OnlineChangeOption": types.SimpleNamespace(Never=0)}), \
             patch.object(self.bridge, "_load_request", return_value=request), \
             patch.object(self.bridge, "_inspect", side_effect=[inspection_fixture(), current]), \
             patch.object(self.bridge, "_application", return_value=Mock()), \
             patch.object(self.bridge, "_update"), \
             patch.object(self.bridge, "_online_session", session), \
             patch.object(self.bridge, "_messages", return_value=([], [], True)), \
             patch.object(self.bridge, "_hash", return_value="b" * 64), \
             patch.object(self.bridge, "_write_json", side_effect=lambda path, value: artifacts.update({path: value})):
            self.bridge.main()
        return artifacts["result.json"], session, system

    def test_bridge_rechecks_actual_mappings_before_any_online_login(self):
        current = inspection_fixture(); current["device_mappings"][0]["mapping"]["variable"] = "Application.PLC_PRG.Other"
        result, online, system = self._run_main(current)
        self.assertTrue(result["compiled"]); self.assertFalse(result["bindings_verified"]); self.assertFalse(result["started"])
        online.assert_not_called()

    def test_bridge_rechecks_task_call_before_any_online_login(self):
        current = inspection_fixture(); current["tasks"] = []
        result, online, system = self._run_main(current)
        self.assertTrue(result["compiled"]); self.assertFalse(result["task_program_verified"]); self.assertFalse(result["started"])
        online.assert_not_called()

    def test_bridge_starts_only_with_real_mapping_and_task_match(self):
        result, online, system = self._run_main(inspection_fixture())
        self.assertTrue(result["compiled"]); self.assertTrue(result["bindings_verified"]); self.assertTrue(result["task_program_verified"]); self.assertTrue(result["started"])
        online.assert_called_once()
        system.clear_messages.assert_called_once_with(self.bridge._BUILD_CATEGORY)


if __name__ == "__main__":
    unittest.main()
