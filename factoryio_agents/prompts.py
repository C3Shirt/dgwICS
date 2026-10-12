"""Versioned system prompts and observed component knowledge, loaded as one policy snapshot."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from .vision_prompts import VISION_PROMPTS
from .layout_prompts import LAYOUT_PROPOSAL_PROMPT, LAYOUT_REVIEW_PROMPT

_ROOT = Path(__file__).parent
_COMMON = (_ROOT / "prompts/common.md").read_text(encoding="utf-8")
PROMPTS = {role: _COMMON + "\n" + (_ROOT / f"prompts/{role}.md").read_text(encoding="utf-8")
           for role in ("planner", "scene", "plc", "supervisor")}
_KNOWLEDGE = json.loads((_ROOT / "knowledge/component-semantics.json").read_text(encoding="utf-8"))
_PROMPT_HASHES = {role: hashlib.sha256(p.encode()).hexdigest() for role, p in PROMPTS.items()}
_VISION_HASHES = {"vision_" + ("layout" if phase == "scene" else phase): hashlib.sha256(p.encode()).hexdigest()
                  for phase, p in VISION_PROMPTS.items()}
_PROMPT_HASHES.update(_VISION_HASHES)
# Preserve a role-level identity for existing cache/provenance consumers. It is a
# digest of two independent policies, not a combined prompt sent to the model.
_PROMPT_HASHES["vision"] = hashlib.sha256(json.dumps(_VISION_HASHES, sort_keys=True).encode()).hexdigest()
_PROMPT_HASHES.update({key:hashlib.sha256(prompt.encode()).hexdigest() for key,prompt in
    [('scene_layout_proposal',LAYOUT_PROPOSAL_PROMPT),('planner_layout_review',LAYOUT_REVIEW_PROMPT)]})
_POLICY = {"version": "7", "prompt_sha256": _PROMPT_HASHES,
           "knowledge_sha256": hashlib.sha256(json.dumps(_KNOWLEDGE, sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()}


def policy_manifest() -> dict:
    return deepcopy(_POLICY)


def component_knowledge() -> dict:
    return deepcopy(_KNOWLEDGE)
