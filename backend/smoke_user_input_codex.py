"""Manual container smoke: actual Codex + MCP, synthetic Responses provider, no credentials.

Run inside the rebuilt runner image: python3.12 /test/backend/smoke_user_input_codex.py
The platform's website submission/delivery path is covered by test_input_runtime.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from input_bridge import InputBridge


class Provider(BaseHTTPRequestHandler):
  calls = []

  def log_message(self, *_args):
    pass

  def do_POST(self):
    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
    body["_fixture_path"] = self.path
    self.calls.append(body)
    first = len(self.calls) == 1
    tool_names = [tool.get("name", "") for tool in body.get("tools", [])]
    namespace = next((tool["name"] for tool in body.get("tools", []) if tool.get("type") == "namespace" and tool.get("name") == "mcp__ai_audit_input"), None)
    name = "user_input_tool" if namespace else next((name for name in tool_names if "user_input_tool" in name), None)
    if first and not name:
      self.send_error(500, f"interactive tool missing: {tool_names}")
      return
    if first:
      item = {"type": "function_call", "id": "fc_input", "call_id": "call_input", "name": name,
              "arguments": json.dumps({"type": "yes_no", "title": "Proceed with the smoke test?"})}
      if namespace:
        item["namespace"] = namespace
    else:
      item = {"type": "message", "id": "msg_done", "status": "completed", "role": "assistant",
              "content": [{"type": "output_text", "text": "Interactive smoke complete.", "annotations": []}]}
    response = {"id": f"resp_{len(self.calls)}", "object": "response", "status": "completed",
                "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    events = [
      ("response.created", {"response": {**response, "status": "in_progress", "output": []}}),
      ("response.output_item.added", {"output_index": 0, "item": item}),
      ("response.output_item.done", {"output_index": 0, "item": item}),
      ("response.completed", {"response": response}),
    ]
    data = "".join(f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n" for kind, payload in events).encode()
    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)


def main():
  server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    home = root / "home"
    home.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    bridge = InputBridge(root, "smoke")
    bridge.path.mkdir(parents=True)
    (home / "config.toml").write_text(f'''model_provider = "fixture"
model = "input-smoke"
[model_providers.fixture]
name = "fixture"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
[mcp_servers.ai_audit_input]
command = "python3.12"
args = ["/opt/ai-audit/user_input_mcp.py"]
tool_timeout_sec = 2147483647
required = true
env = {{ AI_AUDIT_INPUT_DIR = "{bridge.path}" }}
''')
    log_path = root / "codex.log"
    with log_path.open("w") as log:
      process = subprocess.Popen([
        "codex", "--ask-for-approval", "never", "--sandbox", "danger-full-access", "exec",
        "--json", "--skip-git-repo-check", "-C", str(workspace), "Ask whether to proceed using user_input_tool.",
      ], env={**os.environ, "CODEX_HOME": str(home)}, stdout=log, stderr=subprocess.STDOUT)
      try:
        deadline = time.monotonic() + 45
        paths = []
        while not paths and process.poll() is None and time.monotonic() < deadline:
          paths = list(bridge.path.glob("*.request.json"))
          time.sleep(0.1)
        if not paths:
          details = [{"tools": [{key: tool.get(key) for key in ("type", "name", "namespace")} for tool in call.get("tools", [])],
                      "results": [item for item in call.get("input", []) if isinstance(item, dict) and item.get("type") == "function_call_output"]} for call in Provider.calls]
          raise AssertionError(f"Codex did not request input: {log_path.read_text()}\nProvider diagnostics: {details}")
        time.sleep(0.3)
        assert process.poll() is None, "Codex must wait while input is pending"
        request_id = paths[0].name.split(".")[0]
        result = {"requestId": request_id, "status": "answered", "value": False}
        bridge.respond(request_id, result)
        assert process.wait(timeout=30) == 0, log_path.read_text()
        assert len(Provider.calls) >= 2, "Codex did not send the tool result back to the provider"
        returned = json.dumps(Provider.calls[-1])
        assert request_id in returned and "answered" in returned, "tool result missing from continuation"
        print("PASS: Codex parsed the long timeout, discovered the MCP tool, waited, and returned the answer.")
      finally:
        if process.poll() is None:
          process.terminate()
          process.wait(timeout=10)
        server.shutdown()


if __name__ == "__main__":
  main()
