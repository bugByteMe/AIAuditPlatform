from __future__ import annotations

import json
import math
from pathlib import Path
from runner_errors import RunnerError
from user_input_schema import SERVER_NAME

def toml_string(value: str) -> str:
  return value.replace("\\", "\\\\").replace('"', '\\"')


MCP_SERVER_FIELD_ALIASES = {
  "bearerTokenEnvVar": "bearer_token_env_var",
  "disabledTools": "disabled_tools",
  "enabledTools": "enabled_tools",
  "envVars": "env_vars",
  "envHttpHeaders": "env_http_headers",
  "headers": "http_headers",
  "httpHeaders": "http_headers",
  "startupTimeoutSec": "startup_timeout_sec",
  "toolTimeoutSec": "tool_timeout_sec",
}
MCP_SERVER_FIELD_ORDER = (
  "command",
  "args",
  "env",
  "env_vars",
  "cwd",
  "url",
  "bearer_token_env_var",
  "http_headers",
  "env_http_headers",
  "enabled",
  "required",
  "startup_timeout_sec",
  "tool_timeout_sec",
  "enabled_tools",
  "disabled_tools",
)


def load_mcp_servers(path: Path) -> dict[str, dict]:
  try:
    payload = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError) as exc:
    raise RunnerError(f"Failed to read MCP server configuration from {path}: {exc}") from exc
  if not isinstance(payload, dict):
    raise RunnerError(f"MCP server configuration in {path} must be a JSON object")
  unexpected_top_level = set(payload) - {"mcpServers", "mcp_servers"}
  if unexpected_top_level:
    raise RunnerError(
      f"MCP server configuration in {path} has unsupported top-level fields: {', '.join(sorted(unexpected_top_level))}"
    )
  if "mcpServers" in payload and "mcp_servers" in payload:
    raise RunnerError(f"MCP server configuration in {path} must not contain both mcpServers and mcp_servers")
  servers = payload.get("mcpServers")
  if servers is None:
    servers = payload.get("mcp_servers")
  if not isinstance(servers, dict):
    raise RunnerError(f"MCP server configuration in {path} must contain an mcpServers object")
  if SERVER_NAME in servers:
    raise RunnerError(f"MCP server name {SERVER_NAME} is reserved")
  return {name: normalize_mcp_server(name, raw_server, path) for name, raw_server in servers.items()}


def normalize_mcp_server(name, raw_server, path: Path) -> dict:
  if not isinstance(name, str) or not name.strip():
    raise RunnerError(f"MCP server configuration in {path} contains an invalid server name")
  if not isinstance(raw_server, dict):
    raise RunnerError(f"MCP server {name} in {path} must be a JSON object")
  normalized = {}
  server_type = raw_server.get("type")
  if server_type is not None and server_type not in {"stdio", "http"}:
    raise RunnerError(f"MCP server {name} in {path} has unsupported type {server_type!r}")
  for source_key, value in raw_server.items():
    if source_key == "type":
      continue
    key = MCP_SERVER_FIELD_ALIASES.get(source_key, source_key)
    if key not in MCP_SERVER_FIELD_ORDER:
      raise RunnerError(f"MCP server {name} in {path} has unsupported field {source_key}")
    if key in normalized:
      raise RunnerError(f"MCP server {name} in {path} configures {key} more than once")
    normalized[key] = value
  has_command = isinstance(normalized.get("command"), str) and bool(normalized["command"].strip())
  has_url = isinstance(normalized.get("url"), str) and bool(normalized["url"].strip())
  if has_command == has_url:
    raise RunnerError(f"MCP server {name} in {path} must configure exactly one of command or url")
  if (server_type == "stdio" and not has_command) or (server_type == "http" and not has_url):
    raise RunnerError(f"MCP server {name} in {path} has a type that does not match its transport")
  validate_mcp_server_fields(name, normalized, path, has_command)
  return normalized


def validate_mcp_server_fields(name: str, server: dict, path: Path, has_command: bool) -> None:
  string_fields = {"command", "cwd", "url", "bearer_token_env_var"}
  list_fields = {"args", "env_vars", "enabled_tools", "disabled_tools"}
  map_fields = {"env", "http_headers", "env_http_headers"}
  for key in string_fields & set(server):
    if not isinstance(server[key], str) or not server[key].strip():
      raise RunnerError(f"MCP server {name} field {key} in {path} must be a non-empty string")
  for key in list_fields & set(server):
    if not isinstance(server[key], list) or not all(isinstance(item, str) for item in server[key]):
      raise RunnerError(f"MCP server {name} field {key} in {path} must be an array of strings")
  for key in map_fields & set(server):
    if not isinstance(server[key], dict) or not all(isinstance(item, str) and isinstance(value, str) for item, value in server[key].items()):
      raise RunnerError(f"MCP server {name} field {key} in {path} must be an object of string values")
  for key in {"enabled", "required"} & set(server):
    if not isinstance(server[key], bool):
      raise RunnerError(f"MCP server {name} field {key} in {path} must be a boolean")
  for key in {"startup_timeout_sec", "tool_timeout_sec"} & set(server):
    value = server[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
      raise RunnerError(f"MCP server {name} field {key} in {path} must be a positive number")
  stdio_only = {"args", "env", "env_vars", "cwd"}
  http_only = {"bearer_token_env_var", "http_headers", "env_http_headers"}
  invalid_transport_fields = (http_only if has_command else stdio_only) & set(server)
  if invalid_transport_fields:
    transport = "stdio" if has_command else "HTTP"
    raise RunnerError(
      f"MCP server {name} in {path} has fields unsupported by its {transport} transport: "
      f"{', '.join(sorted(invalid_transport_fields))}"
    )


def toml_key(value: str) -> str:
  return json.dumps(value, ensure_ascii=False)


def toml_value(value) -> str:
  if isinstance(value, str):
    return json.dumps(value, ensure_ascii=False)
  if isinstance(value, bool):
    return "true" if value else "false"
  if isinstance(value, (int, float)):
    return str(value)
  if isinstance(value, list):
    return "[" + ", ".join(toml_value(item) for item in value) + "]"
  if isinstance(value, dict):
    return "{ " + ", ".join(f"{toml_key(str(key))} = {toml_value(item)}" for key, item in value.items()) + " }"
  raise TypeError(f"unsupported TOML value: {type(value).__name__}")
