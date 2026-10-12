# Four Agent Simulation Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for isolated module implementation and review. Keep the current workspace because it is not a Git checkout; do not initialize a repository or create an unrelated worktree.

**Goal:** Implement the approved four-role Factory I/O/CODESYS workflow with structured artifacts, real tool adapters, bounded repair and evidence-based validation.

**Architecture:** Python standard-library package; model roles exchange validated JSON. A deterministic orchestrator owns scene revisions, PLC artifacts and tool operations. Live execution and offline demonstration have separate result states.

**Tech Stack:** Python 3.11+, XML ElementTree, urllib, unittest, configured capture hooks, CODESYS Scripting.

---

## File responsibilities and tasks

- [x] Contracts and role implementation: `factoryio_agents/contracts.py`, `llm.py`, `agents.py`; validate required fields, object identity, feedback categories, protocol and finite numeric values. Model HTTP failures must have sanitized diagnostics.
- [x] Scene tools: `factoryio_agents/scene.py`; build a template catalog from existing files, materialize plans with UUID5 identities, coherent quaternion/basis, preserved configuration and valid driver references, extract I/O contracts. Test source preservation, cloned identities, invalid references, rotations, and ambiguous mappings.
- [x] PLC tools: `factoryio_agents/plc.py`, `factoryio_agents/codesys_bridge.py` (compatibility entry: `tools/codesys_bridge.py`); export complete declaration/implementation artifacts and compile in a copied template project through CODESYS Scripting. Reject missing/conflicting bindings and distinguish compiler results from process exit status. Test diagnostics and copy boundaries.
- [x] Evidence tools: `factoryio_agents/runtime.py`, `validation.py`; collect real Factory I/O tags, reconcile expected IDs/types, import/capture timestamped images, validate controlled weight-routing events. Test missing tags, units, duplicate/misrouted items, timeouts and stale evidence.
- [x] Orchestration/CLI: `factoryio_agents/workflow.py`, `__main__.py`; support catalog, prepare, demo, collect, review, compile and run. Persist state, bounded scene/runtime repair loops, reject stale feedback, stop clearly at unavailable capabilities. Add configuration examples and runnable offline demo.
- [x] Verification/docs: meaningful `unittest` scenarios, real read-only Web API check, XML inspection of generated output, README commands and a verified-versus-unverified run report.

## Concrete validation commands

```powershell
& 'C:\Python314\python.exe' -m unittest discover -s tests -v
& 'C:\Python314\python.exe' -m factoryio_agents catalog --config config.example.json --out artifacts/agent-catalog.json
& 'C:\Python314\python.exe' -m factoryio_agents demo --config config.example.json --out artifacts/agent-demo
& 'C:\Python314\python.exe' -m factoryio_agents collect --config config.example.json --out artifacts/live-probe --seconds 1
```

Expected: tests pass; catalog references installed source scenes; demo produces a generated scene with resolved Key references and demonstrably routed repairs, explicitly labelled offline; live probe records actual API values without writes. Real CODESYS baseline/ST-write/negative compilation was checked separately. Live model calls, current screenshots and scene loading remain unverified; see docs/implementation-verification.md.

- [x] Optional MCP facade/client and primary-source evaluation; two real inventories and a live Factory I/O tags call recorded.
