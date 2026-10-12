"""Reproducible manual weight-sorting baseline from inspected local templates.

This is authored in the current assistant session, not an external-model run.
It prepares and compiles a project; calibration and physical routing remain live gates.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET

from .contracts import file_hash, read_json, require, version_tuple, write_json
from .plc import export_program, transport_fingerprint


_SIGNALS = {
    "BitInput0": "AtScaleEntry", "BitInput1": "AtScale", "BitInput2": "AtScaleExit",
    "BitInput3": "AtLeftEntry", "BitInput4": "AtExitLeft", "BitInput5": "AtForwardEntry",
    "BitInput6": "AtExitFront", "BitInput7": "AtRightEntry", "BitInput8": "AtExitRight",
    "BitInput9": "StartButton", "BitInput10": "ResetButton", "BitInput11": "StopPermissive",
    "BitInput12": "AutoMode", "BitInput13": "SimulationRunning",
    "BitOutput0": "EntryConveyor", "BitOutput1": "LoadScale", "BitOutput2": "SendLeft",
    "BitOutput3": "LeftConveyor", "BitOutput4": "SendRight", "BitOutput5": "RightConveyor",
    "BitOutput6": "SendForward", "BitOutput7": "FrontConveyor", "BitOutput8": "StartLight",
    "BitOutput9": "ResetLight", "BitOutput10": "StopLight",
    "NumericInput0": "WeightRaw", "NumericOutput0": "LeftCount", "NumericOutput1": "ForwardCount",
    "NumericOutput2": "RightCount", "NumericOutput3": "WeightDisplay",
    "BitOutput11": "Emit",
}
_NAMES = ["At scale entry", "At scale", "At scale exit", "At left entry", "At exit left",
          "At forward entry", "At exit front", "At right entry", "At exit right", "Start", "Reset",
          "Stop", "Auto", "RunInput", "Entry conveyor", "Load scale", "Send left", "Left conveyor",
          "Send right", "Right conveyor", "Send forward", "Front conveyor", "Start light", "Reset light",
          "Stop light", "Weight", "Left count", "Forward count", "Right count", "Weight", "Emitter"]

_DECLARATION = """PROGRAM PLC_PRG
VAR
    State : INT := 0;
    PreviousState : INT := -1;
    ScanState : INT;
    Route : INT := 0;
    FaultCode : INT := 0;
    WeightKg : REAL := 0.0;
    LatchedWeightKg : REAL := 0.0;
    StableAnchorKg : REAL := 0.0;
    KgPerVolt : REAL := 2.0;
    StableInput : BOOL;
    PlantEmpty : BOOL;
    AnyExit : BOOL;
    MatchingExit : BOOL;
    ConflictingExit : BOOL;
    LeftOccupied : BOOL;
    FrontOccupied : BOOL;
    RightOccupied : BOOL;
    ExitEdges : INT;
    StartEdge : R_TRIG;
    ResetEdge : R_TRIG;
    LeftEdge : R_TRIG;
    FrontEdge : R_TRIG;
    RightEdge : R_TRIG;
    StableTimer : TON;
    StateTimer : TON;
    ClearTimer : TON;
END_VAR"""

_IMPLEMENTATION = """(* Manual baseline. Confirm 20 kg scale calibration before deployment.
   State: 0 idle, 10 feed, 20 weigh, 30 route, 40 clear, 99 latched fault.
   Modbus WORD = positive logical value * driver scale; logical voltage -> kg is separate. *)
ScanState := State;
WeightKg := WORD_TO_REAL(WeightIO.WeightRaw) / 100.0 * KgPerVolt;
StartEdge(CLK := WeightIO.StartButton);
ResetEdge(CLK := WeightIO.ResetButton);
(* Retroreflective exit sensors are TRUE with a clear beam, FALSE when occupied. *)
LeftOccupied := NOT WeightIO.AtExitLeft;
FrontOccupied := NOT WeightIO.AtExitFront;
RightOccupied := NOT WeightIO.AtExitRight;
LeftEdge(CLK := LeftOccupied);
FrontEdge(CLK := FrontOccupied);
RightEdge(CLK := RightOccupied);
ExitEdges := BOOL_TO_INT(LeftEdge.Q) + BOOL_TO_INT(FrontEdge.Q) + BOOL_TO_INT(RightEdge.Q);
AnyExit := ExitEdges > 0;
MatchingExit := ((Route = 1) AND LeftEdge.Q) OR ((Route = 2) AND FrontEdge.Q) OR ((Route = 3) AND RightEdge.Q);
ConflictingExit := AnyExit AND ((ExitEdges <> 1) OR NOT MatchingExit);
(* Upstream material may wait at scale entry while the active route clears. *)
PlantEmpty := NOT (WeightIO.AtScale OR WeightIO.AtScaleExit
    OR WeightIO.AtLeftEntry OR LeftOccupied OR WeightIO.AtForwardEntry
    OR FrontOccupied OR WeightIO.AtRightEntry OR RightOccupied);

(* Set every motion output every scan. Stop/fault overrides after state processing. *)
WeightIO.Emit := FALSE;
WeightIO.EntryConveyor := FALSE;
WeightIO.LoadScale := FALSE;
WeightIO.SendLeft := FALSE;
WeightIO.SendRight := FALSE;
WeightIO.SendForward := FALSE;
WeightIO.LeftConveyor := FALSE;
WeightIO.FrontConveyor := FALSE;
WeightIO.RightConveyor := FALSE;

IF NOT WeightIO.SimulationRunning THEN
    State := 0;
    Route := 0;
    FaultCode := 0;
ELSIF ResetEdge.Q THEN
    IF PlantEmpty THEN
        State := 0; Route := 0; FaultCode := 0;
        WeightIO.LeftCount := WORD#0; WeightIO.ForwardCount := WORD#0;
        WeightIO.RightCount := WORD#0; WeightIO.WeightDisplay := WORD#0;
    ELSE
        State := 99; FaultCode := 1; (* Reset requested with occupied transport. *)
    END_IF;
ELSIF NOT WeightIO.StopPermissive OR NOT WeightIO.AutoMode THEN
    IF (State >= 10) AND (State <= 40) THEN
        State := 99; FaultCode := 2; (* Require empty/reset after interrupted cycle. *)
    END_IF;
END_IF;

StableInput := FALSE;
IF State = 20 THEN
    IF (PreviousState <> 20) OR ABS(WeightKg - StableAnchorKg) > 0.05 THEN
        StableAnchorKg := WeightKg;
    ELSE
        StableInput := WeightIO.AtScale AND (WeightKg > 0.05) AND (WeightKg < 20.0);
    END_IF;
END_IF;
StableTimer(IN := StableInput, PT := T#500MS);
StateTimer(IN := (State = PreviousState) AND (State >= 10) AND (State <= 40), PT := T#30S);
ClearTimer(IN := (State = 40) AND PlantEmpty, PT := T#500MS);
IF StateTimer.Q THEN State := 99; FaultCode := 3; END_IF;

CASE State OF
0:
    IF StartEdge.Q AND WeightIO.SimulationRunning AND WeightIO.StopPermissive AND WeightIO.AutoMode AND PlantEmpty THEN
        State := 10;
    END_IF;
10:
    WeightIO.Emit := TRUE; (* Emitter UpTo=1; FALSE in other states rearms the next item. *)
    WeightIO.EntryConveyor := TRUE;
    WeightIO.LoadScale := TRUE;
    IF WeightIO.AtScale THEN State := 20; END_IF;
20:
    IF StableTimer.Q THEN
        LatchedWeightKg := WeightKg;
        WeightIO.WeightDisplay := REAL_TO_WORD(LatchedWeightKg * 100.0); (* Display stores 0.01 kg units. *)
        IF LatchedWeightKg < 3.0 THEN Route := 1;
        ELSIF LatchedWeightKg < 6.0 THEN Route := 2;
        ELSE Route := 3; END_IF;
        State := 30;
    END_IF;
30:
    WeightIO.LoadScale := TRUE;
    WeightIO.SendLeft := Route = 1; WeightIO.LeftConveyor := Route = 1;
    WeightIO.SendForward := TRUE; (* PopUpWheelSorter roller lift/motor enable, including lateral routes. *)
    WeightIO.FrontConveyor := Route = 2;
    WeightIO.SendRight := Route = 3; WeightIO.RightConveyor := Route = 3;
    IF ConflictingExit THEN
        State := 99; FaultCode := 4;
    ELSIF MatchingExit THEN
        CASE Route OF
        1: IF WeightIO.LeftCount < WORD#32767 THEN WeightIO.LeftCount := WeightIO.LeftCount + WORD#1; ELSE State := 99; FaultCode := 5; END_IF;
        2: IF WeightIO.ForwardCount < WORD#32767 THEN WeightIO.ForwardCount := WeightIO.ForwardCount + WORD#1; ELSE State := 99; FaultCode := 5; END_IF;
        3: IF WeightIO.RightCount < WORD#32767 THEN WeightIO.RightCount := WeightIO.RightCount + WORD#1; ELSE State := 99; FaultCode := 5; END_IF;
        END_CASE;
        IF State <> 99 THEN State := 40; END_IF;
    END_IF;
40:
    WeightIO.LoadScale := TRUE;
    WeightIO.SendLeft := Route = 1; WeightIO.LeftConveyor := Route = 1;
    WeightIO.SendForward := TRUE; (* PopUpWheelSorter roller lift/motor enable, including lateral routes. *)
    WeightIO.FrontConveyor := Route = 2;
    WeightIO.SendRight := Route = 3; WeightIO.RightConveyor := Route = 3;
    IF AnyExit THEN State := 99; FaultCode := 6;
    ELSIF ClearTimer.Q THEN State := 10; Route := 0; END_IF;
99:
    ; (* Fault is reset only when the plant is empty. *)
ELSE
    State := 99; FaultCode := 7;
END_CASE;

IF (State = 0) OR (State = 99) OR NOT WeightIO.SimulationRunning OR NOT WeightIO.StopPermissive OR NOT WeightIO.AutoMode THEN
    WeightIO.Emit := FALSE;
    WeightIO.EntryConveyor := FALSE; WeightIO.LoadScale := FALSE;
    WeightIO.SendLeft := FALSE; WeightIO.SendForward := FALSE; WeightIO.SendRight := FALSE;
    WeightIO.LeftConveyor := FALSE; WeightIO.FrontConveyor := FALSE; WeightIO.RightConveyor := FALSE;
END_IF;
WeightIO.StartLight := (State >= 10) AND (State <= 40);
WeightIO.ResetLight := State = 99;
WeightIO.StopLight := (State = 0) OR (State = 99);
PreviousState := ScanState;
"""


def configure_paced_emitter(root: ET.Element, contract: dict, manifest: dict) -> None:
    """Prepare an owned scene copy for native one-item emission under PLC control."""
    emitters = [o for o in root.findall("Object") if o.find("Proxy").get("PrefabName") == "Emitter"]
    require(len(emitters) == 1, "paced baseline requires exactly one observed Emitter")
    emitter = emitters[0]
    require(emitter.find("OperatingMode").get("Description") == "$C_Emitter_OM_Default_NAME", "unknown Emitter operating mode")
    outputs = emitter.findall("OperatingMode/GroupIO/BinaryOutput")
    require(len(outputs) == 1, "Emitter output is ambiguous")
    key = outputs[0].get("Key")
    tags = [t for t in contract["tags"] if t["key"] == key]
    require(len(tags) == 1 and tags[0]["name"] == "Emitter" and tags[0]["kind"] == "output" and tags[0]["data_type"] == "bool", "Emitter contract differs")
    mappings = contract["mappings"]
    occupied = [m for m in mappings if m["channel"] == "BitOutput11" or m["tag_key"] == key]
    require(not occupied or (len(occupied) == 1 and occupied[0]["channel"] == "BitOutput11" and occupied[0]["tag_key"] == key), "Emitter channel collides with another mapping")
    driver = root.find("Drivers/ModbusTCPClient")
    require(driver is not None and driver.find("PointIOOffset").get("BitOutputOffset") == "0", "Emitter supports observed zero-offset Modbus client only")
    count = driver.find("PointIOCount")
    require(count.get("BitOutputCount") in ("11", "12"), "unexpected output count for paced baseline")
    existing = driver.findall("BitOutput11")
    require(not existing or (len(existing) == 1 and existing[0].get("PointIOKey") == key), "serialized Emitter slot collides")
    require(not driver.findall("*[@PointIOKey='" + key + "']") or existing, "Emitter already mapped to another serialized slot")
    if not existing:
        ET.SubElement(driver, "BitOutput11", PointIOKey=key)
    root.find("Drivers").set("CurrentDriver", "64")  # Observed Factory I/O Modbus TCP Client enum.
    count.set("BitOutputCount", "12")
    outputs[0].set("UseForcedValue", "False")
    outputs[0].set("ForcedValue", "False")
    emitter.find("Emitter").set("UpTo", "1")
    tag = tags[0]
    tag.update({"use_forced_value": False, "forced_value": "False", "forced": {"enabled": False, "value": "False"}})
    for obj in manifest["objects"]:
        for item in obj.get("tags", []):
            if item["key"] == key:
                item.update({"use_forced_value": False, "forced_value": "False", "forced": {"enabled": False, "value": "False"}})
    if not occupied:
        mappings.append({"channel": "BitOutput11", "tag_key": key, "tag_id": tag["id"], "kind": "output", "data_type": "bool",
                         "type": "BinaryOutput", "channel_family": "Bit", "direction": "Output", "channel_index": 11, "offset": 0})
    contract["channel_counts"]["BitOutputCount"] = 12
    contract["unmapped_tag_keys"] = [k for k in contract["unmapped_tag_keys"] if k != key]
    contract["transport_confirmed"] = False
    contract["software_load_confirmed"] = False


def _verify_component_semantics(root: ET.Element, rows: dict) -> None:
    """Bind the observed signal semantics to actual components via their I/O keys."""
    expected = {
        "BitInput0": ("RetroreflectivePhotoelectricSensor", "$C_DefaultSensor_OM_Binary_NAME"),
        "BitInput1": ("DiffusePhotoelectricSensor", "$C_DefaultSensor_OM_Binary_NAME"),
        "BitInput2": ("DiffusePhotoelectricSensor", "$C_DefaultSensor_OM_Binary_NAME"),
        "BitInput3": ("DiffusePhotoelectricSensor", "$C_DefaultSensor_OM_Binary_NAME"),
        "BitInput4": ("RetroreflectivePhotoelectricSensor", "$C_DefaultSensor_OM_Binary_NAME"),
        "BitInput5": ("DiffusePhotoelectricSensor", "$C_DefaultSensor_OM_Binary_NAME"),
        "BitInput6": ("RetroreflectivePhotoelectricSensor", "$C_DefaultSensor_OM_Binary_NAME"),
        "BitInput7": ("DiffusePhotoelectricSensor", "$C_DefaultSensor_OM_Binary_NAME"),
        "BitInput8": ("RetroreflectivePhotoelectricSensor", "$C_DefaultSensor_OM_Binary_NAME"),
        "BitOutput2": ("PopUpWheelSorter", "$C_PopUpWheelSorter_OM_SingleBinary_NAME"),
        "BitOutput4": ("PopUpWheelSorter", "$C_PopUpWheelSorter_OM_SingleBinary_NAME"),
        "BitOutput6": ("PopUpWheelSorter", "$C_PopUpWheelSorter_OM_SingleBinary_NAME"),
    }
    for channel, semantics in expected.items():
        key = rows[channel]["tag_key"]
        owners = [o for o in root.findall("Object")
                  if any(io.get("Key") == key for io in o.findall("OperatingMode/GroupIO/*"))]
        require(len(owners) == 1, "baseline component ownership is not unique: " + channel)
        owner = owners[0]
        actual = (owner.find("Proxy").get("PrefabName"), owner.find("OperatingMode").get("Description"))
        require(actual == semantics, "baseline component signal semantics changed: " + channel)


def prepare_weight_baseline(config: dict, scene_dir: Path, inspection_path: Path, output: Path) -> dict:
    require(not output.exists(), "baseline output exists; choose a fresh directory")
    source_scene = scene_dir / "scene.factoryio"
    manifest = read_json(scene_dir / "manifest.json")
    contract = read_json(scene_dir / "io-contract.json")
    require(file_hash(source_scene) == manifest["scene_sha256"] == contract["scene_sha256"], "scene artifacts changed")
    root = ET.parse(source_scene).getroot()
    modes = [o.find("OperatingMode").get("Description") for o in root.findall("Object")
             if o.find("Proxy").get("PrefabName") == "ConveyorScale"]
    require(modes == ["$C_ConveyorScale_OM_Default20_NAME"], "baseline requires the observed 20 kg scale mode")
    configure_paced_emitter(root, contract, manifest)
    rows = {r["channel"]: r for r in contract["mappings"]}
    require(set(rows) == set(_SIGNALS), "baseline requires exact observed weight scene channel layout")
    tags = {t["key"]: t for t in contract["tags"]}
    require([tags[rows[c]["tag_key"]]["name"] for c in _SIGNALS] == _NAMES, "baseline tag roles changed")
    _verify_component_semantics(root, rows)
    require(all(r["offset"] == 0 for r in rows.values()), "baseline supports only inspected zero-offset transport")
    require(contract["driver_properties"] == {"ReadCoils": "False", "ReadHoldingRegisters": "False", "ScaleFactor": "100"}, "baseline driver mode/scaling differs")
    inspection = read_json(inspection_path)
    require(not inspection["diagnostics"], "source inspection is incomplete")
    baseline_config = deepcopy(config)
    template = Path(config["codesys"]["template_project"])
    source_sha = file_hash(template)
    # Bind inspection to the project actually opened by its bridge request/result.
    request = read_json(inspection_path.with_name("request.json"))
    result = read_json(inspection_path.with_name("result.json"))
    require(request["source_sha256"] == result["source_sha256"] == source_sha and result["request_id"] == request["request_id"], "inspection belongs to another baseline")
    device_paths = {r["device_path"] for r in inspection["device_mappings"] if r.get("id") == "13" and r.get("name") == "Coils Size"}
    require(len(device_paths) == 1, "expected one inspected Modbus server device")
    device_path = next(iter(device_paths))
    channels = [r for r in inspection["device_mappings"] if r["device_path"] == device_path and r["connector_id"] == "1"]
    parameters = {r["id"]: r for r in channels if "id" in r}
    def base(parameter_id, letter):
        match = re.fullmatch("%" + letter + "B([0-9]+)", parameters[parameter_id]["mapping"]["manual_iec_address"])
        require(match is not None, "inspected bit-array base is unsupported")
        return int(match.group(1))
    input_base, output_base = base("3000", "I"), base("4000", "Q")
    require(parameters["6"]["value"] == str(config["protocol"]["port"]), "configured Modbus port differs from inspected server")
    contract["protocol"] = deepcopy(config["protocol"])
    contract["transport_confirmed"] = False
    contract["wire_encodings"] = []
    bindings, endpoints, declarations = [], [], []
    for channel, name in _SIGNALS.items():
        row = rows[channel]; key = row["tag_key"]; type_ = "BOOL" if channel.startswith("Bit") else "WORD"
        variable = "Application.WeightIO." + name
        bindings.append({"tag_key": key, "variable": variable, "plc_type": type_})
        declarations.append("    " + name + " : " + type_ + ";")
        index, direction = row["channel_index"], row["direction"]
        prefix = "I" if direction == "Input" else "Q"
        if type_ == "BOOL":
            address = "%" + prefix + "X" + str((input_base if prefix == "I" else output_base) + index // 8) + "." + str(index % 8)
        else:
            address = "%" + prefix + "W" + str(index)
            contract["wire_encodings"].append({"tag_key": key, "format": "modbus_positive16", "scale_factor": 100 if tags[key]["data_type"] == "float" else 1,
                                                "logical_min": 0, "logical_max": 10 if tags[key]["data_type"] == "float" else 32767})
        endpoints.append({"tag_key": key, "variable": variable, "device_path": device_path, "connector_id": "1",
                          "manual_iec_address": address, "channel_type": direction, "bit_size": "1" if type_ == "BOOL" else "16"})
    program = {"schema_version": 1, "program_name": "PLC_PRG", "declaration": _DECLARATION, "implementation": _IMPLEMENTATION,
               "globals": [{"name": "WeightIO", "declaration": "VAR_GLOBAL\n" + "\n".join(declarations) + "\nEND_VAR"}],
               "bindings": bindings, "unresolved": ["Recheck active-high diffuse sensors, clear=True retroreflective exits/scale-entry, Stop permissive=True, and Auto/Start panel states in each new scene",
                   "Calibrate 20 kg scale: expected voltage-to-kg factor 2; verify measured Modbus WORD/100 matches voltage",
                   "Confirm UpTo=1 PLC-paced emission, single-item transport and physical exit routing in Factory I/O"],
               "notes": ["Authored by current assistant; external LLM not invoked", "500 ms weight stability, 30 s state timeout; [0,3), [3,6), [6,20) kg bins",
                         "Weight display is an integer in 0.01 kg units; counters saturate at 32767",
                         "Observed exit sensors are clear=True; normalize to occupancy before edge counting and empty checks",
                         "Observed PopUpWheelSorterBinary SendForward enables roller lift/motor on all three routes; lateral bits select steering",
                         "Emitter is mapped to BitOutput11, unforced and UpTo=1; PLC enables it only during feed",
                         "Simulation stop clears state; panel stop/Auto-off during a cycle latches fault until empty/reset"]}
    baseline_config["codesys"].update({"deploy": False, "transport_confirmed": False, "bindings": bindings,
        "wire_encodings": deepcopy(contract["wire_encodings"]),
        "verified_device_bindings": endpoints, "verified_template_sha256": None, "verified_transport_sha256": None,
        "device_patch": {"source_sha256": source_sha, "device_path": device_path, "connector_id": "1", "clear_existing_mappings": True,
                         "parameters": [{"id": id_, "name": parameters[id_]["name"], "expected_value": parameters[id_]["value"], "value": str(value)}
                                        for id_, value in (("13", 14), ("14", 12))], "bindings": endpoints}})
    task = read_json(scene_dir.parent / "task.json")
    task["assumptions"] = [s for s in task["assumptions"] if "离线" not in s]
    task["assumptions"].append("基于现有官方布局；本轮由当前助手构建控制基线，未调用外部模型")
    task["unresolved"] = list(program["unresolved"])
    output.mkdir(parents=True)
    scene_output = output / "scene"
    scene_output.mkdir()
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(scene_output / "scene.factoryio", encoding="utf-8", xml_declaration=True)
    contract["scene_sha256"] = manifest["scene_sha256"] = file_hash(scene_output / "scene.factoryio")
    manifest["scene_path"] = str((scene_output / "scene.factoryio").resolve())
    write_json(output / "task.json", task)
    write_json(output / "config.json", baseline_config)
    write_json(scene_output / "manifest.json", manifest)
    write_json(scene_output / "io-contract.json", contract)
    versions = version_tuple(task, contract["scene_sha256"], contract, None, baseline_config)
    write_json(scene_output / "versions.json", versions)
    export_program(program, contract, output / "plc")
    write_json(output / "plc" / "versions.json", version_tuple(task, contract["scene_sha256"], contract, program, baseline_config))
    report = {"schema_version": 1, "author": "current_assistant", "external_model_used": False, "source_project_sha256": source_sha,
              "inspection": str(inspection_path.resolve()), "scene_sha256": contract["scene_sha256"],
              "transport_sha256": transport_fingerprint(contract), "binding_count": len(bindings), "output": str(output.resolve()),
              "compiled": None, "live_simulation_passed": False, "unresolved": program["unresolved"]}
    write_json(output / "baseline-report.json", report)
    return report
