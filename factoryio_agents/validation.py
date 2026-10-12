"""Deterministic acceptance checks; a model verdict never proves process success."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import math

from .contracts import require


def validate_events(task: dict, evidence: dict, versions: dict, io_contract: dict | None = None) -> dict:
    require(evidence.get("versions") == versions, "acceptance evidence belongs to another revision")
    events = [r for r in evidence.get("records", []) if r.get("kind") == "item_event"]
    failures = []
    live = evidence.get("mode") == "live"
    records = {r.get("id"): r for r in evidence.get("records", []) if r.get("id")}
    if live:
        for event in events:
            elapsed = event.get("elapsed_seconds")
            refs = event.get("source_record_ids")
            try:
                captured = datetime.fromisoformat(event.get("timestamp", "").replace("Z", "+00:00"))
                timestamp_valid = captured.tzinfo is not None
            except (ValueError, TypeError, AttributeError):
                timestamp_valid = False
            valid = (timestamp_valid and isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                     and math.isfinite(elapsed) and elapsed >= 0 and isinstance(refs, list) and bool(refs)
                     and all(isinstance(ref, str) and ref in records and
                             records[ref].get("kind") in ("tag_sample", "plc_diagnostics") for ref in refs))
            if not valid:
                failures.append("live item event lacks timestamp or measured source references")
    rules = []
    for rule in task["acceptance"]:
        problems = []
        if rule['kind'] == 'signal_contract':
            from .signal_validation import validate_signal_trace
            result = validate_signal_trace(rule, task, evidence, io_contract)
            rules.append(result)
            failures.extend(result['issues'])
            continue
        elif rule["kind"] == "weight_routing":
            items = defaultdict(list)
            for event in events:
                items[event.get("item_id")].append(event)
            counts = Counter()
            if not items: problems.append("no measured item events; raw images/tags alone do not prove routing")
            for item_id, trajectory in items.items():
                if not item_id: problems.append("event without item_id"); continue
                measured = [e for e in trajectory if e.get("event") == "weighed"]
                exited = [e for e in trajectory if e.get("event") == "exited"]
                if len(measured) != 1 or len(exited) != 1:
                    problems.append(f"{item_id}: expected exactly one weighing and one exit"); continue
                weight = measured[0].get("weight_kg")
                if not isinstance(weight, (int, float)) or isinstance(weight, bool) or not math.isfinite(weight):
                    problems.append(f"{item_id}: invalid kg measurement"); continue
                selected = [b for b in rule["bins"] if b["lower_kg"] <= weight < b["upper_kg"]]
                if len(selected) != 1:
                    problems.append(f"{item_id}: weight outside configured bins"); continue
                bin_ = selected[0]
                if exited[0].get("exit") != bin_["exit"]:
                    problems.append(f"{item_id}: wrong exit, expected {bin_['exit']}"); continue
                begin = measured[0].get("elapsed_seconds")
                end = exited[0].get("elapsed_seconds")
                if not all(isinstance(t, (float, int)) and not isinstance(t, bool) and math.isfinite(t)
                           for t in (begin, end)) or not 0 <= end - begin <= rule["max_cycle_seconds"]:
                    problems.append(f"{item_id}: invalid order or cycle timeout"); continue
                if measured[0].get("measurement_verified") is not True or exited[0].get("tracking_verified") is not True:
                    problems.append(f"{item_id}: measurement/tracking provenance not verified"); continue
                counts[bin_["id"]] += 1
            for bin_ in rule["bins"]:
                if counts[bin_["id"]] < rule["min_items_per_bin"]:
                    problems.append(f"{bin_['id']}: insufficient verified samples")
            rules.append({"kind": rule["kind"], "passed": not problems, "counts": dict(counts), "issues": problems})
        else:
            if live and any(e.get("provenance_verified") is not True for e in events):
                problems.append("event sequence has unverified measurement provenance")
            def event_time(event):
                value = event.get("elapsed_seconds")
                return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else math.inf
            observed = [e.get("event") for e in sorted(events, key=event_time)]
            index = 0
            for event in observed:
                if index < len(rule["events"]) and event == rule["events"][index]: index += 1
            if index != len(rule["events"]): problems.append("required event sequence not observed")
            rules.append({"kind": rule["kind"], "passed": not problems, "issues": problems})
        failures.extend(problems)
    return {"passed": not failures and live, "checks_passed": not failures, "mode": evidence.get("mode"),
            "rules": rules, "issues": failures + ([] if live else ["offline evidence is not live simulation proof"])}


def events_from_single_item_trace(evidence: dict, config: dict) -> list[dict]:
    """Only use an explicitly isolated one-item-at-a-time experiment with calibrated weight.

    The collector cannot infer item identity in a concurrent conveyor stream. The caller
    must confirm isolation, polarity, scaling and signal roles before enabling this decoder.
    """
    require(config.get("single_item_confirmed") is True, "single-item isolation has not been confirmed")
    require(config.get("calibration_confirmed") is True, "weight calibration has not been confirmed")
    weight_key = config["weight_tag_key"]
    at_scale_key = config["at_scale_tag_key"]
    exits = config["exits"]
    scale = config["kg_per_raw_unit"]
    require(isinstance(scale, (int, float)) and not isinstance(scale, bool) and math.isfinite(scale) and scale > 0, "invalid weight scale")
    window = config.get("stable_samples", 3)
    tolerance = config.get("stable_tolerance_kg", 0.05)
    require(type(window) is int and window >= 2 and isinstance(tolerance, (int, float)) and
            not isinstance(tolerance, bool) and math.isfinite(tolerance) and tolerance >= 0, "invalid stability configuration")
    output, samples, sample_refs, previous_exits = [], [], [], {}
    item_id = None
    weighed = False
    counter = 0
    previous_at_scale = False
    for sample in evidence.get("records", []):
        if sample.get("kind") != "tag_sample": continue
        tags = {t["id"]: t for t in sample["tags"]}
        required = {weight_key, at_scale_key} | {e["tag_key"] for e in exits}
        require(required <= set(tags), "trace is missing required signals")
        require(all(tags[key].get("isForced") is False for key in required),
                "measurement/tracking signal is forced or its forced status is unknown")
        require(isinstance(sample.get("id"), str) and bool(sample["id"]), "sample has no evidence ID")
        at_scale = tags[at_scale_key]["value"] == config.get("at_scale_active", True)
        if at_scale and not previous_at_scale:
            require(item_id is None, "new item arrived before previous exit; tracking is not isolated")
            counter += 1; item_id = f"item-{counter}"; weighed = False; samples = []; sample_refs = []
        previous_at_scale = at_scale
        if at_scale and not weighed:
            raw = tags[weight_key]["value"]
            require(isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw), "invalid weight signal")
            samples.append(raw * scale)
            sample_refs.append(sample["id"])
            if len(samples) >= window and max(samples[-window:]) - min(samples[-window:]) <= tolerance:
                output.append({"id": f"weighed-{counter}", "kind": "item_event", "event": "weighed",
                    "item_id": item_id, "weight_kg": sum(samples[-window:]) / window,
                    "measurement_verified": True, "elapsed_seconds": sample["elapsed_seconds"],
                    "timestamp": sample["timestamp"], "source": "calibrated_single_item_trace",
                    "source_record_ids": sample_refs[-window:], "provenance_verified": True})
                weighed = True
        rising = []
        for exit_ in exits:
            active = tags[exit_["tag_key"]]["value"] == exit_.get("active_value", True)
            old = previous_exits.get(exit_["id"], active)
            if active and not old and item_id is not None:
                rising.append(exit_)
            previous_exits[exit_["id"]] = active
        if rising:
            for exit_ in rising:
                verified = weighed and len(rising) == 1
                output.append({"id": f"exited-{counter}-{exit_['id']}", "kind": "item_event", "event": "exited",
                    "item_id": item_id, "exit": exit_["id"], "tracking_verified": verified,
                    "elapsed_seconds": sample["elapsed_seconds"], "timestamp": sample["timestamp"],
                    "source": "isolated_exit_edge" if verified else "conflicting_or_unverified_exit_edges",
                    "source_record_ids": [sample["id"]], "provenance_verified": verified})
            item_id = None; samples = []; sample_refs = []; weighed = False
    return output
