"""Compile the bridge against a read-only managed DLL copy and prepare bootstrap.

Does not install files in Factory I/O, restart it, or invoke the generated cfg.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--managed-copy", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--ipc", required=True, type=Path)
    parser.add_argument("--compiler", type=Path,
                        default=Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"))
    args = parser.parse_args()
    managed, out, ipc = args.managed_copy.resolve(), args.out.resolve(), args.ipc.resolve()
    out.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name("AgentBridge.cs").resolve()
    dll = out / "FactoryIOAgentBridge.dll"
    if dll.exists():
        raise FileExistsError("Use a fresh build output directory")
    refs = ["mscorlib.dll", "System.dll", "System.Core.dll", "netstandard.dll",
            "Newtonsoft.Json.dll", "UnityEngine.dll", "UnityEngine.CoreModule.dll",
            "UnityEngine.SharedInternalsModule.dll", "Assembly-CSharp.dll"]
    for name in refs:
        if not (managed / name).is_file(): raise FileNotFoundError(managed / name)
    assembly_sha = hashlib.sha256((managed / "Assembly-CSharp.dll").read_bytes()).hexdigest()
    flags = ["/nologo", "/nostdlib+", "/target:library", f'/out:"{dll}"']
    flags += [f'/reference:"{managed / name}"' for name in refs]
    flags.append(f'"{source}"')
    rsp = out / "compile.rsp"
    rsp.write_text("\n".join(flags), encoding="utf-8-sig")
    result = subprocess.run([str(args.compiler), "/noconfig", "@" + str(rsp)],
                            capture_output=True, timeout=30)
    report = {"compiler": str(args.compiler), "exit_code": result.returncode,
              "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "factoryio_assembly_sha256": assembly_sha,
              "reference_sha256": {name: hashlib.sha256((managed / name).read_bytes()).hexdigest() for name in refs},
              "stdout": result.stdout.decode("gbk", errors="replace"),
              "stderr": result.stderr.decode("gbk", errors="replace"),
              "runtime_bootstrapped": False}
    if result.returncode == 0:
        report["bridge_sha256"] = hashlib.sha256(dll.read_bytes()).hexdigest()
        # A tuple is required for MethodInfo.Invoke arguments in this IronPython.
        # No import restoration or modification of application assemblies.
        statements = [
            '_agent_load_type = scene.GetType().GetType().Assembly.GetType("System.Reflection.Assembly")',
            '_agent_load_method = [m for m in _agent_load_type.GetMethods() if m.Name == "LoadFrom" and len(m.GetParameters()) == 1][0]',
            f'_agent_bridge_assembly = _agent_load_method.Invoke(None, ({str(dll)!r},))',
            '_agent_protocol = _agent_bridge_assembly.GetType("FactoryIOAgentBridge.Bootstrap").GetMethod("Protocol").Invoke(None, None)',
            f'_agent_bridge_assembly.GetType("FactoryIOAgentBridge.Bootstrap").GetMethod("Install").Invoke(None, ({str(ipc)!r}, {assembly_sha!r}))',
        ]
        (out / "bootstrap.cfg").write_text("\n".join(statements) + "\n", encoding="utf-8")
    (out / "compile-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if result.returncode != 0: raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
