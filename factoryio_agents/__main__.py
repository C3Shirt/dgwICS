"""Command-line entry point for scene tools, model roles, evidence and MCP probes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .agents import VisionAgent
from .contracts import canonical_hash, read_json, require, write_json
from .llm import ModelClient
from .training import RecordingClient, TrainingRecorder, configured_secret_values
from .runtime import attach_images, collect_evidence
from .scene import build_catalog, build_scene, validate_scene
from .workflow import Workflow, load_config, verify_artifact_versions


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("catalog", "demo", "weight-baseline", "prepare", "run", "collect", "compile", "build", "review", "mcp-probe", "inspect"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, default=Path("config.example.json"))
        sub.add_argument("--out", type=Path, required=name != "inspect")
        if name in ("prepare", "run"): sub.add_argument("--requirement", required=True)
        if name == "build": sub.add_argument("--plan", type=Path, required=True)
        if name == "collect":
            sub.add_argument("--seconds", type=float, default=1)
            sub.add_argument("--scene-dir", type=Path)
            sub.add_argument("--plc-dir", type=Path)
        if name == "compile":
            sub.add_argument("--program", type=Path)
            sub.add_argument("--io-contract", type=Path)
        if name == "weight-baseline":
            sub.add_argument("--scene-dir", type=Path, required=True)
            sub.add_argument("--inspection", type=Path, required=True)
        if name == "review":
            sub.add_argument("--scene-dir", type=Path, required=True)
            sub.add_argument("--task", type=Path, required=True)
            sub.add_argument("--plc-dir", type=Path)
            sub.add_argument("--evidence", type=Path, required=True)
            sub.add_argument("--image", action="append", type=Path, default=[])
            sub.add_argument("--phase", choices=("scene", "runtime"), default="scene")
        if name == "mcp-probe": sub.add_argument("--server", type=Path, required=True)
        if name == "inspect": sub.add_argument("scene", type=Path)
    return root


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "catalog":
            catalog = build_catalog([Path(p) for p in config["scene_sources"]])
            write_json(args.out, catalog)
            result = {"source_count": len(catalog["sources"]), "template_count": len(catalog["templates"]), "output": str(args.out)}
        elif args.command == "demo":
            from .demo import run_demo
            result = run_demo(config, args.out)
        elif args.command == "weight-baseline":
            from .weight_baseline import prepare_weight_baseline
            result = prepare_weight_baseline(config, args.scene_dir, args.inspection, args.out)
        elif args.command in ("prepare", "run"):
            workflow = Workflow(config, args.out)
            result = getattr(workflow, args.command)(args.requirement)
        elif args.command == "build":
            require(not (args.out / "scene.factoryio").exists(), "output revision exists")
            result = build_scene(read_json(args.plan), build_catalog([Path(p) for p in config["scene_sources"]]),
                                 args.out / "scene.factoryio")
            for name, value in result.items(): write_json(args.out / (name.replace("_", "-") + ".json"), value)
            write_json(args.out / "scene-plan.json", read_json(args.plan))
            result = {"scene_path": result["manifest"]["scene_path"], "validation": result["validation"]}
        elif args.command == "collect":
            require(args.plc_dir is None or args.scene_dir is not None, "--plc-dir requires --scene-dir")
            if args.scene_dir:
                _, contract, versions = verify_artifact_versions(args.scene_dir)
                require(canonical_hash(config) == versions["config"], "configuration changed after generation")
                if args.plc_dir:
                    versions, _ = runtime_revision(versions, args.plc_dir)
            else: contract, versions = None, None
            evidence = collect_evidence(config["factoryio"], args.out, versions, contract, args.seconds)
            result = {"mode": evidence["mode"], "records": len(evidence["records"]),
                      "reconciliation": evidence["reconciliation"], "evidence": str(args.out / "evidence.json")}
        elif args.command == "compile":
            from .plc import run_codesys
            result = run_codesys(config["codesys"], args.out,
                read_json(args.program) if args.program else None,
                read_json(args.io_contract) if args.io_contract else None, deploy=False)
            write_json(args.out / "runner-report.json", result)
        elif args.command == "review":
            manifest, contract, versions = verify_artifact_versions(args.scene_dir)
            require(canonical_hash(config) == versions["config"], "configuration changed after generation")
            task = read_json(args.task)
            require(canonical_hash(task) == versions["task"], "task was modified after generation")
            program = None
            if args.phase == "runtime":
                require(args.plc_dir is not None, "runtime review requires --plc-dir")
                versions, program = runtime_revision(versions, args.plc_dir)
            evidence = attach_images(read_json(args.evidence), args.image, versions)
            client = ModelClient(config["llm"])
            recorder = None
            training = config.get("training", {})
            if training.get("enabled", True):
                recorder = TrainingRecorder(args.out.parent / (args.out.stem + "-training-data"),
                    scenario_id=training.get("scenario_id"), lineage_group=training.get("lineage_group"),
                    secrets=configured_secret_values(config) + client.secret_values())
                client = RecordingClient(client, recorder)
            try:
                result = VisionAgent(client).review(task, manifest, contract, evidence, versions, args.phase, program)
            except Exception as error:
                if recorder:
                    recorder.event("review_contract_failed", {"error_type": type(error).__name__, "error": str(error)})
                raise
            write_json(args.out, result)
            if recorder:
                recorder.event("review_written", {"path": str(args.out.resolve()), "phase": args.phase, "versions": versions})
        elif args.command == "mcp-probe":
            from .mcp_client import probe
            result = probe(read_json(args.server))
            write_json(args.out, result)
            result = {"tool_count": result["tool_count"], "verified": result["verified"], "output": str(args.out)}
        else: result = validate_scene(args.scene)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if result.get("status") in ("failed", "needs_configuration", "needs_attention", "repair_limit_reached"):
            return 2
        if result.get("compiled") is False or result.get("passed") is False: return 2
        if result.get("reconciliation") and result["reconciliation"]["passed"] is False: return 2
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


def runtime_revision(scene_versions: dict, plc_dir: Path) -> tuple[dict, dict]:
    versions = read_json(plc_dir / "versions.json")
    require(all(versions[k] == scene_versions[k] for k in ("task", "scene", "io", "config")),
            "PLC belongs to another scene/task/config revision")
    require(versions.get("agent_policy") == scene_versions.get("agent_policy"), "PLC uses another agent policy")
    program = read_json(plc_dir / "program.json")
    require(canonical_hash(program) == versions["plc"], "PLC program was modified after generation")
    return versions, program


if __name__ == "__main__": sys.exit(main())
