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

from config import SETTINGS, parse_memory_bytes

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

def safe_segment(value: str) -> str:
  safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)
  return safe.strip("._") or "default"

def toml_string(value: str) -> str:
  return value.replace("\\", "\\\\").replace('"', '\\"')
