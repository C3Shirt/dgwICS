"""Reviewed SFT/DPO export with exact-context, evidence, privacy and lineage checks."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import shutil

from .contracts import file_hash, nonempty, read_json, require, write_json
from .training import json_text, now, text_hash


# These are reviewer-attested scopes backed by immutable files, not model-generated grades.
REQUIRED_GATES = {
    "planner": {"task_contract", "design_review"},
    "scene": {"scene_load", "geometry_check", "process_check", "visual_layout"},
    "plc": {"plc_compile", "io_binding", "protocol_mapping", "runtime_acceptance", "visual_runtime"},
    "vision": {"current_evidence", "human_alignment"},
}
SPLITS = {"train", "validation", "test"}


def checked_file(base: Path, reference: dict) -> Path:
    require(isinstance(reference, dict), "evidence/asset reference must be object")
    path = Path(nonempty(reference.get("path"), "reference.path"))
    path = (base / path).resolve() if not path.is_absolute() else path.resolve()
    require(path.is_file() and file_hash(path) == reference.get("sha256"),
            "evidence/asset file missing or hash changed")
    return path


def exact_prompt_hash(record: dict) -> str:
    messages = record["messages"]
    require(len(messages) == 2 and messages[0].get("role") == "system" and messages[1].get("role") == "user",
            "candidate must contain system and user messages")
    content = messages[1]["content"]
    if record["images"]:
        require(isinstance(content, list) and len(content) == len(record["images"]) + 1 and
                content[0].get("type") == "text" and all(part == {"type": "image"} for part in content[1:]),
                "invalid multimodal input placeholders")
        content = content[0]["text"]
    require(isinstance(messages[0]["content"], str) and isinstance(content, str), "invalid prompt text")
    require(content == json_text(record["payload"]), "stored payload differs from model user text")
    return text_hash(json_text({"system": messages[0]["content"], "user_text": content,
        "images": [{"sha256": asset["sha256"], "mime_type": asset["mime_type"]} for asset in record["images"]]}))


def export_reviewed(raw_directories: list[Path], review_path: Path, output: Path) -> dict:
    """Reject invalid requested rows before writing; unreviewed candidates stay out of gold."""
    output = output.resolve()
    require(not output.exists(), "export output already exists; use a fresh directory")
    review_path = review_path.resolve()
    document = read_json(review_path)
    require(isinstance(document, dict) and document.get("schema_version") == 1, "review schema_version must be 1")
    require(all(isinstance(document.get(key), list) for key in ("candidate_reviews", "preference_reviews")),
            "review file requires candidate_reviews and preference_reviews arrays")
    require(document.get("purpose") == "human_training_review", "review purpose must be human_training_review")
    records = {}
    for directory in raw_directories:
        directory = directory.resolve()
        require(directory.is_dir(), "raw training directory missing")
        for path in sorted((directory / "candidates").glob("*.json")):
            record = read_json(path)
            require(record.get("schema_version") == 1 and record.get("kind") == "agent_candidate",
                    "unsupported raw candidate schema")
            identifier = nonempty(record.get("candidate_id"), "candidate_id")
            require(identifier not in records, "duplicate candidate_id across raw directories")
            records[identifier] = (record, directory)

    approved = {}
    split_by_lineage = {}
    assets = {}
    def human_review(value):
        require(value.get("reviewer_type") == "human", "explicit human reviewer required")
        nonempty(value.get("reviewer_id"), "reviewer_id")
        nonempty(value.get("reviewed_at"), "reviewed_at")
        nonempty(value.get("rationale"), "rationale")
        require(value.get("privacy_review_passed") is True and value.get("source_use_permitted") is True,
                "privacy and source permission reviews required")

    def register_asset(path: Path, digest: str) -> str:
        name = "assets/" + digest + path.suffix.lower()
        assets[name] = (path, digest)
        return name

    for review in document["candidate_reviews"]:
        require(isinstance(review, dict), "candidate review must be object")
        human_review(review)
        identifier = review.get("candidate_id")
        require(identifier in records and identifier not in approved, "unknown or duplicate reviewed candidate")
        record, directory = records[identifier]
        require(record.get("source_kind") == "live_model", "injected clients and fixtures cannot become training gold")
        require(record.get("redaction_count") == 0, "redacted candidates require a fresh sanitized model call")
        require(record.get("role") in REQUIRED_GATES, "unknown training role")
        require(record.get("response_text") is not None and record.get("output_origin") == "provider_content",
                "provider completion required; transport-only failures cannot be preference answers")
        require(text_hash(record["response_text"]) == record.get("response_sha256"), "candidate response changed")
        require(exact_prompt_hash(record) == record.get("prompt_sha256"), "candidate prompt changed")
        audit_context=record.get('recording_context')
        if audit_context is not None:
            from .contracts import canonical_hash
            require(isinstance(audit_context,dict),'recording context must be an object')
            require(canonical_hash(audit_context)==record.get('recording_context_sha256'),'recording context changed')
            require(audit_context.get('policy')==record.get('policy') and
                    (audit_context.get('handoff') or {}).get('versions',audit_context.get('versions',{}))==record.get('versions'),
                    'recording context metadata changed')
        handoff = (audit_context if audit_context is not None else record["payload"]).get("handoff") or {}
        require(record.get('handoff_id')==handoff.get('id'),'recording context handoff changed')
        require(record.get("input_artifacts", []) == handoff.get("inputs", []), "input references differ from prompt")
        require(review.get("prompt_sha256") == record["prompt_sha256"] and
                review.get("response_sha256") == record["response_sha256"], "review hashes are stale")
        group = nonempty(review.get("lineage_group"), "lineage_group")
        require(record.get("lineage_group") in (None, group), "review lineage conflicts with collection")
        split = review.get("split")
        require(split in SPLITS, "split must be train, validation or test")
        require(group not in split_by_lineage or split_by_lineage[group] == split, "lineage crosses dataset splits")
        split_by_lineage[group] = split
        require(review.get("status") in ("accepted", "rejected"), "review status must be accepted or rejected")
        gates = review.get("gates")
        require(isinstance(gates, dict) and bool(gates), "review must cite scoped evidence gates")
        for name, gate in gates.items():
            require(isinstance(gate, dict) and gate.get("result") in ("passed", "failed"), "invalid evidence gate")
            checked_file(review_path.parent, gate)
        if review["status"] == "accepted":
            require(record["status"] == "response_received" and isinstance(record.get("structured_output"), dict),
                    "accepted JSON role candidate must have successful structured completion")
            require(REQUIRED_GATES[record["role"]] <= gates.keys() and
                    all(gate["result"] == "passed" for gate in gates.values()), "accepted candidate lacks passed role gates")
        else:
            require(any(gate["result"] == "failed" for gate in gates.values()), "rejected candidate requires failure evidence")
        # Input identities are also verified: version changes cannot silently relabel old runs.
        for reference in record.get("input_artifacts", []):
            checked_file(directory, reference)
        image_names = []
        for asset in record["images"]:
            require(not Path(asset["path"]).is_absolute(), "image snapshot must be local to raw directory")
            path = checked_file(directory, asset)
            require(path.is_relative_to(directory), "image snapshot escapes raw directory")
            image_names.append(register_asset(path, asset["sha256"]))
        approved[identifier] = (record, review, image_names)

    sft, dpo, audit = [], [], []
    for identifier, (record, review, image_names) in approved.items():
        if review["status"] != "accepted":
            continue
        row = {"messages": deepcopy(record["messages"]) + [{"role": "assistant", "content": record["response_text"]}]}
        if image_names:
            row["images"] = image_names
        sft.append((review["split"], row))
        audit.append({"kind": "sft", "candidate_id": identifier, "split": review["split"],
                      "lineage_group": review["lineage_group"], "role": record["role"], "review": review,
                      "run_id": record["run_id"], "scenario_id": record.get("scenario_id"), "phase": record["phase"],
                      "versions": record["versions"], "policy": record["policy"],
                      "model_metadata": record["model_metadata"], "prompt_sha256": record["prompt_sha256"],
                      "response_sha256": record["response_sha256"]})

    pair_ids = set()
    for pair in document["preference_reviews"]:
        require(isinstance(pair, dict), "preference review must be object")
        human_review(pair)
        pair_id = nonempty(pair.get("pair_id"), "pair_id")
        require(pair_id not in pair_ids, "duplicate preference pair_id")
        pair_ids.add(pair_id)
        require(pair.get("chosen_id") in approved and pair.get("rejected_id") in approved,
                "preference pair requires two reviewed candidates")
        chosen, chosen_review, images = approved[pair["chosen_id"]]
        rejected, rejected_review, _ = approved[pair["rejected_id"]]
        require(chosen_review["status"] == "accepted" and rejected_review["status"] in ("accepted", "rejected"),
                "chosen candidate must be accepted")
        require(chosen["role"] == rejected["role"] and chosen["prompt_sha256"] == rejected["prompt_sha256"],
                "DPO requires exactly the same input context, including feedback and image content")
        require(chosen["response_sha256"] != rejected["response_sha256"], "DPO completions must differ")
        require(chosen_review["lineage_group"] == rejected_review["lineage_group"] and
                chosen_review["split"] == rejected_review["split"], "DPO pair lineage/split mismatch")
        require(pair.get("same_evaluation_conditions") is True, "preference requires matching evaluation conditions")
        checked_file(review_path.parent, pair.get("evaluation_context"))
        row = {"prompt": deepcopy(chosen["messages"]),
               "chosen": [{"role": "assistant", "content": chosen["response_text"]}],
               "rejected": [{"role": "assistant", "content": rejected["response_text"]}]}
        if images:
            row["images"] = images
        dpo.append((chosen_review["split"], row))
        audit.append({"kind": "dpo", "pair_id": pair_id, "chosen_id": pair["chosen_id"],
                      "rejected_id": pair["rejected_id"], "split": chosen_review["split"],
                      "lineage_group": chosen_review["lineage_group"], "role": chosen["role"], "review": pair})

    # All requested rows have passed validation before creating an output directory.
    output.mkdir(parents=True)
    for name, (path, digest) in assets.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        require(file_hash(destination) == digest, "export image hash changed during copy")
    for kind, rows in (("sft", sft), ("dpo", dpo)):
        for split in sorted(SPLITS):
            with (output / (kind + "-" + split + ".jsonl")).open("w", encoding="utf-8") as stream:
                for row_split, row in rows:
                    if row_split == split:
                        stream.write(json_text(row) + "\n")
    with (output / "audit.jsonl").open("w", encoding="utf-8") as stream:
        for item in audit:
            stream.write(json_text(item) + "\n")
    report = {"schema_version": 1, "created_at": now(), "raw_candidates": len(records),
        "reviewed_candidates": len(approved), "unreviewed_candidates": len(records) - len(approved),
        "sft_rows": len(sft), "dpo_pairs": len(dpo), "lineage_splits": split_by_lineage,
        "review_file_sha256": file_hash(review_path), "output": str(output),
        "limitations": ["Gate outcomes and preferences are human attestations backed by file hashes; the exporter does not rerun simulation.",
                         "Multimodal image paths require a training-loader/processor adapter before training."]}
    write_json(output / "export-report.json", report)
    return report
