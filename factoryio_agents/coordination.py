"""Deterministic handoffs, component contracts and diagnostic context selection."""
from copy import deepcopy
from pathlib import Path

from .contracts import canonical_hash, finite, nonempty, require, file_hash
from .prompts import component_knowledge, policy_manifest


def component_context(configuration: dict) -> dict:
    """Expose operating mode and editable settings without repeated XML/tag payloads."""
    nodes = configuration.get("nodes", [])
    component = next((n for n in nodes if n["element"] == "ComponentIO"), {})
    modes = [n for n in nodes if n["element"] == "OperatingMode"]
    current = component.get("attributes", {}).get("CurrentOperatingMode")
    mode = modes[int(current)].get("attributes", {}).get("Description") if (
        isinstance(current, str) and current.isdigit() and int(current) < len(modes)) else None
    settings = []
    groups = []
    if mode is not None:
        def tag_groups(node, group=None):
            if node["element"] == "GroupIO": group = node.get("attributes", {}).get("Description")
            if node["element"] in ("BinaryInput", "BinaryOutput", "AnalogueInput", "AnalogueOutput",
                                    "FloatInput", "FloatOutput", "IntInput", "IntOutput"):
                groups.append(group)
            for child in node.get("children", []): tag_groups(child, group)
        tag_groups(modes[int(current)])
    def visit(node, path):
        if node["element"] in ("Emitter", "Sensor", "Encoder", "Bases", "Parts", 'BinaryInput', 'BinaryOutput',
                              'AnalogueInput', 'AnalogueOutput', 'FloatInput', 'FloatOutput', 'IntInput', 'IntOutput'):
            settings.append({"path": path, "attributes": deepcopy(node.get("attributes", {}))})
        counts = {}
        for child in node.get("children", []):
            counts[child["element"]] = counts.get(child["element"], 0) + 1
            visit(child, path + "/" + child["element"] + f"[{counts[child['element']]}]")
    root_counts = {}
    for node in nodes:
        name = node['element']; root_counts[name] = root_counts.get(name, 0) + 1
        repeated = sum(n['element'] == name for n in nodes) > 1
        visit(node, name + (f'[{root_counts[name]}]' if repeated else ''))
    io_nodes = {'BinaryInput', 'BinaryOutput', 'AnalogueInput', 'AnalogueOutput',
                'FloatInput', 'FloatOutput', 'IntInput', 'IntOutput'}
    is_io = lambda s: s['path'].split('/')[-1].split('[')[0] in io_nodes
    # Preserve component settings order for existing consumers; append editable I/O paths.
    settings = [s for s in settings if not is_io(s)] + [s for s in settings if is_io(s)]
    return {"current_mode": current, "operating_mode": mode, "settings": settings, "tag_groups": groups}


def attach_component_semantics(manifest: dict, contract: dict, app_version: str | None) -> None:
    knowledge = component_knowledge()
    semantics = []
    for obj in manifest["objects"]:
        context = component_context(obj.get("configuration", {}))
        obj["component_context"] = context
        for tag in obj["tags"]:
            for rule in knowledge["rules"]:
                if rule["prefab"] == "PopUpWheelSorter" and (context["tag_groups"] != ["Left", "Right", "Forward"]
                    or len(obj["tags"]) != 3 or any(t["kind"] != "output" or t["data_type"] != "bool" for t in obj["tags"])):
                    continue
                if (obj["prefab"] == rule["prefab"] and context["operating_mode"] == rule["operating_mode"]
                    and context["current_mode"] == rule["current_mode"] and tag["index"] == rule["tag_index"]
                    and tag["kind"] == rule["kind"] and tag["data_type"] == rule["data_type"]
                    and ("group" not in rule or (tag["index"] < len(context["tag_groups"]) and
                         context["tag_groups"][tag["index"]] == rule["group"]))):
                    semantics.append({"tag_key": tag["key"], "object_id": obj["id"],
                        "prefab": obj["prefab"], "operating_mode": context["operating_mode"],
                        "semantics": deepcopy(rule["semantics"]),
                        "app_version_confirmed": app_version == knowledge["verified_app_version"],
                        "verified_app_version": knowledge["verified_app_version"],
                        "evidence": knowledge["evidence"]})
    contract["component_semantics"] = semantics
    contract["component_knowledge_sha256"] = policy_manifest()["knowledge_sha256"]


def validate_execution_contract(task: dict) -> dict:
    value = task.get("execution_contract")
    require(isinstance(value, dict), "planner must provide execution_contract")
    require(value.get("tracking_mode") in ("single_item", "concurrent"), "unknown item tracking mode")
    require(type(value.get("max_in_flight")) is int and value["max_in_flight"] > 0, "invalid max_in_flight")
    objects = {o["id"]: o for o in task["objects"]}
    feed = value.get("feed", {})
    require(isinstance(feed, dict) and feed.get("object_id") in objects, "feed references unknown object")
    require(objects[feed["object_id"]]["prefab"] == "Emitter", "feed must reference an Emitter")
    require(feed.get("control") in ("plc", "scene"), "unknown feed authority")
    require(type(feed.get("items_per_enable")) is int and feed["items_per_enable"] > 0, "invalid feed limit")
    if value["tracking_mode"] == "single_item":
        require(value["max_in_flight"] == feed["items_per_enable"] == 1 and feed["control"] == "plc",
                "single-item contract requires PLC-controlled one-item feeding")
    for name in ("startup", "shutdown"):
        require(isinstance(value.get(name), list) and bool(value[name]), f"execution_contract.{name} is empty")
        for text in value[name]: nonempty(text, name)
    steps = value.get("steps")
    require(isinstance(steps, list) and bool(steps), "execution steps are empty")
    ids = []
    for step in steps:
        require(isinstance(step, dict), "execution step must be object")
        ids.append(nonempty(step.get("process_id"), "process_id"))
        require(finite(step.get("timeout_seconds"), "step timeout") > 0, "step timeout must be positive")
        for name in ("preconditions", "commands", "completion"):
            require(isinstance(step.get(name), list) and bool(step[name]), f"execution step {step['process_id']}.{name} is empty")
            for text in step[name]: nonempty(text, name)
    require(len(ids) == len(set(ids)) and set(ids) == {s["id"] for s in task["process"]},
            "execution steps must cover each process ID exactly once")
    return value


def check_process_scene(task: dict, manifest: dict, contract: dict) -> dict:
    """A prose single-item assumption cannot override an uncontrolled/continuous emitter."""
    value = task.get("execution_contract")
    if value is None:
        return {"passed": True, "scope": "legacy_task", "issues": []}
    validate_execution_contract(task)
    issues = []
    feed = value["feed"]
    obj = next((o for o in manifest["objects"] if o["id"] == feed["object_id"]), None)
    if obj is None or obj["prefab"] != "Emitter":
        issues.append("planned feed object is not an Emitter in this scene")
    else:
        context = component_context(obj.get("configuration", {}))
        settings = next((s["attributes"] for s in context["settings"] if s["path"] == "Emitter"), {})
        if settings.get("UpTo") != str(feed["items_per_enable"]):
            issues.append("Emitter.UpTo differs from the process feed limit")
        if feed["control"] == "plc":
            tags = [t for t in obj["tags"] if t["kind"] == "output" and t["data_type"] == "bool"]
            if len(tags) != 1:
                issues.append("PLC feed requires one observed binary Emitter output")
            else:
                tag = tags[0]
                mappings = [m for m in contract["mappings"] if m["tag_key"] == tag["key"]]
                if tag.get("use_forced_value") is not False:
                    issues.append("PLC-controlled Emitter output is forced or force status is unknown")
                if len(mappings) != 1 or mappings[0]["kind"] != "output":
                    issues.append("Emitter output needs a unique driver mapping")
    return {"passed": not issues, "scope": "process_scene_invariants", "issues": issues,
            "target": "scene", "execution_contract_sha256": canonical_hash(value)}


def make_handoff(sender: str, recipient: str, phase: str, versions: dict, inputs: list[Path],
                 expected_output: str, history: list[dict] | None = None) -> dict:
    # Hash actual committed inputs, not paths supplied by a model.
    packet = {"schema_version": 1, "sender": sender, "recipient": recipient, "phase": phase,
        "versions": deepcopy(versions), "policy": policy_manifest(),
        "inputs": [{"path": str(p.resolve()), "sha256": file_hash(p)} for p in inputs],
        "expected_output": expected_output, "recent_repairs": deepcopy((history or [])[-4:]),
        "constraints": ["complete replacement artifact", "preserve stable IDs and unaffected behavior",
                        "no physical input forcing", "tool evidence required for every completion claim"]}
    packet["id"] = canonical_hash(packet)
    return packet


def artifact_digest(value: dict) -> str:
    # A new correlation ID is not a substantive code/scene repair.
    fields = ("program_name", "declaration", "implementation", "globals", "bindings") if "program_name" in value else (
        "scene_id", "base_source_id", "objects", "mappings")
    return canonical_hash({k: value[k] for k in fields if k in value})


def select_review_evidence(evidence: dict, max_samples: int = 40) -> dict:
    require(type(max_samples) is int and max_samples >= 2, "review sample budget must be integer >= 2")
    samples = [r for r in evidence.get("records", []) if r.get("kind") == "tag_sample"]
    other = [r for r in evidence.get("records", []) if r.get("kind") != "tag_sample"]
    selected = set()
    refs = {ref for r in other for ref in r.get("source_record_ids", [])}
    selected.update(i for i, r in enumerate(samples) if r["id"] in refs)
    if samples: selected.update((0, len(samples) - 1))
    edges, priority, critical, previous = [], [], [], {}
    for i, record in enumerate(samples):
        current = {t["id"]: t for t in record.get("tags", [])}
        changed = False
        for key in current.keys() | previous.keys():
            tag, old = current.get(key), previous.get(key)
            if i == 0: continue
            if tag is None or old is None or any(tag.get(k) != old.get(k) for k in
                    ("isForced", "openCircuit", "shortCircuit")):
                critical.extend((i - 1, i)); changed = True
            elif isinstance(tag.get("value"), (bool, int)) and tag.get("value") != old.get("value"):
                changed = True
                if "fault" in tag.get("name", "").lower():
                    critical.extend((i - 1, i))
                elif "count" in tag.get("name", "").lower():
                    priority.extend((i - 1, i))
        if changed: edges.extend((i - 1, i))
        previous = current
    # Keep all item-event provenance even when it exceeds the soft prompt budget.
    selected.update(critical)
    for i in dict.fromkeys(priority):
        if len(selected) >= max_samples: break
        selected.add(i)
    remaining_edges = [i for i in dict.fromkeys(edges) if i not in selected]
    slots = max(0, max_samples - len(selected))
    if len(remaining_edges) <= slots: selected.update(remaining_edges)
    elif slots:
        selected.update(remaining_edges[i * (len(remaining_edges) - 1) // max(1, slots - 1)] for i in range(slots))
    remaining = [i for i in range(len(samples)) if i not in selected]
    slots = max(0, max_samples - len(selected))
    if slots and remaining:
        selected.update(remaining[i * (len(remaining) - 1) // max(1, slots - 1)] for i in range(min(slots, len(remaining))))
    return {**evidence, "records": other + [samples[i] for i in sorted(selected)],
        "raw_tag_sample_count": len(samples), "review_selection": {
            "strategy": "event_sources_then_force_fault_counter_edges_then_discrete_edges",
            "selected_samples": len(selected), "omitted_samples": len(samples) - len(selected),
            "omitted_transition_samples": len(set(edges) - selected), "soft_max_samples": max_samples,
            "raw_evidence_retained": True}}


def validate_diagnoses(feedback: dict, task: dict, io_contract: dict, records: list[dict]) -> None:
    steps = {s["id"] for s in task["process"]}
    keys = {t["key"] for t in io_contract["tags"]}
    kinds = {r["id"]: r["kind"] for r in records}
    for finding in feedback["findings"]:
        diagnosis = finding.get("diagnosis")
        require(isinstance(diagnosis, dict), "review finding needs a structured diagnosis")
        require(diagnosis.get("process_step") in steps or diagnosis.get("process_step") == "scene_setup",
                "diagnosis references unknown process step")
        signals = diagnosis.get("signal_keys")
        require(isinstance(signals, list) and all(isinstance(k, str) and k in keys for k in signals),
                "diagnosis references unknown signal key")
        require(diagnosis.get("causal_status") in ("hypothesis", "supported"), "invalid causal status")
        for name in ("hypothesis", "discriminating_check", "expected_result"):
            nonempty(diagnosis.get(name), name)
        for name in ("alternatives", "repair_scope", "retest"):
            require(isinstance(diagnosis.get(name), list) and bool(diagnosis[name]), f"diagnosis.{name} is empty")
            for value in diagnosis[name]: nonempty(value, name)
        if finding["target"] == "plc" and diagnosis["causal_status"] == "supported":
            require(bool(signals) and any(kinds.get(ref) in ("tag_sample", "plc_diagnostics")
                    for ref in finding["evidence_ids"]), "supported PLC diagnosis needs measured/PLC evidence")
        if feedback["verdict"] == "repair":
            require(diagnosis["causal_status"] == "supported", "hypothesis requires investigation before automatic repair")


def model_review_evidence(evidence: dict, max_samples: int = 40, io_contract=None, compact_status=False) -> dict:
    """Compact a labeled model view, preserving full raw evidence and measurement results.

    Item-event provenance and fault edges retain their original priority. Signal-contract aggregates
    need not bring every hold sample into the prompt: counts/bounds/failures remain authoritative,
    while reference previews point to the complete immutable handoff input.
    """
    prepared = deepcopy(evidence)
    def preview(value):
        if isinstance(value,list): return [preview(v) for v in value]
        if not isinstance(value,dict): return value
        result = {k:preview(v) for k,v in value.items()}
        fields = {}
        for key in ('source_record_ids','evaluated_hold_record_ids'):
            refs = value.get(key)
            if isinstance(refs,list) and len(refs)>8:
                result[key] = refs[:4]+refs[-4:]
                fields[key] = {'original_count':len(refs),'omitted_count':len(refs)-8,
                               'full_list_sha256':canonical_hash(refs)}
        if fields: result['reference_list_projection'] = fields
        issues=value.get('issues')
        if isinstance(issues,list) and all(isinstance(v,str) for v in issues) and len(set(issues))<len(issues):
            counts={v:issues.count(v) for v in dict.fromkeys(issues)}
            result['issues']=list(counts)
            result['issue_occurrence_counts']=counts
        return result
    for i,row in enumerate(prepared.get('records',[])):
        if row.get('kind')=='acceptance_result' and any(
                r.get('kind')=='signal_contract' for r in row.get('result',{}).get('rules',[])):
            prepared['records'][i] = preview(row)
        elif row.get('kind')=='plc_diagnostics':
            result=row.get('result',{})
            row['result']={k:result[k] for k in ('compiled','started','bindings_verified','task_program_verified',
                'transport_verified','error_count','warning_count','diagnostics','project_path','source_sha256','source_unchanged',
                'source','verified','source_program_sha256','source_project_sha256','observed_at','expressions',
                'deploy_requested','physical_acceptance_passed','response_path') if k in result}
            if isinstance(result.get('online_probe'),dict):
                probe=result['online_probe']
                row['result']['online_probe']={k:probe[k] for k in ('status','connected','application_state','values',
                    'monitor_error','error','action','reset_mode','reset_mode_confirmed','stage') if k in probe}
            row['result']['messages']=[m for m in result.get('messages',[]) if m.get('severity') in ('error','warning')]
            row['result_projection']='build and timestamped online observation, including verification/values/errors; full result in handoff inputs'
    selected = select_review_evidence(prepared,max_samples)
    columns=[]; indices={}; statuses=[]
    for row in selected['records']:
        if row.get('kind')=='tag_sample':
            values=[]
            for tag in row.pop('tags',[]):
                key=tag['id']
                if key not in indices: indices[key]=len(columns);columns.append(key)
                status=[tag.get('isForced'),tag.get('openCircuit'),tag.get('shortCircuit')]
                if compact_status:
                    if status not in statuses: statuses.append(status)
                    values.append([indices[key],tag.get('value'),statuses.index(status)])
                else:
                    values.append([indices[key],tag.get('value'),*status])
            row['tag_rows']=values
    selected['tag_columns']=columns
    if compact_status: selected['tag_status_patterns']=statuses
    metadata={t['key']:t for t in (io_contract or {}).get('tags',[])}
    measured={}
    for row in evidence.get('records',[]):
        if row.get('kind')!='tag_sample': continue
        for tag in row.get('tags',[]):
            key=tag['id']
            value=tag.get('value')
            item=measured.setdefault(key,{'key':key,'name':tag.get('name'), 'kind':tag.get('kind'),
                'object_id':metadata.get(key,{}).get('object_id'), 'first_raw':value,
                'first_record_id':row['id'],'observations':0,'observed_raw_values':[],
                'status_violation_samples':0,'unknown_status_samples':0})
            item['observations']+=1
            item['last_raw']=value;item['last_record_id']=row['id']
            if value not in item['observed_raw_values'] and len(item['observed_raw_values'])<8:
                item['observed_raw_values'].append(value)
            statuses=[tag.get(k) for k in ('isForced','openCircuit','shortCircuit')]
            item['status_violation_samples']+=int(any(v is True for v in statuses))
            item['unknown_status_samples']+=int(any(type(v) is not bool for v in statuses))
    selected['signal_index']=[{'column':i,**measured.get(key,{'key':key})} for i,key in enumerate(columns)]
    selected['model_view']={'tag_rows_columns':(['index_into_tag_columns','raw_value','index_into_tag_status_patterns'] if compact_status else
        ['index_into_tag_columns','raw_value','isForced','openCircuit','shortCircuit']),
        'tag_status_pattern_columns':['isForced','openCircuit','shortCircuit'],
        'unknown_status':'null means absent or unknown, never healthy; values and status are not normalized',
        'tag_fields':'actual GUIDs stored once in tag_columns; static metadata in io_contract',
        'signal_index':'explicit column/GUID/name/kind/object mapping with full-window raw first/last observations; observed_raw_values is capped at eight distinct values, not an exhaustive range',
        'aggregate_reference_lists':'preview only, with original count and hash; full lists in handoff evidence.json',
        'repeated_issues':'deduplicated with exact issue_occurrence_counts',
        'measurement_counts_bounds_verdicts_changed':False,'raw_evidence_changed':False}
    return selected
