"""Optional SDK-based MCP client for probing or invoking configured tool servers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os

from .contracts import ContractError, require


@asynccontextmanager
async def connect(server: dict):
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError:
        raise ContractError("MCP optional dependency missing: install this project with .[mcp]") from None
    timeout = timedelta(seconds=server.get("timeout_seconds", 120))
    if server.get("url"):
        async with streamablehttp_client(server["url"]) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                await session.initialize()
                yield session
    else:
        require(bool(server.get("command")), "MCP command or URL is required")
        environment = dict(os.environ)
        environment.update(server.get("env", {}))
        parameters = StdioServerParameters(command=server["command"], args=server.get("args", []), env=environment)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                await session.initialize()
                yield session


async def inventory(server: dict) -> dict:
    async with connect(server) as session:
        result = await session.list_tools()
        tools = list(result.tools)
        cursor = result.nextCursor
        while cursor:
            result = await session.list_tools(cursor=cursor)
            tools.extend(result.tools)
            cursor = result.nextCursor
        return {"tool_count": len(tools), "tools": [t.model_dump(mode="json") for t in tools],
                "verified": "MCP initialize and tools/list only; no PLC operations executed"}


def decode_result(result) -> dict:
    require(not result.isError, "MCP server returned tool error")
    if result.structuredContent is not None:
        value = result.structuredContent
    else:
        parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        require(len(parts) == 1, "MCP tool result has no unique JSON text result")
        try: value = json.loads(parts[0])
        except ValueError: raise ContractError("MCP tool result is not JSON") from None
    require(isinstance(value, dict), "MCP tool result is not an object")
    require(value.get("status") != "error", "MCP backend operation failed: " + str(value.get("error", "unknown")))
    return value


def probe(server: dict) -> dict:
    return asyncio.run(inventory(server))
