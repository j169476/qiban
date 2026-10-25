"""Pooled MCP processes provide links only; control uses a verified CLI thread."""
import json

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .config import metadata, urls


async def serve(project):
    server = Server("qiban")

    @server.list_tools()
    async def listing():
        return [types.Tool(name="local_web", description="打开本机角色和管理入口；不启动角色、不读取私密资料。",
                           inputSchema={"type": "object", "properties": {}, "additionalProperties": False})]

    @server.call_tool()
    async def call(name, arguments):
        value = {"web": urls(metadata(project)), "control": "请从当前 Codex 聊天运行 qiban call；共享 MCP 不承载身份"}
        return [types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))]

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
