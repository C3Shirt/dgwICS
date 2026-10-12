"""Validated interchange contracts shared by the four roles and tools."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any


class ContractError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def finite(value: Any, field: str) -> float:
    require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{field}: expected number")
    require(math.isfinite(value), f"{field}: expected finite number")
    return float(value)


def nonempty(value: Any, field: str) -> str:
    require(isinstance(value, str) and bool(value.strip()), f"{field}: expected nonempty string")
    return value


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path: Path | str, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def acceptance_array(value):
    """Lossless adapter for a single rule; it never creates or removes a criterion."""
    if isinstance(value, dict) and value.get('kind') in ('event_sequence','weight_routing','signal_contract'):
        return [copy.deepcopy(value)]
    require(isinstance(value, list), 'acceptance must be an array of rule objects')
    return copy.deepcopy(value)


def validate_task(value: dict, *, specification_only: bool = False) -> dict:
    require(isinstance(value, dict), "task must be an object")
    result = copy.deepcopy(value)
    require(type(result.get("schema_version")) is int and result["schema_version"] == 1, "task schema_version must be 1")
    task_id = nonempty(result.get("task_id"), "task_id")
    require(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id) is not None, "task_id must be a safe identifier")
    nonempty(result.get("requirement"), "requirement")
    for field in ("objects", "process", "control_rules") + (() if specification_only else ('acceptance',)):
        require(isinstance(result.get(field), list) and bool(result[field]), f"{field}: expected nonempty array")
    for field in ("assumptions", "unresolved"):
        require(isinstance(result.get(field), list), f"{field}: expected array")
    ids = []
    for obj in result["objects"]:
        require(isinstance(obj, dict), "objects entries must be objects")
        ids.append(nonempty(obj.get("id"), "object.id"))
        nonempty(obj.get("purpose"), "object.purpose")
        nonempty(obj.get("prefab"), "object.prefab")
    require(len(ids) == len(set(ids)), "duplicate task object IDs")
    for step in result["process"]:
        require(isinstance(step, dict), "process entry must be object")
        nonempty(step.get("id"), "process.id")
        nonempty(step.get("description"), "process.description")
    require(len(result["process"]) == len({s["id"] for s in result["process"]}), "duplicate process IDs")
    if "execution_contract" in result:
        from .coordination import validate_execution_contract
        validate_execution_contract(result)
    for field in ("control_rules", "assumptions", "unresolved"):
        require(all(isinstance(v, str) and v.strip() for v in result[field]), f"{field}: expected nonempty strings")
    from .verification import validate_verification_tasks
    validate_verification_tasks(result)
    protocol = result.get("protocol", {})
    require(isinstance(protocol, dict), "protocol must be object")
    require(protocol.get("name") == "modbus_tcp", "first implementation supports modbus_tcp only")
    require(protocol.get("factoryio_role") == "client" and protocol.get("plc_role") == "server",
            "supported role pair: Factory I/O client / CODESYS server")
    nonempty(protocol.get("host"), "protocol.host")
    require(type(protocol.get("port")) is int and 1 <= protocol["port"] <= 65535, "invalid protocol.port")
    require(type(protocol.get("unit_id")) is int and 0 <= protocol["unit_id"] <= 255, "invalid protocol.unit_id")
    if specification_only:
        return result
    for rule in result["acceptance"]:
        require(isinstance(rule, dict) and rule.get("kind") in ("weight_routing", "event_sequence", "signal_contract"),
                "unsupported acceptance kind")
        if rule['kind'] == 'signal_contract':
            from .signal_validation import validate_signal_contract
            validate_signal_contract(rule, result)
        elif rule["kind"] == "weight_routing":
            bins = rule.get("bins")
            require(isinstance(bins, list) and len(bins) >= 2, "weight_routing needs at least two bins")
            previous = None
            bin_ids = set()
            for bin_ in bins:
                require(isinstance(bin_, dict), "bin must be object")
                id_ = nonempty(bin_.get("id"), "bin.id")
                require(id_ not in bin_ids, "duplicate bin.id")
                bin_ids.add(id_)
                low = finite(bin_.get("lower_kg"), "lower_kg")
                high = finite(bin_.get("upper_kg"), "upper_kg")
                require(0 <= low < high, "invalid weight interval")
                require(previous is None or low == previous, "weight bins must be ordered and contiguous")
                previous = high
                nonempty(bin_.get("exit"), "bin.exit")
            require(finite(rule.get("max_cycle_seconds"), "max_cycle_seconds") > 0, "cycle timeout must be positive")
            require(type(rule.get("min_items_per_bin")) is int and rule["min_items_per_bin"] > 0,
                    "min_items_per_bin must be positive integer")
        else:
            require(isinstance(rule.get("events"), list) and len(rule["events"]) >= 2, "event sequence too short")
            for event in rule["events"]:
                nonempty(event, "event name")
    return result


def validate_feedback(value: dict, versions: dict, manifest: dict, evidence_ids: set[str]) -> dict:
    require(isinstance(value, dict), "feedback must be an object")
    require(value.get("versions") == versions, "stale or mismatched feedback versions")
    require(value.get("phase") in ("scene", "runtime"), "feedback phase must be scene or runtime")
    require(value.get("verdict") in ("pass", "repair", "inconclusive"), "invalid feedback verdict")
    findings = value.get("findings")
    require(isinstance(findings, list), "feedback findings must be an array")
    object_ids = {obj["id"] for obj in manifest["objects"]}
    routes = {"geometry": "scene", "configuration": "scene", "logic": "plc",
              "communication": "tools", "requirements": "planner", "unknown": "investigate"}
    for finding in findings:
        require(isinstance(finding, dict), "finding must be an object")
        category = finding.get("category")
        require(category in routes, "invalid finding category")
        require(finding.get("target") == routes[category],
                f"finding category {category} requires target='{routes[category]}' (agent role), "
                f"got {finding.get('target')!r}; object IDs belong in object_ids, not target")
        confidence = finite(finding.get("confidence"), "confidence")
        require(0 <= confidence <= 1, "confidence must be between 0 and 1")
        refs = finding.get("evidence_ids")
        require(isinstance(refs, list) and bool(refs) and set(refs) <= evidence_ids,
                "finding must reference actual evidence")
        nonempty(finding.get("observed"), "finding.observed")
        nonempty(finding.get("expected"), "finding.expected")
        require(isinstance(finding.get("object_ids"), list), "finding.object_ids must be array")
        for object_id in finding["object_ids"]:
            require(object_id in object_ids, f"unknown object: {object_id}")
        if "patches" in finding:
            require(finding["target"] == "scene" and isinstance(finding["patches"], list),
                    "only scene findings can propose scene patches")
            require(all(isinstance(p, dict) and p.get("object_id") in object_ids for p in finding["patches"]), "patch targets unknown object")
    if value["verdict"] == "repair":
        require(bool(findings), "repair verdict requires findings")
    if value["verdict"] == "pass":
        require(not findings, "pass verdict cannot contain findings")
    return copy.deepcopy(value)


def version_tuple(task: dict, scene_sha: str, io_contract: dict, plc: dict | None, config: dict) -> dict:
    # Secret values are kept in environment variables, not this configuration.
    from .prompts import policy_manifest
    return {"task": canonical_hash(task), "scene": scene_sha, "io": canonical_hash(io_contract),
            "plc": canonical_hash(plc) if plc else None, "config": canonical_hash(config),
            "agent_policy": canonical_hash(policy_manifest())}
