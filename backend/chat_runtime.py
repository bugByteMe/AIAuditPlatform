from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable

from chat_store import ChatStore
from config import SETTINGS
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
    self.image = SETTINGS.codex_image
    self.codex_root = SETTINGS.codex_root
    self.codex_home_root = SETTINGS.codex_home_root
    self.skill_path = SETTINGS.skill_path
    self.container_uid = SETTINGS.container_uid
    self.container_gid = SETTINGS.container_gid
    self.timeout_seconds = SETTINGS.run_timeout_seconds
    self.run_network = SETTINGS.run_network
    self.run_cpus = SETTINGS.run_cpus
    self.run_memory = SETTINGS.run_memory
    self.process_wait_timeout_seconds = SETTINGS.process_wait_timeout_seconds
    self.docker_stop_grace_seconds = SETTINGS.docker_stop_grace_seconds
    self.docker_stop_timeout_seconds = SETTINGS.docker_stop_timeout_seconds
    self.processes: dict[str, subprocess.Popen] = {}
    self.lock = threading.Lock()

  def start(self, run: dict, workspace_path: Path) -> Iterable[dict]:
    container_name = f"ai-audit-{run['id']}"
    run["container"] = container_name
    codex_home = self.prepare_codex_home(run)
    self.make_tree_writable_for_container(workspace_path)
    command = [
      "docker",
      "run",
      "--rm",
      "--name",
      container_name,
      "--network",
      self.run_network,
      "--cpus",
      self.run_cpus,
      "--memory",
      self.run_memory,
      "--user",
      f"{self.container_uid}:{self.container_gid}",
      "-v",
      f"{workspace_path.resolve()}:/workspace",
      "-v",
      f"{codex_home.resolve()}:/home/codex/.codex",
      "-w",
      "/workspace",
    ]
    command.extend([self.image, *self.codex_command_args(run)])
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
      returncode = process.wait(timeout=self.process_wait_timeout_seconds)
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
    item_event = self.parse_item_event(payload)
    if item_event:
      return item_event
    command_event = self.parse_command_event(payload)
    if command_event:
      return command_event
    websearch_event = self.parse_websearch_event(payload)
    if websearch_event:
      return websearch_event
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

  def parse_item_event(self, payload: dict) -> dict | None:
    item = payload.get("item")
    if not isinstance(item, dict):
      return None
    text = self.event_text(item)
    item_type = str(item.get("type") or payload.get("type") or "progress")
    event_type = self.normalize_event_type(item_type)
    if not text and event_type not in {"command", "tool", "websearch"}:
      return None
    if not text:
      text = {"command": "Command executing.", "websearch": "Web search."}.get(event_type, "Tool executing.")
    result = {"type": event_type, "message": text, "raw": payload}
    self.add_tool_status(result, payload)
    native_id = self.extract_codex_session_id(payload)
    if native_id:
      result["codexSessionId"] = native_id
    return result

  def parse_command_event(self, payload: dict) -> dict | None:
    event_type = self.normalize_event_type(str(payload.get("type") or payload.get("event") or ""))
    if event_type != "command":
      return None
    text = self.event_text(payload) or "Command execution"
    result = {"type": "command", "message": text, "raw": payload}
    self.add_tool_status(result, payload)
    native_id = self.extract_codex_session_id(payload)
    if native_id:
      result["codexSessionId"] = native_id
    return result

  def parse_websearch_event(self, payload: dict) -> dict | None:
    event_type = self.normalize_event_type(str(payload.get("type") or payload.get("event") or ""))
    if event_type != "websearch":
      return None
    text = self.event_text(payload) or "Web search."
    result = {"type": "websearch", "message": text, "raw": payload}
    self.add_tool_status(result, payload)
    native_id = self.extract_codex_session_id(payload)
    if native_id:
      result["codexSessionId"] = native_id
    return result

  def normalize_event_type(self, event_type: str) -> str:
    return {
      "agent_message": "assistant",
      "assistant_message": "assistant",
      "command_execution": "command",
      "command_output": "command",
      "exec_command": "command",
      "exec_command_begin": "command",
      "exec_command_output": "command",
      "exec_command_end": "command",
      "web_search": "websearch",
      "web_search_call": "websearch",
      "web_search_result": "websearch",
      "web_search_results": "websearch",
      "websearch": "websearch",
      "tool_call": "tool",
      "tool_output": "tool",
      "tool_result": "tool",
      "reasoning": "progress",
      "agent_reasoning": "progress",
    }.get(event_type, event_type)

  def event_text(self, payload: dict) -> str:
    for key in ["text", "message", "summary", "query", "url", "title", "command", "cmd", "output", "result", "content"]:
      value = payload.get(key)
      if isinstance(value, str) and value:
        return value
      if isinstance(value, list) and value:
        if all(isinstance(item, str) for item in value):
          return " ".join(value)
        return json.dumps(value, ensure_ascii=False)
      if isinstance(value, dict) and value:
        return json.dumps(value, ensure_ascii=False)
    return ""

  def add_tool_status(self, result: dict, payload: dict) -> None:
    call_id = self.extract_tool_call_id(payload)
    if call_id:
      result["toolCallId"] = call_id
    event_type = str(payload.get("type") or payload.get("event") or "")
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    item_type = str(item.get("type") or "")
    combined = f"{event_type} {item_type}".lower()
    if any(marker in combined for marker in ["completed", "complete", "output", "result", "end"]):
      result["status"] = "completed"
    elif any(marker in combined for marker in ["started", "start", "begin", "call", "command"]):
      result["status"] = "executing"

  def extract_tool_call_id(self, payload: dict) -> str | None:
    keys = ["tool_call_id", "toolCallId", "call_id", "callId", "id"]
    item = payload.get("item")
    if isinstance(item, dict):
      for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value:
          return value
    for key in keys:
      value = payload.get(key)
      if isinstance(value, str) and value:
        return value
    return None

  def prepare_codex_home(self, run: dict) -> Path:
    settings = run.get("codexSettings") or {}
    api_key = str(settings.get("apiKey") or "").strip()
    base_url = str(settings.get("baseUrl") or "").strip()
    if not api_key:
      raise RunnerError("Codex API key is not configured for this user.")
    codex_home = self.codex_home_root / safe_segment(run["user"]) / safe_segment(run["sessionId"])
    codex_home.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(codex_home / "tmp", ignore_errors=True)
    os.chmod(codex_home, 0o700)
    self.copy_skills(codex_home)
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
    self.make_tree_writable_for_container(codex_home)
    return codex_home

  def copy_skills(self, codex_home: Path) -> None:
    if not self.skill_path or not self.skill_path.exists():
      return
    destination = codex_home / "skills"
    if destination.exists():
      shutil.rmtree(destination)
    shutil.copytree(self.skill_path, destination)

  def make_tree_writable_for_container(self, root: Path) -> None:
    for path in [root, *root.rglob("*")]:
      if hasattr(os, "chown"):
        try:
          os.chown(path, self.container_uid, self.container_gid)
        except OSError as exc:
          if getattr(os, "geteuid", lambda: -1)() == 0:
            raise RunnerError(f"Failed to set container ownership for {path}") from exc
      if path.is_dir():
        path.chmod(0o700)
      elif path.is_file():
        path.chmod(0o600)

  def codex_command_args(self, run: dict) -> list[str]:
    base = ["codex", "--ask-for-approval", "never", "--sandbox", "danger-full-access", "exec"]
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
      subprocess.run(
        ["docker", "stop", "--time", str(self.docker_stop_grace_seconds), container],
        capture_output=True,
        timeout=self.docker_stop_timeout_seconds,
        check=False,
      )
    with self.lock:
      process = self.processes.get(run["id"])
    if process and process.poll() is None:
      process.terminate()


class ChatRuntime:
  def __init__(
    self,
    store: WorkspaceStore,
    users: dict[str, dict],
    runner: CodexRunner | None = None,
    capacity: int = 1,
    chat_store: ChatStore | None = None,
    save_users=None,
  ):
    self.store = store
    self.users = users
    self.runner = runner or DockerCodexRunner()
    self.chat_store = chat_store or ChatStore(store.root / "chat")
    self.save_users = save_users
    self.capacity = max(1, capacity)
    self.scheduler_poll_seconds = max(0.01, SETTINGS.scheduler_poll_seconds)
    self.lock = threading.RLock()
    self.condition = threading.Condition(self.lock)
    self.queue: queue.Queue[str] = queue.Queue()
    self.active_runs: set[str] = set()
    self.stop_requested: set[str] = set()
    self.shutdown = False
    self.store.set_chat_session_provider(self.chat_store.public_workspace_sessions)
    self.migrate_legacy_chat_metadata()
    self.scheduler = threading.Thread(target=self.scheduler_loop, name="ai-audit-chat-scheduler", daemon=True)
    self.scheduler.start()

  def migrate_legacy_chat_metadata(self) -> None:
    metadata = self.store.load_metadata()
    self.ensure_chat_metadata(metadata)
    if self.chat_store.migrate_from_metadata(metadata):
      self.store.save_metadata(metadata)

  def ensure_chat_metadata(self, metadata: dict) -> None:
    for workspace in metadata.get("workspaces", {}).values():
      workspace.setdefault("sessions", [])

  def list_sessions(self, workspace_id: str, user: dict) -> list[dict]:
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      workspace = self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      return [self.public_session(session["id"]) for session in workspace.get("sessions", []) if self.chat_store.get_session(session["id"])]

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
      self.chat_store.save_session(session)
      workspace.setdefault("sessions", []).insert(0, {"id": session["id"]})
      workspace["updated"] = timestamp
      self.store.save_metadata(metadata)
      return self.public_session(session["id"])

  def start_run(self, workspace_id: str, user: dict, payload: dict) -> dict:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
      raise StorageError("bad_request", "prompt is required")
    if int(user.get("usedTokens") or 0) >= int(user.get("budgetTokens") or 0):
      raise StorageError("budget_exhausted", "user token budget is exhausted")
    self.user_codex_settings(user)
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      workspace = self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      if workspace.get("locked"):
        raise StorageError("workspace_locked", "workspace has an active write lock")
      session_id = str(payload.get("sessionId") or "")
      if session_id and not self.chat_store.get_session(session_id):
        session_id = ""
      if not session_id:
        session = self._create_session_in_metadata(metadata, workspace, user, self.session_title(prompt))
      else:
        session = self.chat_store.get_session(session_id)
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
        "codexResume": bool(session.get("codexNativeResumable")),
        "codexSessionId": session.get("codexSessionId"),
      }
      self.chat_store.save_run(run)
      workspace["locked"] = True
      workspace["activeRunId"] = run["id"]
      session["status"] = "queued"
      session["latestRunId"] = run["id"]
      session["updated"] = timestamp
      self.chat_store.save_session(session)
      self.append_event(session["id"], "user", prompt, run["id"])
      self.append_event(session["id"], "queued", "Run queued. Waiting for local Docker capacity.", run["id"])
      self.store.save_metadata(metadata)
      self.queue.put(run["id"])
      self.condition.notify_all()
      return {"session": self.public_session(session["id"]), "run": run}

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
    self.chat_store.save_session(session)
    workspace.setdefault("sessions", []).insert(0, {"id": session["id"]})
    return session

  def stop_run(self, workspace_id: str, run_id: str, user: dict) -> dict:
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      run = self.chat_store.get_run(run_id)
      if not run or run["workspaceId"] != workspace_id:
        raise StorageError("not_found", "run not found")
      if run["status"] in TERMINAL_STATES:
        return {"run": run}
      run["status"] = "stopping"
      run["updated"] = now_string()
      self.stop_requested.add(run_id)
      self.append_event(run["sessionId"], "stopping", "Stop requested. Creating a resumable checkpoint.", run_id)
      self.chat_store.save_run(run)
      self.condition.notify_all()
    self.runner.stop(run)
    return {"run": run}

  def events(self, workspace_id: str, session_id: str, after: int, user: dict) -> list[dict]:
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      session = self.chat_store.get_session(session_id)
      if not session or session["workspaceId"] != workspace_id:
        raise StorageError("not_found", "chat session not found")
      return [event for event in self.chat_store.events(session_id) if int(event["id"]) > after]

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
        time.sleep(self.scheduler_poll_seconds)
      self.active_runs.add(run_id)
      threading.Thread(target=self.execute_run, args=(run_id,), name=f"ai-audit-run-{run_id}", daemon=True).start()

  def execute_run(self, run_id: str) -> None:
    try:
      with self.lock:
        metadata = self.store.load_metadata()
        self.ensure_chat_metadata(metadata)
        run = self.chat_store.get_run(run_id)
        if not run:
          return
        run["status"] = "starting"
        run["updated"] = now_string()
        session = self.chat_store.get_session(run["sessionId"])
        session["status"] = "running"
        session["updated"] = run["updated"]
        self.append_event(run["sessionId"], "starting", "Starting Codex container.", run_id)
        self.chat_store.save_session(session)
        self.chat_store.save_run(run)
        self.store.save_metadata(metadata)
        self.condition.notify_all()
      workspace_path = self.store.workspace_path(run["workspaceId"])
      run_for_worker = {**run, "codexSettings": self.user_codex_settings(self.users[run["user"]])}
      for event in self.runner.start(run_for_worker, workspace_path):
        with self.lock:
          metadata = self.store.load_metadata()
          self.ensure_chat_metadata(metadata)
          stored_run = self.chat_store.get_run(run_id)
          if not stored_run:
            return
          if run_id in self.stop_requested:
            stored_run["status"] = "stopping"
            self.chat_store.save_run(stored_run)
            break
          stored_run["status"] = "running"
          stored_run["container"] = run_for_worker.get("container", stored_run.get("container", ""))
          self.record_runner_event(stored_run, event)
          self.chat_store.save_run(stored_run)
          self.condition.notify_all()
      with self.lock:
        metadata = self.store.load_metadata()
        self.ensure_chat_metadata(metadata)
        run = self.chat_store.get_run(run_id)
        if run:
          final_status = "stopped" if run_id in self.stop_requested or run["status"] == "stopping" else "completed"
          self.finalize_run(metadata, run, final_status, None)
          self.chat_store.save_run(run)
          self.store.save_metadata(metadata)
          self.condition.notify_all()
    except Exception as exc:
      with self.lock:
        metadata = self.store.load_metadata()
        self.ensure_chat_metadata(metadata)
        run = self.chat_store.get_run(run_id)
        if run:
          self.finalize_run(metadata, run, "failed", str(exc))
          self.chat_store.save_run(run)
          self.store.save_metadata(metadata)
          self.condition.notify_all()
    finally:
      self.active_runs.discard(run_id)
      self.stop_requested.discard(run_id)

  def record_runner_event(self, run: dict, event: dict) -> None:
    event_type = str(event.get("type") or "progress")
    message = str(event.get("message") or event_type)
    if "tokens" in event:
      delta = max(0, int(event.get("tokens") or 0) - int(run.get("tokens") or 0))
      run["tokens"] = max(int(run.get("tokens") or 0), int(event.get("tokens") or 0))
      session = self.chat_store.get_session(run["sessionId"])
      session["totalTokens"] = int(session.get("totalTokens") or 0) + delta
      session["tokens"] = f"{session['totalTokens']:,}"
      self.chat_store.save_session(session)
      user = self.users.get(run["user"])
      if user:
        user["usedTokens"] = int(user.get("usedTokens") or 0) + delta
        if self.save_users:
          self.save_users()
    if event.get("codexSessionId"):
      session = self.chat_store.get_session(run["sessionId"])
      session["codexSessionId"] = event["codexSessionId"]
      session["codexNativeResumable"] = True
      run["codexSessionId"] = event["codexSessionId"]
      self.chat_store.save_session(session)
    self.append_event(
      run["sessionId"],
      event_type,
      message,
      run["id"],
      raw=event.get("raw"),
      status=event.get("status"),
      tool_call_id=event.get("toolCallId"),
    )
    run["updated"] = now_string()
    session = self.chat_store.get_session(run["sessionId"])
    session["updated"] = run["updated"]
    self.chat_store.save_session(session)

  def finalize_run(self, metadata: dict, run: dict, status: str, error: str | None) -> None:
    workspace = metadata["workspaces"].get(run["workspaceId"])
    if not workspace:
      return
    if error:
      self.append_event(run["sessionId"], "error", error, run["id"])
    try:
      snapshot = self.store.refresh_workspace_metadata(metadata, workspace, status, run["id"])
      run["resultSnapshotId"] = snapshot["id"]
    except Exception as exc:
      self.append_event(run["sessionId"], "error", f"Checkpoint failed: {exc}", run["id"])
      if status == "completed":
        status = "failed"
    run["status"] = status
    run["updated"] = now_string()
    workspace["locked"] = False
    workspace["activeRunId"] = None
    session = self.chat_store.get_session(run["sessionId"])
    session["status"] = status
    session["updated"] = run["updated"]
    if status in {"completed", "stopped"}:
      session["codexNativeResumable"] = True
    self.chat_store.save_session(session)
    self.append_event(run["sessionId"], status, f"Run {status}.", run["id"])

  def append_event(
    self,
    session_id: str,
    event_type: str,
    message: str,
    run_id: str | None,
    raw: dict | None = None,
    status: str | None = None,
    tool_call_id: str | None = None,
  ) -> dict:
    event = {
      "time": now_string(),
      "type": event_type,
      "message": message,
      "runId": run_id,
    }
    if raw is not None:
      event["raw"] = raw
    if status:
      event["status"] = status
    if tool_call_id:
      event["toolCallId"] = tool_call_id
    return self.chat_store.append_event(session_id, event)

  def public_session(self, session_id: str) -> dict:
    return self.chat_store.public_session(session_id)

  def session_title(self, prompt: str) -> str:
    compact = " ".join(prompt.split())
    return compact[:48] or "Audit task"

  def user_codex_settings(self, user: dict) -> dict:
    settings = user.get("codex") or {}
    base_url = str(settings.get("baseUrl") or SETTINGS.default_codex_base_url).strip()
    api_key = str(settings.get("apiKey") or "").strip()
    if not api_key:
      raise StorageError("codex_auth_required", "configure Codex API key before starting a run")
    return {"baseUrl": base_url, "apiKey": api_key}


def safe_segment(value: str) -> str:
  safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)
  return safe.strip("._") or "default"


def toml_string(value: str) -> str:
  return value.replace("\\", "\\\\").replace('"', '\\"')
