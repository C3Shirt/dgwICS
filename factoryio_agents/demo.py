"""Explicitly scripted offline demonstration; never reports a live simulation pass."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from .agents import PLCAgent, PlannerAgent, SceneAgent
from .contracts import read_json, validate_feedback, validate_task, version_tuple, write_json
from .scene import build_catalog, build_scene, patch_plan
from .validation import validate_events
from .workflow import run_lock


class ScriptedClient:
    def __init__(self, task: dict, initial_plan: dict, fixed_plan: dict, program: dict):
        self.task, self.initial, self.fixed, self.program = task, initial_plan, fixed_plan, program
        self.calls = []

    def complete(self, role, system, payload, images=None):
        self.calls.append(role)
        if role == "planner": return deepcopy(self.task)
        if role == "scene": return deepcopy(self.fixed if payload.get("feedback") else self.initial)
        if role == "plc": return deepcopy(self.program)
        if role == "vision": return deepcopy(payload["scripted_feedback"])
        raise ValueError("unknown scripted role")


def demo_task(protocol: dict) -> dict:
    return {"schema_version": 1, "task_id": "weight-sorting-demo", "scenario_type": "weight_sorting",
        "requirement": "构建一个重量分拣 ICS 场景，通过 Modbus TCP 和 CODESYS 控制",
        "objects": [{"id": "scale", "purpose": "测量单个箱子的重量", "prefab": "ConveyorScale"},
                    {"id": "sorter", "purpose": "按重量选择出口", "prefab": "PopUpWheelSorter"}],
        "process": [{"id": "feed", "description": "单件进料"}, {"id": "weigh", "description": "稳定称重"},
                    {"id": "route", "description": "选择出口"}, {"id": "confirm", "description": "出口检测与计数"}],
        "control_rules": ["称重稳定后锁存重量和出口", "当前物料离开前不接纳下一件",
                          "启动、停止、复位和超时均有显式状态", "重量信号、通信编码和传感器极性需要实测确认"],
        "protocol": protocol, "assumptions": ["示例三档区间为 [0,3)、[3,6)、[6,20) kg；需要按实际任务调整",
            "例子使用官方重量分拣场景的现有设备与位置", "离线事件是固定测试输入，未运行物理仿真"],
        "unresolved": ["真实 PLC 设备映射和寄存器编码尚未验证"],
        "acceptance": [{"kind": "weight_routing", "bins": [
            {"id": "light", "lower_kg": 0, "upper_kg": 3, "exit": "left"},
            {"id": "medium", "lower_kg": 3, "upper_kg": 6, "exit": "front"},
            {"id": "heavy", "lower_kg": 6, "upper_kg": 20, "exit": "right"}],
            "max_cycle_seconds": 30, "min_items_per_bin": 1}]}


def demo_program(contract: dict) -> dict:
    # A compilable artifact exercises the PLC export boundary. Physical I/O bindings remain unresolved.
    tags = [t for t in contract["tags"] if t.get("object_id") is not None]
    names = {t["key"]: "signal_" + str(i) for i, t in enumerate(tags)}
    types = {"bool": "BOOL", "float": "REAL", "int": "DINT"}
    declaration = "PROGRAM PLC_PRG\nVAR\n    State : INT := 0;\n"
    declaration += "".join(f"    {names[t['key']]} : {types[t['data_type']]};\n" for t in tags)
    declaration += "END_VAR"
    return {"schema_version": 1, "program_name": "PLC_PRG", "declaration": declaration,
        "implementation": "(* Offline export fixture; physical control is deliberately unresolved. *)\nState := 0;",
        "globals": [], "bindings": [{"tag_key": t["key"], "variable": names[t["key"]],
                                      "plc_type": types[t["data_type"]]} for t in tags],
        "unresolved": ["示例 ST 仅验证工程导出；真实控制逻辑由配置的 PLC 模型生成",
                       "模板设备的 I/O 映射与重量编码需要核验"], "notes": ["scripted offline fixture"]}


def run_demo(config: dict, output: Path) -> dict:
    from .plc import export_program
    with run_lock(output):
        if (output / "demo-report.json").exists(): raise ValueError("demo output exists; use a new directory")
        catalog = build_catalog([Path(p) for p in config["scene_sources"]])
        weight_sources = {t["source_id"] for t in catalog["templates"] if t["prefab"] == "ConveyorScale"}
        if not weight_sources: raise ValueError("demo requires an observed ConveyorScale template")
        source_id = sorted(weight_sources)[0]
        templates = [t for t in catalog["templates"] if t["source_id"] == source_id]
        objects = []
        sensor_id = None
        for template in templates:
            id_ = "object-" + str(template["ordinal"])
            if template["prefab"] == "ConveyorScale": id_ = "scale"
            if template["prefab"] == "PopUpWheelSorter": id_ = "sorter"
            if any(tag["name"] == "At scale" for tag in template["tags"]):
                id_ = "at-scale-sensor"; sensor_id = id_
            objects.append({"id": id_, "template_id": template["template_id"], "position": template["position"]})
        if sensor_id is None: raise ValueError("demo requires an At scale sensor in the weight scene")
        correct = {"scene_id": "weight-sorting-demo", "base_source_id": source_id, "objects": objects}
        initial = patch_plan(correct, [{"object_id": sensor_id, "translate": [0, 10, 0]}])
        task = demo_task(config["protocol"])
        client = ScriptedClient(task, initial, correct, {})
        # Retain the explicitly legacy fixture; it does not claim the v2 process-contract gate.
        task = validate_task(client.complete("planner", "legacy scripted fixture", {"requirement": task["requirement"]}))
        plan = SceneAgent(client).plan(task, catalog)
        write_json(output / "task.json", task)
        write_json(output / "catalog.json", catalog)
        revisions = []
        for index in range(2):
            directory = output / f"scene-{index:03d}"
            directory.mkdir(parents=True, exist_ok=True)
            built = build_scene(plan, catalog, directory / "scene.factoryio")
            versions = version_tuple(task, built["manifest"]["scene_sha256"], built["io_contract"], None, config)
            write_json(directory / "scene-plan.json", plan)
            for name in ("manifest", "io_contract", "validation"):
                write_json(directory / (name.replace("_", "-") + ".json"), built[name])
            write_json(directory / "versions.json", versions)
            sensor = next(o for o in plan["objects"] if o["id"] == sensor_id)
            original = next(o for o in correct["objects"] if o["id"] == sensor_id)
            feedback = {"versions": versions, "phase": "scene", "verdict": "repair" if index == 0 else "pass",
                "findings": [] if index else [{"category": "geometry", "target": "scene", "object_ids": [sensor_id],
                    "observed": f"fixture sensor at {sensor['position']}", "expected": f"fixture reference {original['position']}",
                    "evidence_ids": ["fixture-layout"], "confidence": 1,
                    "suggestion": "restore fixture's reference sensor height",
                    "patches": [{"object_id": sensor_id, "position": original["position"]}]}]}
            client.complete("vision", "scripted fixture", {"scripted_feedback": feedback})
            validate_feedback(feedback, versions, built["manifest"], {"fixture-layout"})
            write_json(directory / "scene-feedback.json", {**feedback, "mode": "offline_demo", "source": "scripted_fixture"})
            revisions.append(built)
            if index == 0: plan = SceneAgent(client).plan(task, catalog, plan, feedback)
        client.program = demo_program(built["io_contract"])
        program = PLCAgent(client).program(task, built["io_contract"])
        export_program(program, built["io_contract"], output / "plc")
        versions = version_tuple(task, built["manifest"]["scene_sha256"], built["io_contract"], program, config)
        records = []
        for i, (weight, exit_) in enumerate(((1, "left"), (4, "front"), (8, "right"))):
            records.extend([{"id": f"weighed-{i}", "kind": "item_event", "event": "weighed", "item_id": str(i),
                "weight_kg": weight, "elapsed_seconds": i * 10, "measurement_verified": True},
                {"id": f"exited-{i}", "kind": "item_event", "event": "exited", "item_id": str(i), "exit": exit_,
                 "elapsed_seconds": i * 10 + 5, "tracking_verified": True}])
        evidence = {"versions": versions, "mode": "offline_demo", "records": records}
        acceptance = validate_events(task, evidence, versions)
        write_json(output / "event-fixture.json", evidence)
        write_json(output / "acceptance.json", acceptance)
        old_keys = {o["id"]: o["key"] for o in revisions[0]["manifest"]["objects"]}
        new_keys = {o["id"]: o["key"] for o in revisions[1]["manifest"]["objects"]}
        report = {"schema_version": 1, "mode": "offline_demo", "status": "offline_tools_verified",
            "roles_invoked": client.calls, "object_count": len(objects), "scene_revisions": 2,
            "stable_object_keys": old_keys == new_keys, "structural_validation": built["validation"],
            "acceptance_checks_passed": acceptance["checks_passed"], "live_simulation_passed": False,
            "models_called": False, "software_load_confirmed": False, "plc_compiled": False,
            "scene_path": str((output / "scene-001" / "scene.factoryio").resolve()),
            "limitations": ["scripted role responses and event inputs", "no model service configured",
                            "generated PLC artifact is an export fixture, not weight-control implementation"]}
        write_json(output / "demo-report.json", report)
        return report
