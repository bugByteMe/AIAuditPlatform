#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import secrets
import ssl
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from chat_runtime import DockerCodexRunner, safe_segment
from config import SETTINGS
from upload_store import WorkerUploadStore
from workspace_store import StorageError


TERMINAL = {"completed", "stopped", "failed"}


class WorkerState:
  def __init__(self, node: dict):
    self.node = node
    configured_root = Path(node["workspaceStorageDir"])
    self.root = configured_root if configured_root.is_absolute() else SETTINGS.root / configured_root
    self.state_dir = self.root / "worker_state" / safe_segment(node["id"])
    self.events_dir = self.state_dir / "events"
    self.runs_path = self.state_dir / "runs.json"
    self.lock = threading.RLock()
    self.condition = threading.Condition(self.lock)
    self.runner = DockerCodexRunner()
    self.uploads = WorkerUploadStore(self.root, SETTINGS.upload_stream_buffer_bytes)
    self.runs: dict[str, dict] = {}
    self.events: dict[str, list[dict]] = {}
    self.state_dir.mkdir(parents=True, exist_ok=True)
    self.events_dir.mkdir(parents=True, exist_ok=True)
    self.load()
    threading.Thread(target=self.lease_watchdog, name=f"worker-lease-{node['id']}", daemon=True).start()

  def load(self) -> None:
    if self.runs_path.exists():
      self.runs = json.loads(self.runs_path.read_text(encoding="utf-8"))
    for run_id, run in self.runs.items():
      path = self.event_path(run_id)
      self.events[run_id] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
      if run.get("status") not in TERMINAL:
        run["status"] = "failed"
        run["error"] = "worker agent restarted during execution"
        self.append_event(run_id, {"type": "error", "message": run["error"]})
    self.save_runs()

  def save_runs(self) -> None:
    self.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = self.runs_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(self.runs, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(self.runs_path)

  def event_path(self, run_id: str) -> Path:
    return self.events_dir / f"{safe_segment(run_id)}.jsonl"

  def append_event(self, run_id: str, event: dict) -> dict:
    with self.condition:
      rows = self.events.setdefault(run_id, [])
      stored = {**event, "id": len(rows) + 1}
      rows.append(stored)
      with self.event_path(run_id).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(stored, ensure_ascii=False, sort_keys=True) + "\n")
      self.condition.notify_all()
      return stored

  def validate_id(self, value: str, label: str) -> str:
    if not value or safe_segment(value) != value:
      raise ValueError(f"invalid {label}")
    return value

  def start_run(self, payload: dict) -> dict:
    run_id = self.validate_id(str(payload.get("id") or ""), "run id")
    workspace_id = self.validate_id(str(payload.get("workspaceId") or ""), "workspace id")
    session_id = self.validate_id(str(payload.get("sessionId") or ""), "session id")
    username = self.validate_id(str(payload.get("user") or ""), "username")
    cpu = float(payload.get("requestedCpu") or 0)
    memory_bytes = int(payload.get("requestedMemoryBytes") or 0)
    if not math.isfinite(cpu) or cpu <= 0 or memory_bytes <= 0 or cpu > float(self.node["cpu"]) or memory_bytes > int(self.node["memoryBytes"]):
      raise ValueError("run resources exceed worker capacity")
    workspace_path = self.root / "active" / workspace_id
    codex_home = self.root / "codex" / "homes" / username / session_id
    if not workspace_path.is_dir() or not codex_home.is_dir():
      raise ValueError("shared workspace or Codex home is unavailable")
    with self.lock:
      existing = self.runs.get(run_id)
      if existing:
        return self.public_run(existing)
      active_runs = [item for item in self.runs.values() if item.get("status") not in TERMINAL]
      used_cpu = sum(float(item.get("requestedCpu") or 0) for item in active_runs)
      used_memory = sum(int(item.get("requestedMemoryBytes") or 0) for item in active_runs)
      if used_cpu + cpu > float(self.node["cpu"]) or used_memory + memory_bytes > int(self.node["memoryBytes"]):
        raise ValueError("worker does not have enough available resources")
      run = {
        **payload,
        "id": run_id,
        "workspaceId": workspace_id,
        "sessionId": session_id,
        "user": username,
        "status": "starting",
        "container": f"ai-audit-{run_id}",
        "error": "",
        "lastLeaseMonotonic": time.monotonic(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
      }
      self.runs[run_id] = run
      self.events[run_id] = []
      self.save_runs()
      self.append_event(run_id, {"type": "starting", "message": "Worker accepted run."})
      threading.Thread(target=self.execute, args=(run_id, workspace_path, codex_home), name=f"worker-run-{run_id}", daemon=True).start()
      return self.public_run(run)

  def execute(self, run_id: str, workspace_path: Path, codex_home: Path) -> None:
    try:
      with self.lock:
        run = self.runs[run_id]
        if run["status"] == "stopping":
          run["status"] = "failed" if run.get("failureReason") else "stopped"
          run["error"] = str(run.get("failureReason") or "")
          self.save_runs()
          self.condition.notify_all()
          return
        run["status"] = "running"
        self.save_runs()
      for event in self.runner.start_prepared(run, workspace_path, codex_home):
        with self.lock:
          if self.runs[run_id]["status"] == "stopping":
            break
          self.append_event(run_id, event)
      with self.lock:
        run = self.runs[run_id]
        if run.get("failureReason"):
          run["status"] = "failed"
          run["error"] = str(run["failureReason"])
        else:
          run["status"] = "stopped" if run["status"] == "stopping" else "completed"
        self.save_runs()
    except Exception as exc:
      with self.lock:
        run = self.runs.get(run_id)
        if run:
          run["status"] = "failed"
          run["error"] = str(exc)
          self.append_event(run_id, {"type": "error", "message": str(exc)})
          self.save_runs()

  def renew_lease(self, run_id: str) -> dict:
    with self.lock:
      run = self.require_run(run_id)
      run["lastLeaseMonotonic"] = time.monotonic()
      return self.public_run(run)

  def stop_run(self, run_id: str, reason: str = "stop requested") -> dict:
    with self.lock:
      run = self.require_run(run_id)
      if run["status"] in TERMINAL:
        return self.public_run(run)
      run["status"] = "stopping"
      if reason == "control-plane lease expired":
        run["failureReason"] = reason
      self.append_event(run_id, {"type": "stopping", "message": reason})
      self.save_runs()
    self.runner.stop(run)
    return self.public_run(run)

  def lease_watchdog(self) -> None:
    while True:
      time.sleep(max(0.25, SETTINGS.worker_run_lease_seconds / 4))
      expired = []
      with self.lock:
        now = time.monotonic()
        for run_id, run in self.runs.items():
          if run.get("status") not in TERMINAL and now - float(run.get("lastLeaseMonotonic") or 0) > SETTINGS.worker_run_lease_seconds:
            expired.append(run_id)
      for run_id in expired:
        try:
          self.stop_run(run_id, "control-plane lease expired")
        except Exception:
          pass

  def require_run(self, run_id: str) -> dict:
    run = self.runs.get(run_id)
    if not run:
      raise KeyError(run_id)
    return run

  def acknowledge_run(self, run_id: str) -> dict:
    with self.lock:
      run = self.require_run(run_id)
      if run.get("status") not in TERMINAL:
        raise ValueError("run is not terminal")
      self.runs.pop(run_id, None)
      self.events.pop(run_id, None)
      self.event_path(run_id).unlink(missing_ok=True)
      self.save_runs()
      return {"id": run_id, "acknowledged": True}

  def public_run(self, run: dict) -> dict:
    return {key: run.get(key) for key in ["id", "status", "container", "error", "created"]}

  def run_events(self, run_id: str, after: int, wait_seconds: float) -> dict:
    deadline = time.monotonic() + max(0, min(wait_seconds, 10))
    with self.condition:
      while True:
        run = self.require_run(run_id)
        rows = [event for event in self.events.get(run_id, []) if int(event["id"]) > after]
        if rows or run["status"] in TERMINAL or time.monotonic() >= deadline:
          return {"events": rows, **self.public_run(run)}
        self.condition.wait(min(0.5, deadline - time.monotonic()))

  def health(self) -> dict:
    with self.lock:
      active = [run_id for run_id, run in self.runs.items() if run.get("status") not in TERMINAL]
    return {
      "nodeId": self.node["id"],
      "status": "healthy",
      "cpuTotal": self.node["cpu"],
      "memoryTotalBytes": self.node["memoryBytes"],
      "activeRuns": sorted(active),
      "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


class WorkerHandler(BaseHTTPRequestHandler):
  state: WorkerState
  auth_token: str

  def log_message(self, fmt: str, *args) -> None:
    return

  def do_GET(self) -> None:
    self.handle_request("GET")

  def do_POST(self) -> None:
    self.handle_request("POST")

  def do_PUT(self) -> None:
    self.handle_request("PUT")

  def do_DELETE(self) -> None:
    self.handle_request("DELETE")

  def handle_request(self, method: str) -> None:
    if not self.authorized():
      self.write_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
      return
    parsed = urlparse(self.path)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
      if method == "GET" and parsed.path == "/v1/health":
        self.write_json(self.state.health())
      elif method == "POST" and parsed.path == "/v1/runs":
        self.write_json(self.state.start_run(self.read_json()), HTTPStatus.ACCEPTED)
      elif method == "POST" and parsed.path == "/v1/uploads":
        self.write_json(self.state.uploads.initialize(self.read_json()), HTTPStatus.CREATED)
      elif len(parts) >= 3 and parts[:2] == ["v1", "uploads"]:
        upload_id = parts[2]
        action = parts[3] if len(parts) > 3 else ""
        if method == "GET" and not action:
          self.write_json(self.state.uploads.status(upload_id))
        elif method == "PUT" and action == "files" and len(parts) == 5:
          query = parse_qs(parsed.query)
          length = int(self.headers.get("Content-Length") or -1)
          result = self.state.uploads.write_chunk(upload_id, int(parts[4]), int((query.get("offset") or ["0"])[0]), self.rfile, length)
          self.write_json(result)
        elif method == "POST" and action == "complete":
          self.write_json(self.state.uploads.complete(upload_id), HTTPStatus.ACCEPTED)
        elif method == "DELETE" and not action:
          self.write_json(self.state.uploads.cancel(upload_id))
        else:
          self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
      elif len(parts) >= 3 and parts[:2] == ["v1", "runs"]:
        run_id = parts[2]
        action = parts[3] if len(parts) > 3 else ""
        if method == "GET" and not action:
          self.write_json(self.state.public_run(self.state.require_run(run_id)))
        elif method == "GET" and action == "events":
          query = parse_qs(parsed.query)
          self.write_json(self.state.run_events(run_id, int((query.get("after") or ["0"])[0]), float((query.get("wait") or ["0"])[0])))
        elif method == "POST" and action == "stop":
          self.write_json(self.state.stop_run(run_id))
        elif method == "POST" and action == "lease":
          self.write_json(self.state.renew_lease(run_id))
        elif method == "DELETE" and not action:
          self.write_json(self.state.acknowledge_run(run_id))
        else:
          self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
      else:
        self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
    except KeyError:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
    except StorageError as exc:
      self.write_json({"error": exc.code, "message": exc.message}, HTTPStatus.BAD_REQUEST)
    except ValueError as exc:
      self.write_json({"error": "bad_request", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
    except Exception:
      traceback.print_exc()
      self.write_json({"error": "internal_error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

  def authorized(self) -> bool:
    supplied = self.headers.get("Authorization", "")
    expected = f"Bearer {self.auth_token}"
    return bool(self.auth_token) and secrets.compare_digest(supplied, expected)

  def read_json(self) -> dict:
    length = int(self.headers.get("Content-Length") or 0)
    return json.loads(self.rfile.read(length) or b"{}")

  def write_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    self.wfile.write(body)


def main() -> None:
  parser = argparse.ArgumentParser(description="AI Audit compute worker")
  parser.add_argument("--node-id", default=SETTINGS.worker_node_id)
  parser.add_argument("--host", default="0.0.0.0")
  args = parser.parse_args()
  node = next((item for item in SETTINGS.compute_nodes if item["id"] == args.node_id), None)
  if not node:
    raise SystemExit("configured --node-id is required")
  if not SETTINGS.worker_auth_token:
    raise SystemExit("AI_AUDIT_WORKER_AUTH_TOKEN is required")
  if not node.get("tlsCertFile") or not node.get("tlsKeyFile"):
    raise SystemExit("worker TLS certificate and key paths are required")
  WorkerHandler.state = WorkerState(node)
  WorkerHandler.auth_token = SETTINGS.worker_auth_token
  server = ThreadingHTTPServer((args.host, int(node["port"])), WorkerHandler)
  context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
  cert_file = Path(node["tlsCertFile"])
  key_file = Path(node["tlsKeyFile"])
  context.load_cert_chain(
    cert_file if cert_file.is_absolute() else SETTINGS.root / cert_file,
    key_file if key_file.is_absolute() else SETTINGS.root / key_file,
  )
  server.socket = context.wrap_socket(server.socket, server_side=True)
  print(f"AI Audit worker {node['id']} serving https://{args.host}:{node['port']}", flush=True)
  server.serve_forever()


if __name__ == "__main__":
  main()
