"""Read-only wire-protocol test against the locally configured MCP server."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke(config: str) -> dict:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fantasy_mcp", "--config", str(Path(config).resolve())],
        env={"PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams, read_timeout_seconds=120) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            resources = await session.list_resources()
            results = {}
            for name, args in [
                ("list_capabilities", {}),
                ("list_animation_references", {}),
                ("list_jobs", {"module": "model3d", "limit": 5}),
                ("list_jobs", {"module": "animation", "limit": 5}),
                ("get_effects_guide", {}),
                ("list_effects", {}),
            ]:
                result = await session.call_tool(name, args)
                if result.is_error:
                    raise RuntimeError(f"Read-only MCP call failed: {name}")
                value = result.structured_content
                if value is None:
                    value = json.loads("".join(item.text for item in result.content if item.type == "text"))
                if "result" in value and len(value) == 1:
                    value = value["result"]
                key = name + ("_" + args["module"] if "module" in args else "")
                if name == "list_capabilities":
                    results[key] = {m: entry.get("available") for m, entry in value.get("modules", {}).items()}
                    if set(results[key]) != {"model3d", "animation", "image", "speech", "music", "effects"} or not all(results[key].values()):
                        raise RuntimeError("Expected all six module APIs to be reachable")
                elif name == "list_animation_references":
                    results[key] = {"characters": len(value.get("characters", [])), "motions": len(value.get("motions", []))}
                elif name == "get_effects_guide":
                    results[key] = {"format_available": bool(value.get("format")), "limits": value.get("limits", {})}
                elif name == "list_effects":
                    results[key] = {"count": len(value.get("effects", []))}
                else:
                    results[key] = {"count": len(value.get("jobs", value.get("items", [])))}
            await session.read_resource("fantasy://usage")
            await session.read_resource("fantasy://animation-schema")
            await session.read_resource("fantasy://effects-usage")
            return {
                "server": initialized.server_info.name,
                "version": initialized.server_info.version,
                "protocol": initialized.protocol_version,
                "tools": [tool.name for tool in tools.tools],
                "resources": [str(item.uri) for item in resources.resources],
                "checks": results,
                "generation_submitted": False,
            }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output")
    arguments = parser.parse_args()
    result = asyncio.run(smoke(arguments.config))
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if arguments.output:
        Path(arguments.output).write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
