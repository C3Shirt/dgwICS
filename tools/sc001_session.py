"""Prepare a separate Factory I/O session while keeping the existing window intact."""
from pathlib import Path
import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools" / "factoryio_bridge")]
from client import call
from factoryio_agents.contracts import file_hash, require, write_json


def launch(output: Path, executable: Path | None = None):
    output = output.resolve()
    bootstrap = output / "bridge-build" / "bootstrap.cfg"
    require(bootstrap.is_file(), "compile the scene bridge first")
    report_path = output / "session-launch.json"
    require(not report_path.exists(), "session launch already recorded")
    old_ipc = ROOT / "artifacts" / "software-analysis-001" / "ipc-03"
    original = call(old_ipc, "status", timeout=10)
    write_json(output / "previous-scene-status.json", original)
    old_scene = Path(original["data"]["scene_path"])
    shutil.copyfile(old_scene, output / "previous-scene-file.factoryio")
    screenshot = call(old_ipc, "capture", size=1)
    shutil.copyfile(screenshot["data"]["image_path"], output / "previous-scene.png")
    write_json(output / "previous-scene-capture.json", screenshot)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:7410/api/tags", timeout=5) as response:
        write_json(output / "previous-tags.json", json.load(response))
    cfg = Path.home() / "Documents" / "Factory IO" / "auto.cfg"
    previous = cfg.read_bytes()
    (output / "previous-auto.cfg").write_bytes(previous)
    statements = ["app.web_server = False", "app.web_server_url = r'http://127.0.0.1:7411'",
        "app.web_server = True", "app.run_in_background = True", "ui.show_welcome_window = False",
        "audio.enabled = False", "drivers.modbustcp_client.auto_connect = False",
        f"execfile({str(bootstrap)!r})", f"scene.load_from_path({str(old_scene)!r})"]
    cfg.write_text("\n".join(statements) + "\n", encoding="utf-8")
    executable = (executable or Path(r"E:\Program Files (x86)\factory\Factory IO.exe")).resolve()
    local_cfg = executable.parent / 'auto.cfg'
    local_previous = local_cfg.read_bytes() if local_cfg.is_file() else None
    if local_previous is not None:
        (output / 'previous-executable-auto.cfg').write_bytes(local_previous)
        # Prevent historical commands in a copied runtime from executing before Documents auto.cfg.
        local_cfg.write_text('# Session configuration is in Documents/Factory IO/auto.cfg\n', encoding='utf-8')
    # Factory I/O is the interactive scene viewer requested by the user. Keep the previous process.
    process = subprocess.Popen([str(executable), '-logFile', str(output / 'factoryio-player.log')], cwd=str(executable.parent), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    report = {"schema_version": 1, "new_process_id": process.pid, "previous_process_kept": True,
              "bootstrap_path": str(bootstrap), "ipc": str(output / "ipc"), "api_url": "http://127.0.0.1:7411",
              "auto_cfg": str(cfg), "auto_cfg_backup": str(output / "previous-auto.cfg"),
              "auto_cfg_sha256": file_hash(cfg), "executable": str(executable), "status": "launch_requested"}
    write_json(report_path, report)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            response = call(output / "ipc", "status", timeout=1)
            if response["data"].get("scene_path"):
                write_json(output / "new-session-status.json", response)
                report["status"] = "bridge_responding"; write_json(report_path, report)
                print(json.dumps({k:v for k,v in report.items() if k != "auto_cfg_sha256"}, ensure_ascii=False))
                return
        except (OSError, RuntimeError, TimeoutError, ValueError):
            time.sleep(0.3)
    cfg.write_bytes(previous)
    if local_previous is not None:
        local_cfg.write_bytes(local_previous)
    report["status"] = "initialization_unconfirmed"; report["auto_cfg_restored"] = True
    report['process_returncode'] = process.poll()
    write_json(report_path, report)
    raise RuntimeError("New Factory I/O bridge not confirmed; previous process and cfg preserved")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    args = parser.parse_args()
    launch(args.out, args.executable)
