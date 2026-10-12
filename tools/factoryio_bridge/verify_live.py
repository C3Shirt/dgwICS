"""Verify the real bridge, scene contents and tags; preserve each live response.

Loads the supplied scene after capturing the current scene. Unsaved scene edits
are rejected by the bridge. This does not start the PLC or certify sorting.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from client import call

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from factoryio_agents.runtime import FactoryIOClient, reconcile_tags


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def compare_objects(expected: list[dict], actual: list[dict]) -> dict:
    """Compare serialized identities and transforms, including quaternion sign."""
    by_key = {o["key"]: o for o in actual}
    expected_keys = {o["key"] for o in expected}
    issues = []
    if len(by_key) != len(actual):
        issues.append("duplicate live object keys")
    if len(expected_keys) != len(expected):
        issues.append("duplicate manifest object keys")
    for key in sorted(expected_keys - by_key.keys()):
        issues.append(f"missing object: {key}")
    for key in sorted(by_key.keys() - expected_keys):
        issues.append(f"unexpected object: {key}")
    for obj in expected:
        live = by_key.get(obj["key"])
        if live is None:
            continue
        if live["prefab"] != obj["prefab"]:
            issues.append(f"prefab mismatch: {obj['key']}")
        if live["position_grid"] != obj["position"]:
            issues.append(f"grid position mismatch: {obj['key']}")
        for field in ("fine_position", "fine_rotation"):
            if field in obj and (not math.isfinite(live[field]) or
                                 abs(live[field] - obj[field]) > 1e-5):
                issues.append(f"{field} mismatch: {obj['key']}")
        lhs, rhs = obj["rotation"], live["rotation"]
        if (len(lhs) != 4 or len(rhs) != 4 or
                not all(math.isfinite(v) for v in lhs + rhs) or
                not any(all(abs(a - sign * b) <= 1e-5 for a, b in zip(lhs, rhs))
                        for sign in (1, -1))):
            issues.append(f"rotation mismatch: {obj['key']}")
    return {"passed": not issues, "issues": issues,
            "expected_count": len(expected), "actual_count": len(actual)}


def verify(ipc: Path, manifest_path: Path, contract_path: Path,
           output: Path, web_api_url: str) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "mode": "live",
              "started_at": datetime.now(timezone.utc).isoformat(),
              "gates": {k: False for k in (
                  "bridge_status", "current_capture", "scene_loaded",
                  "objects_match", "tags_match", "loaded_capture")},
              "visual_review_passed": False, "physical_sorting_passed": False}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        contract = json.loads(contract_path.read_text(encoding="utf-8-sig"))
        scene = Path(manifest["scene_path"]).resolve()
        digest = hashlib.sha256(scene.read_bytes()).hexdigest()
        if digest != manifest["scene_sha256"] or digest != contract["scene_sha256"]:
            raise RuntimeError("scene, manifest and I/O contract versions differ")
        report["scene_path"], report["scene_sha256"] = str(scene), digest
        report["ipc_path"] = str(ipc.resolve())
        proxies = {p.attrib["Key"]: p for p in ET.fromstring(scene.read_bytes()).findall("Object/Proxy")}
        for obj in manifest["objects"]:
            proxy = proxies[obj["key"]]
            obj["fine_position"] = float(proxy.findtext("FinePosition", "0"))
            obj["fine_rotation"] = float(proxy.findtext("FineRotation", "0"))

        def invoke(label: str, action: str, **params) -> dict:
            response = call(ipc, action, **params)
            write(output / (label + ".json"), response)
            print(label + ": response saved", flush=True)
            return response["data"]

        initial = invoke("initial-status", "status")
        report["assembly_sha256"] = initial["assembly_sha256"]
        report["app_version"] = initial["app_version"]
        report["previous_scene_path"] = initial["scene_path"]
        report["gates"]["bridge_status"] = True
        invoke("current-capture", "capture", size=1)
        report["gates"]["current_capture"] = True
        loaded = invoke("scene-load", "load", scene_path=str(scene), scene_sha256=digest)
        if (Path(loaded["scene_path"]).resolve() != scene or
                loaded["loaded_scene_sha256"] != digest or loaded["scene_loading"]):
            raise RuntimeError("loaded scene receipt does not match requested file")
        report["gates"]["scene_loaded"] = True
        objects = compare_objects(manifest["objects"], loaded["objects"])
        write(output / "object-reconciliation.json", objects)
        report["gates"]["objects_match"] = objects["passed"]
        tags = FactoryIOClient({"web_api_url": web_api_url}).tags()
        write(output / "runtime-tags.json", tags)
        reconciliation = reconcile_tags(contract, tags)
        write(output / "tag-reconciliation.json", reconciliation)
        report["gates"]["tags_match"] = reconciliation["passed"]
        capture = invoke("loaded-capture", "capture", size=1)
        report["image_path"] = capture["image_path"]
        report["image_sha256"] = capture["image_sha256"]
        report["gates"]["loaded_capture"] = True
        report["automation_checks_passed"] = all(report["gates"].values())
    except Exception as error:
        report["automation_checks_passed"] = False
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write(output / "verification-report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ipc", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--io-contract", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--web-api-url", default="http://127.0.0.1:7410")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    report = verify(args.ipc, args.manifest, args.io_contract, args.out, args.web_api_url)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["automation_checks_passed"] else 1)


if __name__ == "__main__":
    main()
