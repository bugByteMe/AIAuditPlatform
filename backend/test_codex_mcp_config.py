from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from codex_runner import DockerCodexRunner, RunnerError


class CodexMcpConfigTest(unittest.TestCase):
  def setUp(self) -> None:
    self.tempdir = tempfile.TemporaryDirectory()
    self.root = Path(self.tempdir.name)

  def tearDown(self) -> None:
    self.tempdir.cleanup()

  def test_runner_reads_mcp_servers_from_configured_json(self) -> None:
    runner = DockerCodexRunner()
    runner.codex_home_root = self.root / "codex_homes"
    runner.skill_path = None
    runner.mcp_server_config_path = self.root / "mcp_servers.json"
    runner.mcp_server_config_path.write_text(json.dumps({
      "mcpServers": {
        "local-tools": {
          "type": "stdio",
          "command": "node",
          "args": ["server.js", "--mode", "audit"],
          "env": {"LOG_LEVEL": "info"},
          "envVars": ["MCP_API_KEY"],
          "startupTimeoutSec": 30,
        },
        "remote-tools": {
          "type": "http",
          "url": "https://mcp.example.test/api",
          "bearerTokenEnvVar": "MCP_TOKEN",
          "headers": {"X-Tenant": "audit"},
          "enabled": True,
        },
        "disabled-tools": {
          "type": "stdio",
          "command": "node",
          "enabled": False,
        },
      }
    }), encoding="utf-8")
    run = {
      "id": "run_1",
      "user": "li.review",
      "sessionId": "chat_1",
      "model": "gpt-5.6-sol",
      "reasoning": "high",
      "mcpServerIds": ["local-tools", "remote-tools", "disabled-tools"],
      "codexSettings": {"baseUrl": "https://codex.example/v1", "apiKey": "sk-test"},
    }

    config = tomllib.loads((runner.prepare_codex_home(run) / "config.toml").read_text(encoding="utf-8"))

    local = config["mcp_servers"]["local-tools"]
    self.assertEqual(local["command"], "node")
    self.assertEqual(local["args"], ["server.js", "--mode", "audit"])
    self.assertEqual(local["env"], {"LOG_LEVEL": "info"})
    self.assertEqual(local["env_vars"], ["MCP_API_KEY"])
    self.assertEqual(local["startup_timeout_sec"], 30)
    remote = config["mcp_servers"]["remote-tools"]
    self.assertEqual(remote["url"], "https://mcp.example.test/api")
    self.assertEqual(remote["bearer_token_env_var"], "MCP_TOKEN")
    self.assertEqual(remote["http_headers"], {"X-Tenant": "audit"})
    self.assertTrue(remote["enabled"])
    self.assertNotIn("disabled-tools", config["mcp_servers"])

    run["mcpServerIds"] = []
    empty_config = tomllib.loads((runner.prepare_codex_home(run) / "config.toml").read_text(encoding="utf-8"))
    self.assertNotIn("mcp_servers", empty_config)

  def test_runner_rejects_invalid_mcp_server_json(self) -> None:
    runner = DockerCodexRunner()
    runner.mcp_server_config_path = self.root / "mcp_servers.json"
    runner.mcp_server_config_path.write_text(json.dumps({
      "mcpServers": {"invalid": {"command": "node", "url": "https://mcp.example.test"}}
    }), encoding="utf-8")

    with self.assertRaisesRegex(RunnerError, "exactly one of command or url"):
      runner.mcp_server_config_lines()


if __name__ == "__main__":
  unittest.main()
