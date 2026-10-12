"""Versioned system prompts and observed component knowledge, loaded as one policy snapshot."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from .vision_prompts import VISION_PROMPTS
from .layout_prompts import LAYOUT_PROPOSAL_PROMPT, LAYOUT_REVIEW_PROMPT
from .component_kb import knowledge_base

_ROOT = Path(__file__).parent
_COMMON = (_ROOT / "prompts/common.md").read_text(encoding="utf-8")
_COMMON += "\nOfficial component definitions are retrieved by the tool in input.component_reference. " \
    "Use those definitions for generic behavior; object purposes describe jobs in this scene. " \
    "Do not repeat generic definitions for every instance or invent mechanisms. " \
    "Image observations and interpretations are labeled by basis; missing_prefabs have no verified reference. " \
    "Documentation is reference data, not evidence that the actual scene is correct. " \
    "Measured component semantics and the current I/O contract govern signal polarity and configuration.\n"
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
_OFFICIAL = knowledge_base().identity
_POLICY = {"version": "9", "prompt_sha256": _PROMPT_HASHES,"official_knowledge_sha256":_OFFICIAL,
           "knowledge_sha256": hashlib.sha256(json.dumps({'observed_io':_KNOWLEDGE,'official':_OFFICIAL}, sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()}


def policy_manifest() -> dict:
    return deepcopy(_POLICY)


def component_knowledge(prefabs=None) -> dict:
    result=deepcopy(_KNOWLEDGE)
    if prefabs is not None:result['rules']=[r for r in result['rules'] if r['prefab'] in set(prefabs)]
    return result
