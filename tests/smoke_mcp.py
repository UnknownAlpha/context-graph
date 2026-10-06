"""Drive the MCP server over stdio as a client would: list tools, then exercise context_pack and the rest.

  python mcp_check.py [<repo>]     # launches `context-graph-mcp` from this venv, cwd = <repo>
"""
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REPO = sys.argv[1] if len(sys.argv) > 1 else "/home/muhammadtalha/tasks/ocp-tenant-provisioning"


async def main():
    try:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    exe = str(Path(sys.executable).with_name("context-graph-mcp"))
    # no repo argument on purpose: the server must pick up its working directory, as Claude Code launches it
    params = StdioServerParameters(command=exe, args=[], cwd=REPO, env={**os.environ, "CONTEXT_GRAPH_REPO": ""})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", [t.name for t in tools.tools])
            res = await s.call_tool("index_status", {})
            print(res.content[0].text)
            res = await s.call_tool("context_pack", {"question": "quota dot never turns green after the tenant is committed"})
            txt = res.content[0].text
            print(f"context_pack: ~{len(txt) // 4} tokens; first 600 chars:\n{txt[:600]}\n...\n{txt[-200:]}")
            res = await s.call_tool("path", {"a": "portal/app/index.html", "b": "charts/tenant/templates/resourcequota.yaml"})
            print(res.content[0].text)


asyncio.run(main())
