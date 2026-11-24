"""Opt-in isolated platform smoke. Requires Docker CLI/socket and SMOKE_ROOT.

Uses only synthetic identities and a local Responses fixture. SMOKE_ROOT must
be a fresh directory bind-mounted at the same absolute path as on the Docker
host. Run the parent test container with host networking so its runner child
can reach the loopback fixture. No running platform service is used.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from chat_runtime import ChatRuntime, DockerCodexRunner
from server_input_api import handle_input_api
from smoke_user_input_codex import Provider
from workspace_store import WorkspaceStore


def main():
  root = Path(os.environ["SMOKE_ROOT"])
  if root.exists():
    raise ValueError("SMOKE_ROOT must be a fresh directory")
  root.mkdir(parents=True)
  provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
  threading.Thread(target=provider.serve_forever, daemon=True).start()
  user = {"username": "input-smoke", "role": "user", "budgetTokens": 100000, "usedTokens": 0,
          "codex": {"baseUrl": f"http://127.0.0.1:{provider.server_port}/v1", "apiKey": "fixture-not-a-secret"}}
  store = WorkspaceStore(root)
  runner = DockerCodexRunner()
  runner.image = os.environ.get("SMOKE_IMAGE", "ai-audit-codex-runner:user-input-20261124")
  runner.codex_root = root / "codex"
  runner.codex_home_root = runner.codex_root / "homes"
  runner.skill_path = None
  runner.mcp_server_config_path = None
  runner.run_network = "host"
  runner.timeout_seconds = 45
  runtime = ChatRuntime(store, {user["username"]: user}, runner=runner)

  class Website(BaseHTTPRequestHandler):
    def log_message(self, *_args):
      pass

    def do_GET(self):
      self.route("GET")

    def do_POST(self):
      self.route("POST")

    def read_json(self):
      return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")

    def write_json(self, body, status=200):
      data = json.dumps(body).encode()
      self.send_response(status)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(data)))
      self.end_headers()
      self.wfile.write(data)

    def route(self, method):
      url = urlparse(self.path)
      parts = url.path.strip("/").split("/")
      handle_input_api(self, runtime, method, parts[2], parts[5] if len(parts) > 5 else "", url.query, user)

  website = ThreadingHTTPServer(("127.0.0.1", 0), Website)
  threading.Thread(target=website.serve_forever, daemon=True).start()
  started = None
  try:
    workspace = store.create_workspace(user, "Synthetic interactive smoke", False, [])
    started = runtime.start_run(workspace["id"], user, {"prompt": "Use user_input_tool to ask whether to proceed.", "model": "input-smoke"})
    session_id = started["session"]["id"]
    base = f"http://127.0.0.1:{website.server_port}/api/workspaces/{workspace['id']}/chat/input-requests"
    deadline = time.monotonic() + 45
    requests = []
    while not requests and time.monotonic() < deadline:
      with urllib.request.urlopen(f"{base}?sessionId={session_id}") as response:
        requests = json.load(response)["requests"]
      if runtime.chat_store.get_run(started["run"]["id"])["status"] == "failed":
        raise AssertionError(runtime.chat_store.events(session_id))
      if runtime.chat_store.get_run(started["run"]["id"])["status"] == "completed":
        tools = [[{key: tool.get(key) for key in ("type", "name")} for tool in call.get("tools", [])] for call in Provider.calls]
        raise AssertionError(f"Codex completed without asking: {tools}; shapes={[(call.get("_fixture_path"), list(call), call.get("model")) for call in Provider.calls]}")
      time.sleep(0.1)
    assert requests and requests[0]["status"] == "pending", "request was not published to website API"
    assert runtime.public_session(session_id)["waitingForInput"]
    request_id = requests[0]["id"]
    time.sleep(0.3)
    assert len(Provider.calls) == 1, "Codex must wait for the website answer"
    submission = urllib.request.Request(f"{base}/{request_id}?sessionId={session_id}", method="POST",
                                        data=json.dumps({"status": "answered", "value": False}).encode(),
                                        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(submission) as response:
      assert json.load(response)["request"]["status"] == "answered"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
      run = runtime.chat_store.get_run(started["run"]["id"])
      if run["status"] in {"completed", "failed", "stopped"}:
        break
      time.sleep(0.1)
    assert run["status"] == "completed", runtime.chat_store.events(session_id)
    assert request_id in json.dumps(Provider.calls[-1]), "website answer did not reach Codex"
    assert not runtime.public_session(session_id)["waitingForInput"]
    print("PASS: real Codex -> MCP -> runner -> persisted website request -> HTTP answer -> durable delivery -> Codex continuation.")
  finally:
    if started and runtime.chat_store.get_run(started["run"]["id"])["status"] not in {"completed", "failed", "stopped"}:
      runtime.stop_run(started["run"]["workspaceId"], started["run"]["id"], user)
    runtime.shutdown = True
    provider.shutdown()
    website.shutdown()


if __name__ == "__main__":
  main()
