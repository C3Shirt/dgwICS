"""Factory I/O HTTP adapter, command hooks and timestamped evidence capture."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .contracts import ContractError, file_hash, require, write_json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FactoryIOClient:
    def __init__(self, config: dict):
        self.url = config.get("web_api_url", "http://127.0.0.1:7410").rstrip("/")
        self.timeout = config.get("timeout_seconds", 5)
        # Local simulation must not be sent to a configured system HTTP proxy.
        host = urllib.parse.urlparse(self.url).hostname
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if host in (
            "localhost", "127.0.0.1", "::1") else urllib.request.build_opener()

    def request(self, endpoint: str, body=None):
        headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(self.url + endpoint,
            None if body is None else json.dumps(body, allow_nan=False).encode("utf-8"), headers,
            method="GET" if body is None else "PUT")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                value = json.load(response)
        except urllib.error.HTTPError as exc:
            raise ContractError(f"Factory I/O HTTP {exc.code} at {endpoint}") from None
        except (urllib.error.URLError, TimeoutError):
            raise ContractError("Factory I/O Web API unavailable; check app.web_server and port 7410") from None
        require(isinstance(value, list), "Factory I/O returned unexpected response shape")
        errors = [v for v in value if isinstance(v, dict) and v.get("code", 200) >= 400]
        require(not errors, f"Factory I/O returned {len(errors)} tag errors")
        return value

    def tags(self) -> list[dict]:
        return self.request("/api/tags")

    def wait_running(self, expected: bool, timeout: float = 5) -> dict:
        """Observe the system input; a successful Run write only acknowledges the request."""
        require(type(expected) is bool and 0 < timeout <= 30, "invalid Running wait")
        deadline = time.monotonic() + timeout
        while True:
            tags = self.tags()
            rows = [t for t in tags if t["name"] == "FACTORY I/O (Running)" and
                    t["kind"].lower() == "input"]
            require(len(rows) == 1, "cannot uniquely resolve system Running input")
            row = rows[0]
            require(row.get("isForced") is False, "system Running input is forced or force status is unknown")
            if row.get("value") is expected:
                return {"timestamp": utc_now(), "tag_key": row["id"], "running": expected,
                        "isForced": False, "source": "factoryio_web_api"}
            if time.monotonic() >= deadline:
                error = ContractError("Factory I/O Run request was not observed as the expected Running state")
                error.diagnostics = {"expected_running": expected, "observed_at": utc_now(),
                    "system_tags": [t for t in tags if t['name'].startswith('FACTORY I/O')]}
                raise error
            time.sleep(0.1)

    def control(self, action: str, value: bool | int) -> None:
        names = {"run": "FACTORY I/O (Run)", "pause": "FACTORY I/O (Pause)",
                 "reset": "FACTORY I/O (Reset)", "camera": "FACTORY I/O (Camera Position)"}
        require(action in names, "unknown system action")
        rows = [t for t in self.tags() if t["name"] == names[action] and t["kind"].lower() == "output"]
        require(len(rows) == 1, f"cannot uniquely resolve system {action} output")
        self.request("/api/tag/values", [{"id": rows[0]["id"], "value": value}])


def reconcile_tags(io_contract: dict, actual: list[dict]) -> dict:
    by_id = {t["id"]: t for t in actual}
    issues = []
    if len(by_id) != len(actual): issues.append("duplicate runtime tag IDs")
    types = {"bool": "bit", "float": "float", "int": "int"}
    for expected in io_contract["tags"]:
        found = by_id.get(expected["key"])
        if found is None:
            issues.append(f"missing runtime tag: {expected['key']} ({expected['name']})")
            continue
        # XML stores system aliases (RunInputKey, etc.), while the API uses localized display names.
        if expected.get("object_id") is not None and found["name"] != expected["name"]:
            issues.append(f"name mismatch: {expected['key']}")
        if found["kind"].lower() != expected["kind"]: issues.append(f"direction mismatch: {expected['key']}")
        if found["type"].lower() != types[expected["data_type"]]: issues.append(f"type mismatch: {expected['key']}")
        if found.get("isForced") and not expected.get("use_forced_value", False):
            issues.append(f"unexpected forced tag: {expected['key']}")
    return {"passed": not issues, "issues": issues, "actual_count": len(actual),
            "expected_count": len(io_contract["tags"])}


def run_hook(command: list[str] | None, substitutions: dict, timeout: float = 30) -> dict:
    require(isinstance(command, list) and bool(command) and all(isinstance(v, str) for v in command),
            "command hook is not configured; use an argv array, never a shell string")
    argv = [arg.format_map(substitutions) for arg in command]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"tool command failed ({type(exc).__name__})") from None
    require(result.returncode == 0, f"tool command exit {result.returncode}: {result.stderr[-1000:]}")
    return {"argv": argv, "returncode": result.returncode, "stdout": result.stdout[-4000:]}


class ProbeTriggerTimeout(ContractError):
    """An observed test precondition was not reached; not a native/API operation failure."""


def collect_evidence(config: dict, output: Path, versions: dict | None = None,
                     io_contract: dict | None = None, seconds: float | None = None, manifest: dict | None = None,
                     task: dict | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    client = FactoryIOClient(config)
    duration = config.get("runtime_seconds", 30) if seconds is None else seconds
    interval = config.get("sample_interval_seconds", 0.2)
    require(isinstance(duration, (int, float)) and 0 <= duration <= 86400, "invalid capture duration")
    require(isinstance(interval, (int, float)) and 0.01 <= interval <= 60, "invalid capture interval")
    evidence = {"schema_version": 1, "mode": "live", "versions": versions,
                "started_at": utc_now(), "records": [], "reconciliation": None}
    start = time.monotonic()
    index = 0
    while True:
        tags = client.tags()
        timestamp = utc_now()
        reconciliation = reconcile_tags(io_contract, tags) if io_contract else None
        if index == 0: evidence["reconciliation"] = reconciliation
        if reconciliation is not None and not reconciliation["passed"]:
            evidence["reconciliation"] = reconciliation
            evidence["records"].append({"id": f"tags-{index}", "kind": "tag_sample", "timestamp": timestamp,
                                        "elapsed_seconds": time.monotonic() - start, "tags": tags})
            break
        evidence["records"].append({"id": f"tags-{index}", "kind": "tag_sample", "timestamp": timestamp,
                                    "elapsed_seconds": time.monotonic() - start, "tags": tags})
        if config.get("capture_command") and (index == 0 or time.monotonic() - start >= duration):
            views = config.get('capture_views', [None])
            require(isinstance(views, list) and 1 <= len(views) <= 3 and len(set(views)) == len(views) and
                    all(v in (None, 'iso_a', 'iso_b', 'top') for v in views), 'invalid capture_views')
            for view in views:
                suffix = '-' + view if view else ''
                image_path = output / f"frame-{index:04d}{suffix}.png"
                require(not image_path.exists(), "capture output already exists; use a fresh evidence directory")
                run_hook(config["capture_command"], {"output": str(image_path.resolve()), 'view':view or 'iso_a'})
                require(image_path.is_file(), "capture tool did not create requested image")
                evidence["records"].append({"id": f"image-{index}{suffix}", "kind": "image", "timestamp": utc_now(),
                    "path": str(image_path.resolve()), "sha256": file_hash(image_path), "source": "capture_command",
                    'camera_view':view})
        if index==0 and duration>0 and config.get('operator_startup') is True:
            operator_startup(config,manifest,output,evidence,client,start,io_contract)
        if index == 0 and duration > 0 and config.get('execute_model_probes') is True and task:
            try:
                execute_model_probes(config, task, manifest, output, evidence, client, start, io_contract, 'startup')
            except ProbeTriggerTimeout:
                pass  # Continue observing the failed process; acceptance and Agent3 diagnose the evidence.
        index += 1
        remaining = duration - (time.monotonic() - start)
        if remaining <= 0: break
        time.sleep(min(interval, remaining))
    if duration > 0 and config.get('execute_model_probes') is True and task:
        try:
            execute_model_probes(config, task, manifest, output, evidence, client, start, io_contract, 'after_capture')
        except ProbeTriggerTimeout:
            pass
        finally:
            # Preserve actual partial probe evidence even when a native action fails.
            write_json(output / 'evidence.json', evidence)
    evidence["finished_at"] = utc_now()
    write_json(output / "evidence.json", evidence)
    return evidence


def operator_startup(config,manifest,output,evidence,client,start,io_contract):
    """Reset then Start via native momentary buttons, retaining receipts and actual tag samples."""
    require(isinstance(manifest,dict),'operator startup needs this run scene manifest')
    for prefab in ('ResetPushButton','StartPushButton'):
        objects=[o for o in manifest['objects'] if o['prefab']==prefab]
        require(len(objects)==1,'operator startup needs exactly one '+prefab)
        obj=objects[0];name='reset' if prefab=='ResetPushButton' else 'start'
        def action(pressed):
            label=name+('-press' if pressed else '-release');path=output/('operator-'+label+'.json')
            run_hook(config.get('operator_command'),{'object_key':obj['key'],'pressed':'true' if pressed else 'false',
                'output':str(path.resolve())},10)
            receipt=json.loads(path.read_text(encoding='utf-8-sig'))
            observed=receipt.get('data',{}).get('operator_button',{})
            require(receipt.get('ok') is True and observed.get('object_key')==obj['key'] and
                    observed.get('pressed') is pressed and observed.get('native_input') is True,
                    'operator tool did not confirm this native button action')
            evidence['records'].append({'id':'operator-'+label,'kind':'operator_action','timestamp':utc_now(),
                'elapsed_seconds':time.monotonic()-start,'object_id':obj['id'],'pressed':pressed,
                'receipt_path':str(path.resolve()),'receipt_sha256':file_hash(path),'source':'native_operator_button'})
            tags=client.tags();require(reconcile_tags(io_contract,tags)['passed'],'tags changed during operator action')
            evidence['records'].append({'id':'operator-tags-'+label,'kind':'tag_sample','timestamp':utc_now(),
                'elapsed_seconds':time.monotonic()-start,'tags':tags})
        try:
            action(True)
            time.sleep(.35)
        finally:action(False)


def execute_model_probes(config, task, manifest, output, evidence, client, start, io_contract, stage):
    """Execute a validated model test plan using native button actions and measured tags."""
    from .signal_validation import validate_signal_contract, resolve_signals, state_from_tags, matches
    from .signal_validation import BUTTON_PREFABS
    require(isinstance(manifest, dict), 'model probe needs the current scene manifest')
    objects = {o['id']:o for o in manifest['objects']}
    for rule_index, rule in enumerate(task.get('acceptance', [])):
        if rule.get('kind') != 'signal_contract': continue
        validate_signal_contract(rule, task); resolved = resolve_signals(rule, io_contract)
        for probe in rule.get('probes', []):
            if probe['stage'] != stage: continue
            count = 0; held = {}
            def sample(phase):
                nonlocal count
                tags = client.tags()
                require(reconcile_tags(io_contract,tags)['passed'], 'tags changed during model probe')
                state = state_from_tags(tags,resolved)
                evidence['records'].append({'id':f'probe-tags-{rule_index}-{probe["id"]}-{count}',
                    'kind':'tag_sample','timestamp':utc_now(),'elapsed_seconds':time.monotonic()-start,
                    'probe_phase':phase,'probe_stage':stage,'tags':tags})
                count += 1; return state
            def action(spec, cleanup=False):
                obj = objects[spec['object_id']]
                require(obj['prefab'] in BUTTON_PREFABS, 'unsupported native probe object')
                phase = probe['id'] + '.' + spec['label']
                path = output / ('operator-' + phase + '.json')
                require(not path.exists(), 'model probe receipt already exists')
                # Track a requested press before invoking the hook so failures still release it.
                if spec['pressed']: held[obj['id']] = obj
                run_hook(config.get('operator_command'), {'object_key':obj['key'],
                    'pressed':'true' if spec['pressed'] else 'false','output':str(path.resolve())},10)
                receipt = json.loads(path.read_text(encoding='utf-8-sig'))
                observed = receipt.get('data',{}).get('operator_button',{})
                require(receipt.get('ok') is True and observed.get('object_key') == obj['key'] and
                        observed.get('pressed') is spec['pressed'] and observed.get('native_input') is True,
                        'model probe native action not confirmed')
                if not spec['pressed']: held.pop(obj['id'],None)
                evidence['records'].append({'id':'operator-'+phase,'kind':'operator_action','timestamp':utc_now(),
                    'elapsed_seconds':time.monotonic()-start,'probe_phase':phase,'probe_stage':stage,
                    'object_id':obj['id'],'object_key':obj['key'],'pressed':spec['pressed'],'cleanup':cleanup,
                    'receipt_path':str(path.resolve()),'receipt_sha256':file_hash(path),'source':'native_operator_button'})
                end = time.monotonic() + spec['hold_seconds']
                while True:
                    sample(phase)
                    if time.monotonic() >= end: break
                    time.sleep(.05)
            try:
                if probe.get('wait_for') is not None:
                    deadline = time.monotonic() + probe['timeout_seconds']
                    while not matches(sample(probe['id']+'.wait'),probe['wait_for']):
                        if time.monotonic() >= deadline:
                            waiting = [r for r in evidence['records'] if r.get('probe_phase') == probe['id']+'.wait']
                            evidence['records'].append({'id':f'probe-failure-{rule_index}-{probe["id"]}',
                                'kind':'probe_failure','timestamp':utc_now(),'probe_id':probe['id'],'probe_stage':stage,
                                'reason':'trigger_unobserved','required_conditions':probe['wait_for'],
                                'timeout_seconds':probe['timeout_seconds'],
                                'source_record_ids':[r['id'] for r in (waiting[:1]+waiting[-1:])]})
                            raise ProbeTriggerTimeout('model probe trigger timed out: ' + probe['id'])
                        time.sleep(.05)
                for spec in probe['actions']: action(spec)
            finally:
                for oid in list(held):
                    action({'object_id':oid,'pressed':False,'label':'cleanup-'+oid,'hold_seconds':.4},cleanup=True)
                write_json(output / 'evidence.json',evidence)


def attach_images(evidence: dict, paths: list[Path], versions: dict) -> dict:
    require(evidence.get("versions") == versions, "image evidence versions do not match run")
    for path in paths:
        require(path.is_file(), f"image not found: {path}")
        evidence["records"].append({"id": f"image-{len(evidence['records'])}", "kind": "image",
            "timestamp": utc_now(), "path": str(path.resolve()), "sha256": file_hash(path),
            "source": "operator_supplied", "captured_at_verified": False})
    return evidence
