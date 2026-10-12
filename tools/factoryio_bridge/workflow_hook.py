"""Adapt the verified in-process bridge to workflow load/capture argv hooks."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from client import call
from verify_live import compare_objects


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def load(ipc: Path, request_path: Path, confirmation: Path, save_before_load: bool = False) -> dict:
    if confirmation.exists():
        raise FileExistsError("load receipt already exists; use a fresh request")
    request_sha = digest(request_path)
    request = json.loads(request_path.read_text(encoding="utf-8-sig"))
    request_id = request.get("request_id")
    if (request.get("schema_version") != 1 or not isinstance(request_id, str) or
            uuid.UUID(hex=request_id).hex != request_id or not isinstance(request.get("versions"), dict)):
        raise ValueError("invalid workflow load request")
    scene = Path(request["scene_path"])
    expected_sha = request["scene_sha256"]
    if not scene.is_absolute() or digest(scene) != expected_sha:
        raise ValueError("scene path or hash differs from the load request")
    manifest = json.loads((request_path.parent / "manifest.json").read_text(encoding="utf-8-sig"))
    if manifest["scene_sha256"] != expected_sha or Path(manifest["scene_path"]).resolve() != scene.resolve():
        raise ValueError("manifest differs from the load request")
    proxies = {p.attrib["Key"]: p for p in ET.fromstring(scene.read_bytes()).findall("Object/Proxy")}
    for obj in manifest["objects"]:
        obj["fine_position"] = float(proxies[obj["key"]].findtext("FinePosition", "0"))
        obj["fine_rotation"] = float(proxies[obj["key"]].findtext("FineRotation", "0"))
    if save_before_load:
        response = call(ipc, "load", scene_path=str(scene.resolve()), scene_sha256=expected_sha, save_before_load=True)
    else:
        response = call(ipc, "load", scene_path=str(scene.resolve()), scene_sha256=expected_sha)
    data = response["data"]
    if (data["scene_loading"] or Path(data["scene_path"]).resolve() != scene.resolve() or
            data["loaded_scene_sha256"] != expected_sha or digest(scene) != expected_sha or
            digest(request_path) != request_sha):
        raise RuntimeError("live scene receipt or request version differs")
    reconciliation = compare_objects(manifest["objects"], data["objects"])
    if not reconciliation["passed"]:
        raise RuntimeError("live objects differ: " + "; ".join(reconciliation["issues"]))
    response_path = confirmation.with_name(confirmation.stem + ".bridge-response.json")
    write_new(response_path, response)
    receipt = {**request, "scene_path": str(scene.resolve()), "software_load_confirmed": True,
               "confirmation_method": data["completion_method"],
               "confirmed_at": response["completed_at"], "bridge_request_id": response["request_id"],
               "bridge_response_path": str(response_path.resolve()),
               "bridge_response_sha256": digest(response_path),
               "assembly_sha256": data["assembly_sha256"], "app_version": data["app_version"],
               "object_reconciliation": reconciliation}
    if all(isinstance(o.get('bounds_world'), dict) for o in data['objects']):
        receipt['native_geometry'] = {'coordinate_units':'native_world', 'objects':data['objects'],
                                      'coordinate_system':data.get('coordinate_system'),
                                      'world_bounds':data.get('world_bounds'),'bridge':data.get('bridge')}
    if data.get('acknowledged_load_failure') is not None:
        receipt['acknowledged_load_failure'] = data['acknowledged_load_failure']
    snapshot = data.get("saved_snapshot")
    if snapshot is not None:
        snapshot_path = Path(snapshot["path"]).resolve()
        if not snapshot_path.is_relative_to(ipc.resolve() / "snapshots") or digest(snapshot_path) != snapshot["sha256"] or snapshot.get("native_save") is not True:
            raise RuntimeError("native saved snapshot receipt invalid")
        receipt["saved_snapshot"] = snapshot
    write_new(confirmation, receipt)
    return {"ok": True, "confirmation": str(confirmation.resolve()), "request_id": request_id}


def capture(ipc: Path, output: Path, view: str | None = None) -> dict:
    metadata_path = output.with_name(output.stem + ".bridge.json")
    if output.exists() or metadata_path.exists():
        raise FileExistsError("capture output already exists; use a fresh image path")
    camera_receipt = call(ipc, 'camera_fit', view=view) if view is not None else None
    response = call(ipc, "capture", size=1)
    data = response["data"]
    image = Path(data["image_path"])
    content = image.read_bytes()
    if hashlib.sha256(content).hexdigest() != data["image_sha256"]:
        raise RuntimeError("bridge image changed before copy")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        stream.write(content)
    if digest(output) != data["image_sha256"]:
        raise RuntimeError("copied image hash differs")
    write_new(metadata_path, {"schema_version": 1, "source": "factoryio-in-process-bridge",
              "image_path": str(output.resolve()), "image_sha256": data["image_sha256"],
              "copied_at": datetime.now(timezone.utc).isoformat(), "bridge_response": response,
              'camera_receipt':camera_receipt})
    return {"ok": True, "image_path": str(output.resolve()), "image_sha256": data["image_sha256"]}


def operator(ipc: Path, object_key: str, pressed: bool, output: Path) -> dict:
    if output.exists(): raise FileExistsError('operator receipt exists')
    if uuid.UUID(object_key).hex != object_key.replace('-',''): raise ValueError('invalid operator object key')
    response=call(ipc,'operator_button',object_key=object_key,pressed=pressed)
    observed=response['data']['operator_button']
    if observed['object_key']!=object_key or observed['pressed'] is not pressed or observed['native_input'] is not True:
        raise RuntimeError('native operator action was not observed')
    write_new(output,response)
    return {'ok':True,'output':str(output.resolve())}


def connect(ipc: Path, request_path: Path, confirmation: Path) -> dict:
    """Connect the local driver and record its observed endpoint, without an IDE online probe."""
    if confirmation.exists():
        raise FileExistsError("connection receipt already exists; use a fresh request")
    request_sha = digest(request_path)
    request = json.loads(request_path.read_text(encoding="utf-8-sig"))
    if (request.get("schema_version") != 1 or uuid.UUID(hex=request["request_id"]).hex != request["request_id"]
        or request.get("host") not in ("localhost", "127.0.0.1") or type(request.get("port")) is not int
        or not 1 <= request["port"] <= 65535 or type(request.get("unit_id")) is not int
        or not 0 <= request["unit_id"] <= 255 or not isinstance(request.get("versions"), dict)):
        raise ValueError("invalid local connection request")
    scene = Path(request["scene_path"])
    if not scene.is_absolute() or digest(scene) != request["scene_sha256"]:
        raise ValueError("connection scene hash differs from request")
    response = call(ipc, "connect_modbus", host=request["host"], port=request["port"], unit_id=request["unit_id"])
    if response['data'].get('ui',{}).get('window_opened') is True:
        response['acknowledgement'] = call(ipc,'acknowledge_modbus_notice')
    stability = []
    for _ in range(10):
        time.sleep(.2)
        check = call(ipc,'status')
        if (check['data'].get('modbus',{}).get('connected') is not True or
            any(check['data']['modbus'].get(k) != request[k] for k in ('host','port','unit_id')) or
            check['data'].get('ui',{}).get('window_opened') is True):
            raise RuntimeError('Modbus I/O connection did not remain stable before runtime')
        stability.append({'request_id':check['request_id'],'completed_at':check['completed_at'],
                          'modbus':check['data']['modbus']})
    response['hook_connection_stability'] = stability
    data = response["data"]
    modbus = data.get("modbus", {})
    if (data.get("driver") != "ModbusTCPClient" or modbus.get("connected") is not True
        or any(modbus.get(k) != request[k] for k in ("host", "port", "unit_id"))
        or data.get("scene_loading") is not False or Path(data.get("scene_path", "")).resolve() != scene.resolve()
        or digest(scene) != request["scene_sha256"] or digest(request_path) != request_sha):
        raise RuntimeError("driver connection or current scene differs from request")
    response_path = confirmation.with_name(confirmation.stem + ".bridge-response.json")
    write_new(response_path, response)
    write_new(confirmation, {**request, "connection_confirmed": True, "driver": data["driver"],
        "modbus": modbus, "confirmed_at": response["completed_at"], "app_version": data["app_version"],
        "bridge_response_path": str(response_path.resolve()), "bridge_response_sha256": digest(response_path)})
    return {"ok": True, "confirmation": str(confirmation.resolve()), "request_id": request["request_id"]}


def prepare_deployment(ipc, output):
    response = call(ipc,'disconnect_modbus')
    require_disconnected = response['data']['modbus']
    if require_disconnected.get('socket_connected') is not False or require_disconnected.get('auto_connect') is not False:
        raise RuntimeError('native driver did not disconnect before PLC deployment')
    ui = response['data'].get('ui',{})
    if ui.get('window_opened') is True:
        # The native allowlist validates localized title, content and callback; no general OK click.
        response['acknowledgement'] = call(ipc,'acknowledge_modbus_notice')
    write_new(output,response)
    return {'ok':True,'output':str(output.resolve())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ipc", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    load_parser = sub.add_parser("load")
    load_parser.add_argument("--request", type=Path, required=True)
    load_parser.add_argument("--confirmation", type=Path, required=True)
    load_parser.add_argument("--save-before-load", action="store_true")
    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.add_argument('--view', choices=('iso_a', 'iso_b', 'top'))
    connect_parser = sub.add_parser("connect")
    connect_parser.add_argument("--request", type=Path, required=True)
    connect_parser.add_argument("--confirmation", type=Path, required=True)
    geometry_parser = sub.add_parser('geometry')
    geometry_parser.add_argument('--output', type=Path, required=True)
    operator_parser=sub.add_parser('operator')
    operator_parser.add_argument('--object-key',required=True)
    operator_parser.add_argument('--pressed',choices=('true','false'),required=True)
    operator_parser.add_argument('--output',type=Path,required=True)
    deployment_parser = sub.add_parser('prepare_deployment')
    deployment_parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.action == 'prepare_deployment':
            print(json.dumps(prepare_deployment(args.ipc,args.output))); return
        if args.action=='operator':
            print(json.dumps(operator(args.ipc,args.object_key,args.pressed=='true',args.output))); return
        if args.action == 'geometry':
            response = call(args.ipc, 'prefab_geometry')
            write_new(args.output,response)
            print(json.dumps({'ok':True,'output':str(args.output.resolve())})); return
        result = (load(args.ipc, args.request, args.confirmation, args.save_before_load) if args.action == "load" else
                  connect(args.ipc, args.request, args.confirmation) if args.action == "connect" else
                  capture(args.ipc, args.output, args.view))
    except (OSError, ValueError, KeyError, RuntimeError, TimeoutError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
