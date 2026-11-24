"""Built-in stdio MCP server. Only the run's mailbox is exposed to this process."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from user_input_schema import TOOL_SCHEMA, validate_question


async def ask(arguments, mailbox):
  question = validate_question(arguments)
  request_id = uuid.uuid4().hex
  path = mailbox / f"{request_id}.request.json"
  temporary = path.with_suffix(".tmp")
  temporary.write_text(json.dumps(question, ensure_ascii=False), encoding="utf-8")
  temporary.replace(path)
  response = mailbox / f"{request_id}.response.json"
  while not response.exists():
    await asyncio.sleep(0.25)
  return json.loads(response.read_text(encoding="utf-8"))


async def main():
  from mcp.server.lowlevel import Server
  from mcp.server.stdio import stdio_server
  from mcp.types import TextContent, Tool

  mailbox = Path(os.environ["AI_AUDIT_INPUT_DIR"])
  server = Server("ai-audit-input")

  @server.list_tools()
  async def list_tools():
    return [Tool(name="user_input_tool", description=(
      "Ask the user one question and wait for their answer. Supports yes/no, single or multiple "
      "choice, and text. Include optional Markdown and PNG/JPEG/WebP paths in /workspace. "
      "The user can skip. Use this tool when a decision or missing information is needed."
    ), inputSchema=TOOL_SCHEMA)]

  @server.call_tool()
  async def call_tool(name, arguments):
    if name != "user_input_tool":
      raise ValueError("unknown tool")
    result = await ask(arguments, mailbox)
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

  async with stdio_server() as (reader, writer):
    await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
  asyncio.run(main())
