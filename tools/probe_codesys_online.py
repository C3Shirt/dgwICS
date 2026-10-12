"""Read-only CODESYS probe. Credentials are environment references, never CLI values."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factoryio_agents.plc import run_codesys


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=root / "config.local.json")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--credential-env", action="store_true")
    args = parser.parse_args()
    output = args.out or root / "artifacts" / ("codesys-auth-probe-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f"))
    if output.exists():
        parser.error("Probe output already exists; choose a fresh path")
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))["codesys"]
    config["online_probe"] = {"connect": True, "host": "127.0.0.1", "port": 11740, "device_name": "Device"}
    if args.credential_env:
        config["online_credentials"] = {"username_env": "FACTORYIO_CODESYS_USERNAME", "password_env": "FACTORYIO_CODESYS_PASSWORD"}
    result = run_codesys(config, output, deploy=False)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "acceptance-report.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    probe = result.get("online_probe") or {}
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"compiled": result["compiled"], "connected": probe.get("connected", False),
                      "application_state": probe.get("application_state"), "online_error": probe.get("error"),
                      "diagnostics": result["diagnostics"], "report_path": str(output / "acceptance-report.json")},
                     ensure_ascii=False, indent=2))
    return 0 if result["compiled"] and probe.get("connected") else 2


if __name__ == "__main__":
    raise SystemExit(main())
