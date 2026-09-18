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
from compute_nodes import WorkerRegistry, WorkerUnavailable
from config import SETTINGS, parse_memory_bytes
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
    codex_home = self.prepare_codex_home(run)
    yield from self.start_prepared(run, workspace_path, codex_home)

  def start_prepared(self, run: dict, workspace_path: Path, codex_home: Path) -> Iterable[dict]:
    container_name = f"ai-audit-{run['id']}"
    run["container"] = container_name
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
      str(run.get("requestedCpu") or self.run_cpus),
      "--memory",
      str(run.get("requestedMemoryBytes") or self.run_memory),
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
    if run.get("status") == "stopping":
      self.stop(run)
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
    usage_event = self.parse_usage_event(payload)
    if usage_event:
      return usage_event
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

  def parse_usage_event(self, payload: dict) -> dict | None:
    event_type = str(payload.get("type") or payload.get("event") or "")
    usage = payload.get("usage")
    if not isinstance(usage, dict):
      return None
    if event_type not in {"turn.completed", "turn.complete"} and not any(
      key in usage for key in ["total_tokens", "totalTokens"]
    ):
      return None
    input_tokens = int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
    cached_input_tokens = int(usage.get("cached_input_tokens") or usage.get("cachedInputTokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("outputTokens") or 0)
    total_tokens = usage.get("total_tokens") or usage.get("totalTokens")
    total = int(total_tokens) if total_tokens is not None else input_tokens + output_tokens
    result = {
      "type": "usage",
      "message": "Token usage updated.",
      "tokens": max(0, total),
      "inputTokens": max(0, input_tokens),
      "cachedInputTokens": max(0, cached_input_tokens),
      "outputTokens": max(0, output_tokens),
      "raw": payload,
    }
    native_id = self.extract_codex_session_id(payload)
    if native_id:
      result["codexSessionId"] = native_id
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

  def session_home(self, username: str, session_id: str) -> Path:
    return self.codex_home_root / safe_segment(username) / safe_segment(session_id)

  def fork_base_home(self, run_id: str) -> Path:
    return self.codex_root / "fork_bases" / safe_segment(run_id)

  def clone_conversation_state(self, source: Path, destination: Path, conversation_id: str | None = None) -> bool:
    """Copy only the requested Codex rollout, without credentials or shared state."""
    if not source.is_dir() or not conversation_id:
      return False
    candidates = []
    for directory_name in ("sessions", "archived_sessions"):
      directory = source / directory_name
      if directory.is_dir():
        candidates.extend(path for path in directory.rglob("*.jsonl") if conversation_id in path.name)
    if not candidates:
      return False
    rollout = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
      shutil.rmtree(destination)
    destination.mkdir(parents=True)
    target = destination / rollout.relative_to(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rollout, target)
    return True

  def capture_fork_base(self, run: dict) -> Path:
    source = self.session_home(str(run["user"]), str(run["sessionId"]))
    destination = self.fork_base_home(str(run["id"]))
    self.clone_conversation_state(source, destination, str(run.get("codexSessionId") or "") or None)
    return destination

  def remove_fork_base(self, run_id: str) -> None:
    path = self.fork_base_home(run_id)
    if path.exists():
      shutil.rmtree(path)

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
    if run.get("codexFork"):
      return [
        *base,
        "fork",
        "--json",
        "--skip-git-repo-check",
        "-m",
        run["model"],
        run["codexSessionId"],
        run["prompt"],
      ]
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
    worker_registry: WorkerRegistry | None = None,
    groups: dict[str, dict] | None = None,
  ):
    self.store = store
    self.users = users
    self.groups = groups if groups is not None else {}
    self.runner = runner or DockerCodexRunner()
    self.chat_store = chat_store or ChatStore(store.root / "chat")
    self.save_users = save_users
    self.capacity = max(1, capacity)
    self.requested_cpu = float(SETTINGS.run_cpus)
    self.requested_memory_bytes = parse_memory_bytes(SETTINGS.run_memory)
    self.worker_registry = worker_registry or (WorkerRegistry(SETTINGS) if SETTINGS.compute_nodes else None)
    self.codex_preparer = self.runner if isinstance(self.runner, DockerCodexRunner) else DockerCodexRunner()
    if runner is not None and not isinstance(runner, DockerCodexRunner):
      self.codex_preparer.codex_root = store.root / "codex"
      self.codex_preparer.codex_home_root = store.root / "codex" / "homes"
    self.scheduler_poll_seconds = max(0.01, SETTINGS.scheduler_poll_seconds)
    # Serialize chat lifecycle metadata changes with workspace/upload mutations.
    self.lock = store.lock
    self.condition = threading.Condition(self.lock)
    self.queue: queue.Queue[str] = queue.Queue()
    self.active_runs: set[str] = set()
    self.stop_requested: set[str] = set()
    self.shutdown = False
    self.store.set_chat_session_provider(self.public_workspace_sessions)
    self.recover_persisted_runs()
    self.scheduler = threading.Thread(target=self.scheduler_loop, name="ai-audit-chat-scheduler", daemon=True)
    self.scheduler.start()

  def public_workspace_sessions(self, workspace: dict) -> list[dict]:
    return [self.public_session(item["id"]) for item in workspace.get("sessions", []) if self.chat_store.get_session(item.get("id"))]

  def recover_persisted_runs(self) -> None:
    for run in self.chat_store.runs().values():
      status = str(run.get("status") or "")
      if status == "queued":
        self.queue.put(str(run["id"]))
        continue
      if status not in RUNNING_STATES:
        continue
      node_id = str(run.get("workerId") or "")
      if self.worker_registry and node_id in self.worker_registry.nodes:
        self.worker_registry.reserve(node_id, str(run["id"]), float(run.get("requestedCpu") or self.requested_cpu), int(run.get("requestedMemoryBytes") or self.requested_memory_bytes))
        self.active_runs.add(str(run["id"]))
        threading.Thread(target=self.execute_run, args=(str(run["id"]), node_id), name=f"ai-audit-recover-{run['id']}", daemon=True).start()
        continue
      with self.lock:
        metadata = self.store.load_metadata()
        stored = self.chat_store.get_run(str(run["id"]))
        if stored:
          self.finalize_run(metadata, stored, "failed", "control plane restarted before the local run completed")
          self.chat_store.save_run(stored)
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

  def fork_session(self, workspace_id: str, session_id: str, user: dict, title: str | None = None) -> dict:
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      workspace = self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      session_refs = workspace.get("sessions", [])
      source_index = next((index for index, item in enumerate(session_refs) if str(item.get("id") or "") == session_id), -1)
      source = self.chat_store.get_session(session_id)
      if source_index < 0 or not source or source.get("workspaceId") != workspace_id:
        raise StorageError("not_found", "chat session not found")

      active_run = next(
        (
          run
          for run in self.chat_store.runs().values()
          if run.get("sessionId") == session_id and run.get("status") in RUNNING_STATES
        ),
        None,
      )
      source_native_id = str(source.get("codexSessionId") or "")
      stable_events = self.chat_store.events(session_id)
      if active_run:
        stable_events = [event for event in stable_events if event.get("runId") != active_run.get("id")]
      has_conversation = any(event.get("type") in {"user", "assistant"} for event in stable_events)
      if not has_conversation:
        source_native_id = ""
      if has_conversation and not source_native_id:
        raise StorageError("session_not_forkable", "chat session does not have resumable Codex context")

      timestamp = now_string()
      fork_id = generated_id("chat")
      clean_title = str(title or "").strip() or f'{source.get("title") or "Audit task"} Copy'
      fork = {
        "id": fork_id,
        "workspaceId": workspace_id,
        "title": clean_title,
        "status": "stopped",
        "updated": timestamp,
        "tokens": "0",
        "totalTokens": 0,
        "latestRunId": None,
        "createdBy": user["username"],
        "created": timestamp,
        "forkedFromSessionId": session_id,
        "codexForkPending": bool(source_native_id),
        "codexForkSourceId": source_native_id or None,
        "codexHomeUser": user["username"],
      }

      source_user = str(source.get("codexHomeUser") or "")
      if not source_user and source.get("latestRunId"):
        latest_run = self.chat_store.get_run(str(source["latestRunId"]))
        source_user = str((latest_run or {}).get("user") or "")
      source_user = source_user or str(source.get("createdBy") or user["username"])
      source_home = (
        self.codex_preparer.fork_base_home(str(active_run["id"]))
        if active_run
        else self.codex_preparer.session_home(source_user, session_id)
      )
      destination_home = self.codex_preparer.session_home(str(user["username"]), fork_id)
      copied = self.codex_preparer.clone_conversation_state(source_home, destination_home, source_native_id or None)
      if source_native_id and not copied:
        raise StorageError("session_not_forkable", "chat session does not have an available Codex checkpoint")

      self.chat_store.save_session(fork)
      self.chat_store.replace_events(fork_id, stable_events)
      session_refs.insert(source_index + 1, {"id": fork_id})
      workspace["updated"] = timestamp
      self.store.save_metadata(metadata)
      return self.public_session(fork_id, include_events=False)

  def update_session(self, workspace_id: str, session_id: str, user: dict, title: str) -> dict:
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      workspace = self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      if not any(str(item.get("id") or "") == session_id for item in workspace.get("sessions", [])):
        raise StorageError("not_found", "chat session not found")
      session = self.chat_store.get_session(session_id)
      if not session or session.get("workspaceId") != workspace_id:
        raise StorageError("not_found", "chat session not found")
      clean_title = str(title or "").strip()
      if not clean_title:
        raise StorageError("bad_request", "chat session title cannot be empty")
      session["title"] = clean_title
      session["updated"] = now_string()
      self.chat_store.save_session(session)
      return self.public_session(session_id)

  def delete_session(self, workspace_id: str, session_id: str, user: dict) -> dict:
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      workspace = self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      if workspace.get("owner") != user.get("username") and user.get("role") != "system_admin":
        raise StorageError("forbidden", "only the workspace owner or system admin can delete a chat session")
      session_refs = workspace.get("sessions", [])
      if not any(str(item.get("id") or "") == session_id for item in session_refs):
        raise StorageError("not_found", "chat session not found")
      session = self.chat_store.get_session(session_id)
      if not session or session.get("workspaceId") != workspace_id:
        raise StorageError("not_found", "chat session not found")
      session_runs = {
        run_id: run
        for run_id, run in self.chat_store.runs().items()
        if run.get("sessionId") == session_id
      }
      if any(run.get("status") in RUNNING_STATES for run in session_runs.values()):
        raise StorageError("session_run_active", "stop the active chat run before deleting this session")

      workspace["sessions"] = [item for item in session_refs if str(item.get("id") or "") != session_id]
      if workspace.get("activeRunId") in session_runs:
        workspace["activeRunId"] = None
      workspace["updated"] = now_string()
      self.store.save_metadata(metadata)
      removed = self.chat_store.delete_session(session_id)
      if not removed:
        raise StorageError("not_found", "chat session not found")

      usernames = {
        str(candidate or "")
        for candidate in [
          removed["session"].get("createdBy"),
          removed["session"].get("codexHomeUser"),
          *(run.get("user") for run in removed["runs"].values()),
        ]
        if candidate
      }
      for username in usernames:
        shutil.rmtree(self.codex_preparer.session_home(username, session_id), ignore_errors=True)
      for run_id in removed["runs"]:
        self.codex_preparer.remove_fork_base(str(run_id))
      return {"id": session_id, "runIds": sorted(removed["runs"])}

  def start_run(self, workspace_id: str, user: dict, payload: dict) -> dict:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
      raise StorageError("bad_request", "prompt is required")
    if int(user.get("usedTokens") or 0) >= int(user.get("budgetTokens") or 0):
      raise StorageError("budget_exhausted", "user token budget is exhausted")
    self.user_codex_settings(user)
    if self.worker_registry and not self.worker_registry.compatible(self.requested_cpu, self.requested_memory_bytes):
      raise StorageError("no_compatible_worker", "no configured compute node can satisfy the run resources")
    with self.lock:
      metadata = self.store.load_metadata()
      self.ensure_chat_metadata(metadata)
      workspace = self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      quota_owner = self.store.workspace_quota_owner(workspace, user)
      self.store.assert_group_quota(quota_owner, metadata=metadata, require_available=True)
      session_id = str(payload.get("sessionId") or "")
      if session_id and not self.chat_store.get_session(session_id):
        session_id = ""
      active_runs = [
        run
        for run in self.chat_store.runs().values()
        if run.get("workspaceId") == workspace_id and run.get("status") in RUNNING_STATES
      ]
      group_id = str(user.get("groupId") or "")
      group = self.groups.get(group_id) if group_id else None
      if group:
        live_run_limit = int(group.get("liveRunLimit", 1))
        group_live_runs = [
          run
          for run in self.chat_store.runs().values()
          if run.get("status") in RUNNING_STATES and self.run_group_id(run) == group_id
        ]
        if len(group_live_runs) >= live_run_limit:
          raise StorageError(
            "group_live_run_limit_reached",
            f"group concurrent live run limit ({live_run_limit}) is reached",
          )
      if session_id and any(run.get("sessionId") == session_id for run in active_runs):
        raise StorageError("session_run_active", "this chat session already has an active run")
      if workspace.get("activeUploadId") or (workspace.get("locked") and not active_runs):
        raise StorageError("workspace_locked", "workspace has an active non-chat write lock")
      if active_runs and workspace.get("runLockEnabled"):
        raise StorageError("workspace_locked", "workspace exclusive run lock is enabled")
      if active_runs and not bool(payload.get("confirmConcurrent")):
        raise StorageError(
          "concurrent_confirmation_required",
          f"{len(active_runs)} other workspace run(s) are active; confirmation is required",
        )
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
        "groupId": group_id,
        "status": "queued",
        "prompt": prompt,
        "model": str(payload.get("model") or "gpt-5.6-sol"),
        "reasoning": str(payload.get("reasoning") or "high"),
        "worker": "queued",
        "workerId": None,
        "requestedCpu": self.requested_cpu,
        "requestedMemoryBytes": self.requested_memory_bytes,
        "workerEventCursor": 0,
        "container": "",
        "created": timestamp,
        "updated": timestamp,
        "baseSnapshotId": workspace.get("latestSnapshotId"),
        "resultSnapshotId": None,
        "tokens": 0,
        "inputTokens": 0,
        "cachedInputTokens": 0,
        "outputTokens": 0,
        "codexFork": bool(session.get("codexForkPending") and session.get("codexForkSourceId")),
        "codexResume": bool(session.get("codexNativeResumable") and not session.get("codexForkPending")),
        "codexSessionId": session.get("codexForkSourceId") or session.get("codexSessionId"),
      }
      self.codex_preparer.capture_fork_base(run)
      self.chat_store.save_run(run)
      workspace["locked"] = True
      workspace["activeRunId"] = workspace.get("activeRunId") or run["id"]
      session["status"] = "queued"
      session["latestRunId"] = run["id"]
      session["updated"] = timestamp
      session["codexHomeUser"] = user["username"]
      self.chat_store.save_session(session)
      self.append_event(session["id"], "user", prompt, run["id"])
      queue_message = "Run queued. Waiting for compute capacity." if self.worker_registry else "Run queued. Waiting for local Docker capacity."
      self.append_event(session["id"], "queued", queue_message, run["id"])
      self.store.save_metadata(metadata)
      self.queue.put(run["id"])
      self.condition.notify_all()
      return {"session": self.public_session(session["id"]), "run": run}

  def run_group_id(self, run: dict) -> str:
    stored_group_id = str(run.get("groupId") or "")
    if stored_group_id:
      return stored_group_id
    run_user = self.users.get(str(run.get("user") or "")) or {}
    return str(run_user.get("groupId") or "")

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
      was_queued = run["status"] == "queued"
      run["status"] = "stopping"
      run["updated"] = now_string()
      self.stop_requested.add(run_id)
      self.append_event(run["sessionId"], "stopping", "Stop requested. Creating a resumable checkpoint.", run_id)
      self.chat_store.save_run(run)
      if was_queued:
        self.finalize_run(metadata, run, "stopped", None)
        self.chat_store.save_run(run)
        self.store.save_metadata(metadata)
      self.condition.notify_all()
    if not was_queued:
      if self.worker_registry and run.get("workerId"):
        self.worker_registry.client(str(run["workerId"])).stop(str(run["id"]))
      else:
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
      node_id = None
      if self.worker_registry:
        run = self.chat_store.get_run(run_id)
        if not run or run.get("status") != "queued":
          continue
        node_id = self.worker_registry.claim(run_id, float(run["requestedCpu"]), int(run["requestedMemoryBytes"]))
        if not node_id:
          self.queue.put(run_id)
          time.sleep(max(self.scheduler_poll_seconds, 0.1))
          continue
        run["workerId"] = node_id
        run["worker"] = node_id
        self.chat_store.save_run(run)
      else:
        while len(self.active_runs) >= self.capacity:
          time.sleep(self.scheduler_poll_seconds)
      self.active_runs.add(run_id)
      threading.Thread(target=self.execute_run, args=(run_id, node_id), name=f"ai-audit-run-{run_id}", daemon=True).start()

  def execute_run(self, run_id: str, node_id: str | None = None) -> None:
    try:
      with self.lock:
        metadata = self.store.load_metadata()
        self.ensure_chat_metadata(metadata)
        run = self.chat_store.get_run(run_id)
        if not run:
          return
        if run.get("status") in TERMINAL_STATES:
          return
        if run_id in self.stop_requested:
          self.finalize_run(metadata, run, "stopped", None)
          self.chat_store.save_run(run)
          self.store.save_metadata(metadata)
          self.condition.notify_all()
          return
        run["status"] = "starting"
        run["updated"] = now_string()
        session = self.chat_store.get_session(run["sessionId"])
        session["status"] = "running"
        session["updated"] = run["updated"]
        start_message = f"Starting Codex container on {node_id}." if node_id else "Starting local Codex container."
        self.append_event(run["sessionId"], "starting", start_message, run_id)
        self.chat_store.save_session(session)
        self.chat_store.save_run(run)
        self.store.save_metadata(metadata)
        self.condition.notify_all()
      workspace_path = self.store.workspace_path(run["workspaceId"])
      run_for_worker = {**run, "codexSettings": self.user_codex_settings(self.users[run["user"]])}
      if node_id and self.worker_registry:
        self.codex_preparer.prepare_codex_home(run_for_worker)
        run_for_worker.pop("codexSettings", None)
        started, event_stream = self.worker_registry.client(node_id).start(run_for_worker)
        run_for_worker["container"] = str(started.get("container") or "")
      else:
        event_stream = self.runner.start(run_for_worker, workspace_path)
      for event in event_stream:
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
          stored_run["workerEventCursor"] = int(run_for_worker.get("workerEventCursor") or stored_run.get("workerEventCursor") or 0)
          self.record_runner_event(stored_run, event)
          self.chat_store.save_run(stored_run)
          self.condition.notify_all()
      with self.lock:
        metadata = self.store.load_metadata()
        self.ensure_chat_metadata(metadata)
        run = self.chat_store.get_run(run_id)
        if run:
          remote_status = str(run_for_worker.get("remoteStatus") or "")
          final_status = "stopped" if run_id in self.stop_requested or run["status"] == "stopping" or remote_status == "stopped" else "completed"
          self.finalize_run(metadata, run, final_status, None)
          self.chat_store.save_run(run)
          self.store.save_metadata(metadata)
          self.condition.notify_all()
    except Exception as exc:
      if node_id and self.worker_registry and isinstance(exc, WorkerUnavailable):
        time.sleep(max(0.0, SETTINGS.worker_run_lease_seconds))
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
      if node_id and self.worker_registry:
        stored_run = self.chat_store.get_run(run_id)
        if stored_run and stored_run.get("status") in TERMINAL_STATES:
          try:
            self.worker_registry.client(node_id).acknowledge(run_id)
          except WorkerUnavailable:
            pass
      if self.worker_registry:
        self.worker_registry.release(node_id, run_id)
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
      for event_key, run_key in [
        ("inputTokens", "inputTokens"),
        ("cachedInputTokens", "cachedInputTokens"),
        ("outputTokens", "outputTokens"),
      ]:
        if event_key in event:
          run[run_key] = max(int(run.get(run_key) or 0), int(event.get(event_key) or 0))
    if event.get("codexSessionId"):
      session = self.chat_store.get_session(run["sessionId"])
      session["codexSessionId"] = event["codexSessionId"]
      session["codexNativeResumable"] = True
      session["codexForkPending"] = False
      session["codexForkSourceId"] = None
      session["codexHomeUser"] = run["user"]
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
    remaining_runs = [
      item
      for item in self.chat_store.runs().values()
      if item.get("workspaceId") == run["workspaceId"]
      and item.get("id") != run["id"]
      and item.get("status") in RUNNING_STATES
    ]
    workspace["locked"] = bool(remaining_runs or workspace.get("activeUploadId"))
    workspace["activeRunId"] = remaining_runs[0]["id"] if remaining_runs else None
    session = self.chat_store.get_session(run["sessionId"])
    session["status"] = status
    session["updated"] = run["updated"]
    if status in {"completed", "stopped"}:
      session["codexNativeResumable"] = True
    self.chat_store.save_session(session)
    self.append_event(run["sessionId"], status, f"Run {status}.", run["id"])
    self.codex_preparer.remove_fork_base(str(run["id"]))

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

  def public_session(self, session_id: str, include_events: bool = True) -> dict:
    session = self.chat_store.public_session(session_id, include_events=include_events)
    stored = self.chat_store.get_session(session_id)
    run = self.chat_store.get_run(str(stored.get("latestRunId") or "")) if stored else None
    if not run:
      session["resources"] = self.worker_registry.aggregate_status() if self.worker_registry else self.local_resource_status()
      return session
    node_id = str(run.get("workerId") or "")
    worker = self.worker_registry.node_status(node_id) if self.worker_registry and node_id else None
    if worker:
      session["resources"] = worker
    elif self.worker_registry:
      session["resources"] = self.worker_registry.aggregate_status()
    else:
      session["resources"] = self.local_resource_status()
    session["workerId"] = node_id or ("compute-pool" if self.worker_registry else "local-docker")
    session["container"] = str(run.get("container") or "")
    session["requestedCpu"] = float(run.get("requestedCpu") or self.requested_cpu)
    session["requestedMemoryBytes"] = int(run.get("requestedMemoryBytes") or self.requested_memory_bytes)
    return session

  def local_resource_status(self) -> dict:
    active = len(self.active_runs)
    return {
      "id": "local-docker",
      "healthy": True,
      "cpuTotal": self.requested_cpu * self.capacity,
      "cpuUsed": self.requested_cpu * active,
      "cpuAvailable": self.requested_cpu * max(0, self.capacity - active),
      "memoryTotalBytes": self.requested_memory_bytes * self.capacity,
      "memoryUsedBytes": self.requested_memory_bytes * active,
      "memoryAvailableBytes": self.requested_memory_bytes * max(0, self.capacity - active),
      "activeRunCount": active,
    }

  def worker_status(self) -> list[dict]:
    if self.worker_registry:
      return self.worker_registry.status()
    active = len(self.active_runs)
    return [{
      **self.local_resource_status(),
      "ip": "127.0.0.1",
      "port": None,
      "activeRunIds": sorted(self.active_runs),
      "lastContact": time.strftime("%Y-%m-%d %H:%M:%S"),
      "error": "",
    }]

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

  def stop_and_wait_for_users(self, usernames: set[str], actor: dict, timeout: float | None = None) -> set[str]:
    timeout = timeout if timeout is not None else SETTINGS.docker_stop_timeout_seconds + SETTINGS.process_wait_timeout_seconds + 5
    with self.lock:
      runs = [
        run
        for run in self.chat_store.runs().values()
        if run.get("user") in usernames and run.get("status") in RUNNING_STATES
      ]
    for run in runs:
      self.stop_run(str(run["workspaceId"]), str(run["id"]), actor)
    deadline = time.monotonic() + timeout
    with self.condition:
      while True:
        remaining = {
          str(run["id"])
          for run in self.chat_store.runs().values()
          if run.get("user") in usernames and run.get("status") in RUNNING_STATES
        }
        if not remaining:
          return set()
        wait_for = deadline - time.monotonic()
        if wait_for <= 0:
          return remaining
        self.condition.wait(min(0.25, wait_for))

  def delete_user_resources(self, usernames: set[str]) -> dict:
    with self.lock:
      metadata = self.store.load_metadata()
      workspace_ids = {
        workspace_id
        for workspace_id, workspace in metadata.get("workspaces", {}).items()
        if workspace.get("owner") in usernames
      }
      chat_deleted = self.chat_store.delete_workspaces(workspace_ids)
      workspaces = self.store.delete_owned_workspaces(usernames)
      for username in usernames:
        shutil.rmtree(SETTINGS.codex_home_root / safe_segment(username), ignore_errors=True)
      return {"workspaces": workspaces, **chat_deleted}


def safe_segment(value: str) -> str:
  safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)
  return safe.strip("._") or "default"


def toml_string(value: str) -> str:
  return value.replace("\\", "\\\\").replace('"', '\\"')
