"""Evidence-linked, redacted raw candidate collection; never auto-label model outputs as gold."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .contracts import file_hash, require


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Redactor:
    """Retain field structure but withhold known credential fields and explicit secret values."""
    sensitive = re.compile(r"^(?:authorization|credentials|api_?key|password|passwd|secret|access_token|client_secret)$|(?:_password|_api_key|_secret)$", re.I)

    def __init__(self, values=()):
        self.values = sorted({v for v in values if isinstance(v, str) and v}, key=len, reverse=True)
        self.count = 0

    def clean(self, value):
        if isinstance(value, dict):
            output = {}
            for key, child in value.items():
                if self.sensitive.search(str(key)) and child is not None:
                    self.count += 1
                    output[key] = "<redacted>"
                else:
                    output[key] = self.clean(child)
            return output
        if isinstance(value, (list, tuple)):
            return [self.clean(child) for child in value]
        if isinstance(value, str):
            # Provider content may wrap a credential object in JSON text or Markdown fences.
            # Only rewrite when redaction is needed; unchanged prompts/replies retain exact text.
            stripped = value.strip()
            inner = "\n".join(stripped.splitlines()[1:-1]) if stripped.startswith("```") else stripped
            if inner.startswith(("{", "[")):
                try:
                    parsed = json.loads(inner)
                except ValueError:
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    before = self.count
                    cleaned = self.clean(parsed)
                    if self.count != before:
                        value = value.replace(inner, json.dumps(cleaned, ensure_ascii=False), 1)
            for secret in self.values:
                hits = value.count(secret)
                if hits:
                    self.count += hits
                    value = value.replace(secret, "<redacted>")
            value, hits = re.subn(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*|\bsk-[A-Za-z0-9_-]+", "<redacted>", value)
            self.count += hits
            # JSON embedded in user/assistant strings needs the same credential protection as dicts.
            value, hits = re.subn(r'(?i)("(?:api_?key|password|passwd|secret|credentials|access_token|client_secret|authorization|[^"]+_(?:password|api_key|secret))"\s*:\s*)"(?:\\.|[^"\\])*"', r'\1"<redacted>"', value)
            self.count += hits
            value, hits = re.subn(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "<image-data-withheld>", value)
            self.count += hits
            return value
        return value


class TrainingRecorder:
    def __init__(self, directory: Path, *, scenario_id=None, lineage_group=None,
                 source_kind="live_model", secrets=(), run_id=None, event_callback=None):
        self.directory = directory.resolve()
        self.run_id = run_id or uuid.uuid4().hex
        self.scenario_id, self.lineage_group = scenario_id, lineage_group
        self.source_kind = source_kind
        self.secrets = tuple(secrets)
        self.last_candidate_id = None
        self.event_callback = event_callback
        self._lock = threading.RLock()
        self._request_context = ContextVar('factoryio_audit_'+self.run_id,default=None)

    @contextmanager
    def capture_context(self, context):
        """Record tool-owned metadata without sending it to the model or editing raw replies."""
        token=self._request_context.set(context)
        try: yield
        finally: self._request_context.reset(token)

    def _write(self, path: Path, value) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temp, path)

    def _snapshot(self, path: Path) -> dict:
        require(path.is_file(), "training input asset missing")
        digest = file_hash(path)
        relative = Path("assets") / (digest + path.suffix.lower())
        target = self.directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(path.read_bytes())
        require(file_hash(target) == digest, "training asset snapshot hash mismatch")
        return {"path": relative.as_posix(), "sha256": digest, "mime_type": mimetypes.guess_type(path.name)[0], "size_bytes": target.stat().st_size}

    def start(self, role: str, system: str, payload: dict, images=()) -> dict:
        with self._lock:
            redactor = Redactor(self.secrets)
            image_assets = [self._snapshot(Path(path)) for path in images or ()]
            # The wire user text uses insertion order and whitespace from ModelClient, not sorted JSON.
            exact_user_text = json_text(payload)
            original_fingerprint = text_hash(json_text({"system": system, "user_text": exact_user_text,
                "images": [{"sha256": asset["sha256"], "mime_type": asset["mime_type"]} for asset in image_assets]}))
            clean_payload = redactor.clean(deepcopy(payload))
            clean_system = redactor.clean(system)
            content = json_text(clean_payload)
            if image_assets:
                content = [{"type": "text", "text": content}] + [{"type": "image"} for _ in image_assets]
            recording_context=self._request_context.get()
            audit=recording_context if recording_context is not None else payload
            handoff = audit.get("handoff") or {}
            candidate_id = uuid.uuid4().hex
            record = {"schema_version": 1, "kind": "agent_candidate", "candidate_id": candidate_id,
                "run_id": self.run_id, "role": role, "phase": handoff.get("phase", payload.get("phase", "unspecified")),
                "scenario_id": self.scenario_id or (audit.get("task") or {}).get("task_id") or audit.get('task_id'),
                "lineage_group": self.lineage_group, "source_kind": self.source_kind,
                "started_at": now(), "status": "started", "handoff_id": handoff.get("id"),
                "versions": redactor.clean(handoff.get("versions", audit.get("versions", {}))),
                "policy": redactor.clean(audit.get("policy", {})), "prompt_sha256": original_fingerprint,
                "messages": [{"role": "system", "content": clean_system}, {"role": "user", "content": content}],
                "payload": clean_payload, "images": image_assets,
                "input_metrics": {"system_chars":len(system),"payload_chars":len(exact_user_text),
                    "image_count":len(image_assets),"image_bytes":sum(a['size_bytes'] for a in image_assets),
                    "token_estimate_used":False},
                "input_artifacts": redactor.clean(handoff.get("inputs", [])),
                "response_text": None, "response_sha256": None, "structured_output": None,
                "redaction_count": redactor.count,
                "quality": {"review_status": "pending", "sft_eligible": False, "dpo_eligible": False, "split": "unassigned"}}
            if recording_context is not None:
                record['recording_context']=redactor.clean(deepcopy(recording_context))
                from .contracts import canonical_hash
                record['recording_context_sha256']=canonical_hash(record['recording_context'])
                record['redaction_count']=redactor.count
            self._write(self.directory / "candidates" / (candidate_id + ".json"), record)
            self.last_candidate_id = candidate_id
            return record

    def finish(self, record: dict, *, output=None, exchange=None, error=None, elapsed=0) -> None:
        with self._lock:
            redactor = Redactor(self.secrets)
            exchange = exchange if isinstance(exchange, dict) else {}
            raw_content = exchange.get("raw_content")
            text = raw_content if isinstance(raw_content, str) else json_text(output) if output is not None else None
            record.update({"status": "call_error" if error else "response_received", "finished_at": now(),
                "latency_seconds": round(elapsed, 6), "response_text": redactor.clean(text),
                "response_sha256": text_hash(text) if isinstance(text, str) else None,
                "structured_output": redactor.clean(output),
                "output_origin": "provider_content" if isinstance(raw_content, str) else "serialized_client_result" if text else "no_completion",
                "model_metadata": redactor.clean({k: exchange[k] for k in ("endpoint", "provider", "request_options", "response_format", "requested_model", "returned_model", "response_id", "http_status", "usage", "finish_reason", "streaming_requested", "response_mode", "stream_stats") if k in exchange}),
                "error": {"type": type(error).__name__, "message": redactor.clean(str(error))} if error else None})
            record["redaction_count"] += redactor.count
            self._write(self.directory / "candidates" / (record["candidate_id"] + ".json"), record)
            self.event("candidate_recorded", {"candidate_id": record["candidate_id"], "status": record["status"]})

    def event(self, event: str, details: dict) -> None:
        with self._lock:
            redactor = Redactor(self.secrets)
            item = redactor.clean({"schema_version": 1, "run_id": self.run_id, "event": event,
                "candidate_id": self.last_candidate_id, "timestamp": now(), "details": details})
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json_text(item) + "\n")
            if self.event_callback: self.event_callback(item)


class RecordingClient:
    def __init__(self, client, recorder: TrainingRecorder):
        self.client, self.recorder = client, recorder

    def _exchange(self):
        # Mock dynamically invents attributes: consult the declared type, not hasattr(instance).
        method = getattr(type(self.client), "exchange_metadata", None)
        return method(self.client) if callable(method) else {}

    def complete(self, role: str, system: str, payload: dict, images=None):
        record = self.recorder.start(role, system, payload, images)
        frozen_images = [self.recorder.directory / asset["path"] for asset in record["images"]]
        started = time.monotonic()
        try:
            value = self.client.complete(role, system, payload, frozen_images) if frozen_images else self.client.complete(role, system, payload)
            require(all(file_hash(path) == asset["sha256"] for path, asset in zip(frozen_images, record["images"])),
                    "training input snapshot changed during model call")
        except Exception as error:
            self.recorder.finish(record, exchange=self._exchange(), error=error, elapsed=time.monotonic() - started)
            raise
        self.recorder.finish(record, output=value, exchange=self._exchange(), elapsed=time.monotonic() - started)
        return value


def configured_secret_values(config: dict) -> list[str]:
    values = []
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).endswith("_env") and isinstance(child, str) and os.getenv(child):
                    values.append(os.environ[child])
                if Redactor.sensitive.search(str(key)) and isinstance(child, str):
                    values.append(child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(config)
    for key in ("OPENAI_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
        if os.getenv(key):
            values.append(os.environ[key])
    return values
