"""Official MCP Python SDK, stdio transport. Identity is never a model argument."""

import json

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .api import READ_ONLY, TOOLS, call
from .errors import AssistantError


def build_server(engine):
    server = Server("bili-assistant")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=name,
                description=description,
                inputSchema=schema,
                annotations=types.ToolAnnotations(
                    readOnlyHint=name in READ_ONLY,
                    destructiveHint=name == "action_execute",
                    openWorldHint=name.startswith("bili_") or name in ("action_execute", "login_browser_finish"),
                ),
            )
            for name, (description, schema) in TOOLS.items()
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            result = await call(engine, name, arguments)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        except AssistantError as exc:
            return types.CallToolResult(
                isError=True,
                content=[types.TextContent(type="text", text=json.dumps(exc.as_dict(), ensure_ascii=False))],
            )
        except Exception:
            return types.CallToolResult(
                isError=True,
                content=[
                    types.TextContent(
                        type="text", text='{"error":"internal_error","message":"操作失败；未输出敏感诊断"}'
                    )
                ],
            )

    return server


async def serve(engine):
    # Codex may pool MCP processes across chats. An inherited environment variable
    # is not authenticated per-request identity. v0.1 uses the CLI from the current
    # chat for stateful tools until the host exposes a verified MCP request binding.
    engine.host.thread_id = None
    server = build_server(engine)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
