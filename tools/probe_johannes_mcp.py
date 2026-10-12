"""Record the upstream server's actual protocol replies; no PLC deployment."""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def probe(config: dict, output: Path, project: str | None, operations: list):
    environment = dict(os.environ)
    environment.update(config.get("env", {}))
    params = StdioServerParameters(command=config["command"], args=config.get("args", []), env=environment)
    output.mkdir(parents=True, exist_ok=True)

    def save(name, value):
        result = value.model_dump(mode="json")
        (output / (name + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=config.get("timeout_seconds", 90))) as session:
            initialized = save("initialize", await session.initialize())
            inventory = save("tools", await session.list_tools())
            save("resource-templates", await session.list_resource_templates())
            save("resources", await session.list_resources())
            print(json.dumps({"server": initialized["serverInfo"], "tool_names": [t["name"] for t in inventory["tools"]]}, ensure_ascii=False), flush=True)
            if project:
                response = save("open-project", await session.call_tool("open_project", {"filePath": project}))
                print(json.dumps({"open_project": response}, ensure_ascii=False), flush=True)
            for index, operation in enumerate(operations):
                label = str(index).zfill(2) + "-" + operation["label"]
                print(json.dumps({"started": label}, ensure_ascii=False), flush=True)
                started = time.monotonic()
                if "resource" in operation:
                    value = await session.read_resource(operation["resource"])
                else:
                    value = await session.call_tool(operation["tool"], operation["arguments"])
                response = save(label, value)
                elapsed = time.monotonic() - started
                (output / (label + "-timing.json")).write_text(json.dumps({"elapsed_seconds": elapsed}), encoding="utf-8")
                print(json.dumps({"finished": label, "isError": response.get("isError"), "elapsed_seconds": round(elapsed, 2), "response_path": str(output / (label + ".json"))}, ensure_ascii=False), flush=True)
                if response.get("isError"):
                    break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--project")
    parser.add_argument("--operations", type=Path)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    operations = json.loads(args.operations.read_text(encoding="utf-8-sig")) if args.operations else []
    asyncio.run(probe(json.loads(args.server.read_text(encoding="utf-8-sig")), args.out, args.project, operations))


if __name__ == "__main__":
    main()
