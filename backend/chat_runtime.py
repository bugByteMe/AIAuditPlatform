from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable

from workspace_store import StorageError, WorkspaceStore, generated_id, now_string


RUNNING_STATES = {"queued", "starting", "running", "stopping"}
TERMINAL_STATES = {"completed", "stopped", "failed"}


class RunnerError(RuntimeError):
  pass


class CodexRunner:
  def start(self, run: dict, workspace_path: Path) -> Iterable[dict]:
    raise NotImplementedError

  def stop(self, run: dict) -> None:
    raise NotImplementedError


class DockerCodexRunner(CodexRunner):
  def __init__(self) -> None:
    self.image = os.environ.get("AI_AUDIT_CODEX_IMAGE", "ai-audit-codex-runner:0.153.4")
    self.codex_home_root = Path(os.environ.get("AI_AUDIT_CODEX_HOME_ROOT", "") or "workspace_storage/codex_homes")
    self.timeout_seconds = int(os.environ.get("AI_AUDIT_RUN_TIMEOUT_SECONDS", "3600"))
    self.processes: dict[str, subprocess.Popen] = {}
    self.lock = threading.Lock()

  def start(self, run: dict, workspace_path: Path) -> Iterable[dict]:
    container_name = f"ai-audit-{run['id']}"
    run["container"] = container_name
    codex_home = self.prepare_codex_home(run)
    command = [
      "docker",
      "run",
      "--rm",
      "--name",
      container_name,
      "--network",
      os.environ.get("AI_AUDIT_RUN_NETWORK", "bridge"),
      "--cpus",
      os.environ.get("AI_AUDIT_RUN_CPUS", "2"),
      "--memory",
      os.environ.get("AI_AUDIT_RUN_MEMORY", "4g"),
      "-v",
      f"{workspace_path.resolve()}:/workspace",
      "-v",
      f"{codex_home.resolve()}:/home/codex/.codex",
      "-w",
      "/workspace",
    ]
    command.extend([self.image, *self.codex_command_args(run)])
    run["codexHome"] = str(codex_home)
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    with self.lock:
      self.processes[run["id"]] = process
    try:
      if process.stdout:
        for line in process.stdout:
          if time.monotonic() - started > self.timeout_seconds:
            self.stop(run)
            yield {"type": "error", "message": "Run timed out."}
            break
          yield self.parse_json_event(line)
      returncode = process.wait(timeout=10)
      if returncode != 0:
        stderr = process.stderr.read() if process.stderr else ""
        raise RunnerError(stderr.strip() or f"Codex exited with status {returncode}")
    finally:
      with self.lock:
        self.processes.pop(run["id"], None)

  def parse_json_event(self, line: str) -> dict:
    try:
      payload = json.loads(line)
    except json.JSONDecodeError:
      return {"type": "progress", "message": line.strip()}
    if "message" in payload and isinstance(payload["message"], str):
      return {"type": "assistant", "message": payload["message"]}
    if "text" in payload and isinstance(payload["text"], str):
      return {"type": "assistant", "message": payload["text"]}
    if "tokens" in payload:
      return {"type": "usage", "message": "Token usage updated.", "tokens": int(payload.get("tokens") or 0)}
    event_type = str(payload.get("type") or payload.get("event") or "progress")
    message = str(payload.get("message") or payload.get("summary") or payload.get("type") or "Codex event")
    result = {"type": event_type, "message": message, "raw": payload}
    native_id = self.extract_codex_session_id(payload)
    if native_id:
      result["codexSessionId"] = native_id
    usage = payload.get("usage")
    if isinstance(usage, dict):
      total = usage.get("total_tokens") or usage.get("totalTokens") or 0
      if total:
        result["tokens"] = int(total)
    return result

  def prepare_codex_home(self, run: dict) -> Path:
    settings = run.get("codexSettings") or {}
    api_key = str(settings.get("apiKey") or "").strip()
    base_url = str(settings.get("baseUrl") or "").strip()
    if not api_key:
      raise RunnerError("Codex API key is not configured for this user.")
    codex_home = self.codex_home_root / safe_segment(run["user"]) / safe_segment(run["sessionId"])
    codex_home.mkdir(parents=True, exist_ok=True)
    os.chmod(codex_home, 0o700)
    auth = {"auth_mode": "apikey", "OPENAI_API_KEY": api_key}
    (codex_home / "auth.json").write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(codex_home / "auth.json", 0o600)
    config = [
      'model_provider = "OpenAI"',
      f'model = "{toml_string(str(run["model"]))}"',
      f'model_reasoning_effort = "{toml_string(str(run["reasoning"]))}"',
      "disable_response_storage = false",
      "",
      "[model_providers.OpenAI]",
      'name = "OpenAI"',
      f'base_url = "{toml_string(base_url)}"',
      'wire_api = "responses"',
      "requires_openai_auth = true",
      "",
      '[projects."/workspace"]',
      'trust_level = "trusted"',
      "",
    ]
    (codex_home / "config.toml").write_text("\n".join(config), encoding="utf-8")
    os.chmod(codex_home / "config.toml", 0o600)
    return codex_home

  def codex_command_args(self, run: dict) -> list[str]:
    base = ["codex", "exec"]
    if run.get("codexResume"):
      base.extend(["resume", "--json", "--skip-git-repo-check", "-m", run["model"]])
      if run.get("codexSessionId"):
        base.append(run["codexSessionId"])
      else:
        base.append("--last")
      base.append(run["prompt"])
      return base
    return [
      *base,
      "--json",
      "--skip-git-repo-check",
      "-- --ask-for-approval",
      "never",
      "-C",
      "/workspace",
      "-m",
      run["model"],
      run["prompt"],
    ]

  def extract_codex_session_id(self, payload: dict) -> str | None:
    keys = ["session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId"]
    stack = [payload]
    while stack:
      current = stack.pop()
      if isinstance(current, dict):
        for key in keys:
          value = current.get(key)
          if isinstance(value, str) and value:
            return value
        stack.extend(value for value in current.values() if isinstance(value, (dict, list)))
      elif isinstance(current, list):
        stack.extend(value for value in current if isinstance(value, (dict, list)))
    return None

  def stop(self, run: dict) -> None:
    container = run.get("container")
    if container:
      subprocess.run(["docker", "stop", "--time", "10", container], capture_output=True, timeout=20, check=False)
    with self.lock:
      process = self.processes.get(run["id"])
    if process and process.poll() is None:
      process.terminate()


class ChatRuntime:
  def __init__(self, store: WorkspaceStore, users: dict[str, dict], runner: CodexRunner | None = None, capacity: int = 1):
    self.store = store
    self.users = users
    self.runner = runner or DockerCodexRunner()
    self.capacity = max(1, capacity)
    self.lock = threading.RLock()
    self.condition = threading.Condition(self.lock)
    self.queue: queue.Queue[str] = queue.Queue()
    self.active_runs: set[str] = set()
    self.stop_requested: set[str] = set()
    self.shutdown = False
    self.scheduler = threading.Thread(target=self.scheduler_loop, name="ai-audit-chat-scheduler", daemon=True)
    self.scheduler.start()

  def ensure_chat_metadata(self, metadata: dict) -> None:
    metadata.setdefault("chatSessions", {})
    metadata.setdefault("runs", {})
    metadata.setdefault("events", {})
    for workspace in metadata.get("workspaces", {}).values():
      workspace.setdefault("sessions", [])

  def list_sessions(self, workspace_id: str, user: dict) -> list[dict]:
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      workspace = self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      return [self.public_session(metadata, session["id"]) for session in workspace.get("sessions", [])]

  def create_session(self, workspace_id: str, user: dict, title: str | None = None) -> dict:
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      workspace = self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      timestamp = now_string()
      session = {
        "id": generated_id("chat"),
        "workspaceId": workspace_id,
        "title": title or "New audit task",
        "status": "stopped",
        "updated": timestamp,
        "tokens": "0",
        "totalTokens": 0,
        "latestRunId": None,
        "createdBy": user["username"],
        "created": timestamp,
      }
      metadata["chatSessions"][session["id"]] = session
      metadata["events"][session["id"]] = []
      workspace.setdefault("sessions", []).insert(0, {"id": session["id"]})
      workspace["updated"] = timestamp
      self.store.save_metadata(metadata)
      return self.public_session(metadata, session["id"])

  def start_run(self, workspace_id: str, user: dict, payload: dict) -> dict:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
      raise StorageError("bad_request", "prompt is required")
    if int(user.get("usedTokens") or 0) >= int(user.get("budgetTokens") or 0):
      raise StorageError("budget_exhausted", "user token budget is exhausted")
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      workspace = self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      if workspace.get("locked"):
        raise StorageError("workspace_locked", "workspace has an active write lock")
      session_id = str(payload.get("sessionId") or "")
      if session_id and session_id not in metadata["chatSessions"]:
        session_id = ""
      if not session_id:
        session = self._create_session_in_metadata(metadata, workspace, user, self.session_title(prompt))
      else:
        session = metadata["chatSessions"][session_id]
        if session["workspaceId"] != workspace_id:
          raise StorageError("bad_request", "chat session belongs to another workspace")
      timestamp = now_string()
      run = {
        "id": generated_id("run"),
        "workspaceId": workspace_id,
        "sessionId": session["id"],
        "user": user["username"],
        "status": "queued",
        "prompt": prompt,
        "model": str(payload.get("model") or "gpt-5.6-sol"),
        "reasoning": str(payload.get("reasoning") or "high"),
        "worker": "local-docker",
        "container": "",
        "created": timestamp,
        "updated": timestamp,
        "baseSnapshotId": workspace.get("latestSnapshotId"),
        "resultSnapshotId": None,
        "tokens": 0,
        "codexSettings": self.user_codex_settings(user),
        "codexResume": bool(session.get("codexNativeResumable")),
        "codexSessionId": session.get("codexSessionId"),
      }
      metadata["runs"][run["id"]] = run
      workspace["locked"] = True
      workspace["activeRunId"] = run["id"]
      session["status"] = "queued"
      session["latestRunId"] = run["id"]
      session["updated"] = timestamp
      self.append_event(metadata, session["id"], "user", prompt, run["id"], persist=False)
      self.append_event(metadata, session["id"], "queued", "Run queued. Waiting for local Docker capacity.", run["id"], persist=False)
      self.store.save_metadata(metadata)
      self.queue.put(run["id"])
      self.condition.notify_all()
      return {"session": self.public_session(metadata, session["id"]), "run": run}

  def _create_session_in_metadata(self, metadata: dict, workspace: dict, user: dict, title: str) -> dict:
    timestamp = now_string()
    session = {
      "id": generated_id("chat"),
      "workspaceId": workspace["id"],
      "title": title,
      "status": "stopped",
      "updated": timestamp,
      "tokens": "0",
      "totalTokens": 0,
      "latestRunId": None,
      "createdBy": user["username"],
      "created": timestamp,
    }
    metadata["chatSessions"][session["id"]] = session
    metadata["events"][session["id"]] = []
    workspace.setdefault("sessions", []).insert(0, {"id": session["id"]})
    return session

  def stop_run(self, workspace_id: str, run_id: str, user: dict) -> dict:
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      run = metadata["runs"].get(run_id)
      if not run or run["workspaceId"] != workspace_id:
        raise StorageError("not_found", "run not found")
      if run["status"] in TERMINAL_STATES:
        return {"run": run}
      run["status"] = "stopping"
      run["updated"] = now_string()
      self.stop_requested.add(run_id)
      self.append_event(metadata, run["sessionId"], "stopping", "Stop requested. Creating a resumable checkpoint.", run_id, persist=False)
      self.store.save_metadata(metadata)
      self.condition.notify_all()
    self.runner.stop(run)
    return {"run": run}

  def events(self, workspace_id: str, session_id: str, after: int, user: dict) -> list[dict]:
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      session = metadata["chatSessions"].get(session_id)
      if not session or session["workspaceId"] != workspace_id:
        raise StorageError("not_found", "chat session not found")
      return [event for event in metadata["events"].get(session_id, []) if int(event["id"]) > after]

  def wait_events(self, workspace_id: str, session_id: str, after: int, user: dict, timeout: float = 15) -> list[dict]:
    deadline = time.monotonic() + timeout
    with self.condition:
      while True:
        events = self.events(workspace_id, session_id, after, user)
        if events or time.monotonic() >= deadline:
          return events
        self.condition.wait(min(1, deadline - time.monotonic()))

  def scheduler_loop(self) -> None:
    while not self.shutdown:
      run_id = self.queue.get()
      while len(self.active_runs) >= self.capacity:
        time.sleep(0.1)
      self.active_runs.add(run_id)
      threading.Thread(target=self.execute_run, args=(run_id,), name=f"ai-audit-run-{run_id}", daemon=True).start()

  def execute_run(self, run_id: str) -> None:
    try:
      with self.lock:
        metadata = self.store.load_metadata()
        self.ensure_chat_metadata(metadata)
        run = metadata["runs"].get(run_id)
        if not run:
          return
        run["status"] = "starting"
        run["updated"] = now_string()
        session = metadata["chatSessions"][run["sessionId"]]
        session["status"] = "running"
        session["updated"] = run["updated"]
        self.append_event(metadata, run["sessionId"], "starting", "Starting Codex container.", run_id, persist=False)
        self.store.save_metadata(metadata)
        self.condition.notify_all()
      workspace_path = self.store.workspace_path(run["workspaceId"])
      for event in self.runner.start(run, workspace_path):
        with self.lock:
          metadata = self.store.load_metadata()
          self.ensure_chat_metadata(metadata)
          stored_run = metadata["runs"].get(run_id)
          if not stored_run:
            return
          if run_id in self.stop_requested:
            stored_run["status"] = "stopping"
            self.store.save_metadata(metadata)
            break
          stored_run["status"] = "running"
          stored_run["container"] = run.get("container", stored_run.get("container", ""))
          self.record_runner_event(metadata, stored_run, event)
          self.store.save_metadata(metadata)
          self.condition.notify_all()
      with self.lock:
        metadata = self.store.load_metadata()
        self.ensure_chat_metadata(metadata)
        run = metadata["runs"].get(run_id)
        if run:
          final_status = "stopped" if run_id in self.stop_requested or run["status"] == "stopping" else "completed"
          self.finalize_run(metadata, run, final_status, None)
          self.store.save_metadata(metadata)
          self.condition.notify_all()
    except Exception as exc:
      with self.lock:
        metadata = self.store.load_metadata()
        self.ensure_chat_metadata(metadata)
        run = metadata["runs"].get(run_id)
        if run:
          self.finalize_run(metadata, run, "failed", str(exc))
          self.store.save_metadata(metadata)
          self.condition.notify_all()
    finally:
      self.active_runs.discard(run_id)
      self.stop_requested.discard(run_id)

  def record_runner_event(self, metadata: dict, run: dict, event: dict) -> None:
    event_type = str(event.get("type") or "progress")
    message = str(event.get("message") or event_type)
    if "tokens" in event:
      delta = max(0, int(event.get("tokens") or 0) - int(run.get("tokens") or 0))
      run["tokens"] = max(int(run.get("tokens") or 0), int(event.get("tokens") or 0))
      session = metadata["chatSessions"][run["sessionId"]]
      session["totalTokens"] = int(session.get("totalTokens") or 0) + delta
      session["tokens"] = f"{session['totalTokens']:,}"
      user = self.users.get(run["user"])
      if user:
        user["usedTokens"] = int(user.get("usedTokens") or 0) + delta
    if event.get("codexSessionId"):
      session = metadata["chatSessions"][run["sessionId"]]
      session["codexSessionId"] = event["codexSessionId"]
      session["codexNativeResumable"] = True
      run["codexSessionId"] = event["codexSessionId"]
    self.append_event(metadata, run["sessionId"], event_type, message, run["id"], persist=False, raw=event.get("raw"))
    run["updated"] = now_string()
    metadata["chatSessions"][run["sessionId"]]["updated"] = run["updated"]

  def finalize_run(self, metadata: dict, run: dict, status: str, error: str | None) -> None:
    workspace = metadata["workspaces"].get(run["workspaceId"])
    if not workspace:
      return
    if error:
      self.append_event(metadata, run["sessionId"], "error", error, run["id"], persist=False)
    try:
      snapshot = self.store.refresh_workspace_metadata(metadata, workspace, status, run["id"])
      run["resultSnapshotId"] = snapshot["id"]
    except Exception as exc:
      self.append_event(metadata, run["sessionId"], "error", f"Checkpoint failed: {exc}", run["id"], persist=False)
      if status == "completed":
        status = "failed"
    run["status"] = status
    run["updated"] = now_string()
    workspace["locked"] = False
    workspace["activeRunId"] = None
    session = metadata["chatSessions"][run["sessionId"]]
    session["status"] = status
    session["updated"] = run["updated"]
    if status in {"completed", "stopped"}:
      session["codexNativeResumable"] = True
    self.append_event(metadata, run["sessionId"], status, f"Run {status}.", run["id"], persist=False)

  def append_event(
    self,
    metadata: dict,
    session_id: str,
    event_type: str,
    message: str,
    run_id: str | None,
    persist: bool = True,
    raw: dict | None = None,
  ) -> dict:
    events = metadata.setdefault("events", {}).setdefault(session_id, [])
    event = {
      "id": len(events) + 1,
      "time": now_string(),
      "type": event_type,
      "message": message,
      "runId": run_id,
    }
    if raw is not None:
      event["raw"] = raw
    events.append(event)
    if persist:
      self.store.save_metadata(metadata)
      self.condition.notify_all()
    return event

  def public_session(self, metadata: dict, session_id: str) -> dict:
    session = metadata["chatSessions"].get(session_id)
    if not session:
      return {"id": session_id, "title": "Unknown session", "status": "failed", "updated": now_string(), "tokens": "0", "events": []}
    events = metadata.get("events", {}).get(session_id, [])
    return {
      "id": session["id"],
      "title": session["title"],
      "status": session["status"],
      "updated": session["updated"],
      "tokens": session.get("tokens", "0"),
      "latestRunId": session.get("latestRunId"),
      "codexNativeResumable": bool(session.get("codexNativeResumable")),
      "events": [[event["type"], event["message"], event["message"]] for event in events],
    }

  def session_title(self, prompt: str) -> str:
    compact = " ".join(prompt.split())
    return compact[:48] or "Audit task"

  def user_codex_settings(self, user: dict) -> dict:
    settings = user.get("codex") or {}
    base_url = str(settings.get("baseUrl") or os.environ.get("AI_AUDIT_DEFAULT_CODEX_BASE_URL") or "https://api.openai.com/v1").strip()
    api_key = str(settings.get("apiKey") or "").strip()
    if not api_key:
      raise StorageError("codex_auth_required", "configure Codex API key before starting a run")
    return {"baseUrl": base_url, "apiKey": api_key}


def safe_segment(value: str) -> str:
  safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)
  return safe.strip("._") or "default"


def toml_string(value: str) -> str:
  return value.replace("\\", "\\\\").replace('"', '\\"')
