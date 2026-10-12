"""File IPC client for the experimental in-process Factory I/O bridge.

Requires one-time bootstrap in a running Factory I/O process; never installs it.
Does not synthesize live success when the bridge is missing.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid


def call(root: Path, action: str, *, timeout: float = 20, **params) -> dict:
    root = root.resolve()
    requests, responses = root / "requests", root / "responses"
    if not requests.is_dir() or not responses.is_dir():
        raise RuntimeError("Factory I/O bridge has not initialized this IPC directory")
    if not 0 < timeout <= 60:
        raise ValueError("timeout must be 0..60 seconds")
    request_id = uuid.uuid4().hex
    request = {"schema_version": 1, "request_id": request_id, "action": action,
               "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=timeout)).isoformat(), **params}
    temporary = requests / f"{request_id}.tmp"
    temporary.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    temporary.rename(requests / f"{request_id}.json")
    response_path = responses / f"{request_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if response_path.is_file():
            response = json.loads(response_path.read_text(encoding="utf-8-sig"))
            if (response.get("schema_version") != 1 or response.get("request_id") != request_id or
                    response.get("action") != action or response.get("protocol") != "factoryio-agent-file-bridge/1"):
                raise RuntimeError("Bridge response does not match request")
            if response.get("ok") is not True:
                raise RuntimeError(response.get("error") or "Bridge operation failed")
            if action == "capture":
                image = Path(response["data"]["image_path"]).resolve()
                if not image.is_relative_to(root / "images"):
                    raise RuntimeError("Bridge image escaped IPC image directory")
                if hashlib.sha256(image.read_bytes()).hexdigest() != response["data"]["image_sha256"]:
                    raise RuntimeError("Bridge image hash mismatch")
            return response
        time.sleep(0.05)
    raise TimeoutError("No matching Factory I/O bridge response; live operation was not verified")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ipc", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    sub.add_parser("prefabs")
    sub.add_parser("connect_modbus")
    sub.add_parser("save_snapshot")
    capture = sub.add_parser("capture")
    capture.add_argument("--size", type=int, default=1, choices=range(1, 5))
    camera = sub.add_parser("camera")
    camera.add_argument("--index", type=int, required=True)
    load = sub.add_parser("load")
    load.add_argument("scene", type=Path)
    load.add_argument("--save-before-load", action="store_true")
    args = parser.parse_args()
    params = {}
    if args.action == "capture": params["size"] = args.size
    elif args.action == "camera": params["index"] = args.index
    elif args.action == "load":
        params = {"scene_path": str(args.scene.resolve()),
                  "scene_sha256": hashlib.sha256(args.scene.read_bytes()).hexdigest(),
                  "save_before_load": args.save_before_load}
    print(json.dumps(call(args.ipc, args.action, **params), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
