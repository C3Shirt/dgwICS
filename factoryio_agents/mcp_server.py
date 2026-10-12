"""Local MCP facade over verified scene/HTTP/PLC tools. Run with --config PATH."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Literal

from .contracts import require, write_json
from .runtime import FactoryIOClient, collect_evidence
from .scene import build_catalog, build_scene
from .workflow import Workflow, load_config, run_lock


def create_server(config_path: Path):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        raise RuntimeError("Install optional MCP dependencies with pip install -e '.[mcp]'") from None
    config = load_config(config_path.resolve())
    workspace = config_path.resolve().parent
    artifact_root = (workspace / "artifacts" / "mcp-runs").resolve()
    server = FastMCP("factoryio-agents")

    def output_dir(name: str) -> Path:
        path = (artifact_root / name).resolve()
        require(path.is_relative_to(artifact_root) and path != artifact_root, "output must be inside artifacts/mcp-runs")
        return path

    @server.tool()
    def factoryio_catalog() -> dict:
        """List real prefab templates, configurations, tag types and original layout coordinates."""
        return build_catalog([Path(p) for p in config["scene_sources"]])

    @server.tool()
    def factoryio_build_scene(plan: dict, output_name: str) -> dict:
        """Generate a new scene revision from structured objects; returns structural checks, not software-load proof."""
        out = output_dir(output_name)
        with run_lock(out):
            require(not (out / "scene.factoryio").exists(), "revision exists; choose a new output_name")
            catalog = factoryio_catalog()
            result = build_scene(plan, catalog, out / "scene.factoryio")
            write_json(out / "scene-plan.json", plan)
            for name in ("manifest", "io_contract", "validation"):
                write_json(out / (name.replace("_", "-") + ".json"), result[name])
            return result

    @server.tool()
    def factoryio_tags() -> dict:
        """Read actual current Factory I/O tags and values; never writes actuator outputs."""
        return {"tags": FactoryIOClient(config["factoryio"]).tags(), "mode": "live"}

    @server.tool()
    def factoryio_collect(output_name: str, seconds: float = 1) -> dict:
        """Save timestamped live tag samples and configured frame captures; raw traces do not prove task completion."""
        require(0 <= seconds <= 60, "MCP capture is limited to 60 seconds per call")
        out = output_dir(output_name)
        with run_lock(out):
            require(not (out / "evidence.json").exists(), "evidence already exists; use another output_name")
            return collect_evidence(config["factoryio"], out, seconds=seconds)

    @server.tool()
    def codesys_compile_baseline(output_name: str) -> dict:
        """Copy and compile the configured baseline CODESYS project; does not download or start a PLC."""
        from .plc import run_codesys
        out = output_dir(output_name)
        with run_lock(out):
            return run_codesys(config["codesys"], out, deploy=False)

    @server.tool()
    def codesys_compile_program(program: dict, io_contract: dict, output_name: str) -> dict:
        """Write complete ST/GVLs to a baseline copy and return verified compiler errors, mappings and task calls.

        program includes declaration, implementation, globals and tag-key bindings.
        io_contract must describe the scene's actual protocol mappings. This tool
        never downloads or starts the PLC; compiled and started are separate.
        """
        from .plc import run_codesys
        out = output_dir(output_name)
        with run_lock(out):
            require(not any((out / name).exists() or (out / name).is_symlink()
                            for name in ("io-contract.json", "codesys-result.json", "project", "request.json", "result.json")),
                    "PLC revision exists; choose a new output_name")
            write_json(out / "io-contract.json", io_contract)
            result = run_codesys(config["codesys"], out, program=program, io_contract=io_contract, deploy=False)
            write_json(out / "codesys-result.json", result)
            return result

    @server.tool()
    def codesys_runtime_service(action: Literal["status", "start"] = "status") -> dict:
        """Query/start local CODESYS Control Win through Windows SCM. Requires service START permission.

        Running service does not certify a downloaded or running PLC program.
        This tool does not elevate, change startup type, download or stop code.
        """
        from .codesys_services import service_control
        return service_control(start=action == "start")

    @server.tool()
    def codesys_online_probe(output_name: str, connect: bool = False) -> dict:
        """Launch a headless CODESYS copy and inspect official online APIs and current gateways.

        connect=True requests loopback Keep login without calling download/start.
        Runtime invariance has NOT passed live verification: a running experiment
        stopped and counters reset during a probe. Use this for stopped commissioning
        diagnostics; use Factory I/O tag sampling for running-process observation.
        Default is capabilities only; a successful login does not prove runtime invariance.
        """
        from .plc import run_codesys
        out = output_dir(output_name)
        probe_config = dict(config["codesys"], online_probe={"connect": connect, "host": "127.0.0.1",
                                                           "port": 11740, "device_name": "Device"})
        with run_lock(out):
            require(not any((out / name).exists() or (out / name).is_symlink()
                            for name in ("online-probe-report.json", "project", "request.json", "result.json")),
                    "Online probe revision exists; choose a new output_name")
            result = run_codesys(probe_config, out, deploy=False)
            report = result.get("online_probe") or {}
            result["probe_passed"] = result.get("compiled") is True and report.get("status") == "ok"
            result["online_connected"] = report.get("connected") is True
            result["runtime_invariance_verified"] = False
            write_json(out / "online-probe-report.json", result)
            return result

    @server.tool()
    def workflow_prepare(requirement: str, output_name: str) -> dict:
        """Invoke configured Agent1/Agent2 models and generate a reviewable scene; stops before live deployment."""
        return Workflow(config, output_dir(output_name)).prepare(requirement)

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.example.json"))
    args = parser.parse_args()
    create_server(args.config).run(transport="stdio")


if __name__ == "__main__": main()
