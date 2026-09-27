from __future__ import annotations

import shutil
import threading
import time

from chat_common import RUNNING_STATES, TERMINAL_STATES
from codex_runner import RunnerError, safe_segment
from compute_nodes import WorkerUnavailable
from config import SETTINGS
from workspace_store import StorageError, now_string


class ChatExecutionMixin:
  def events(
    self, workspace_id: str, session_id: str, after: int, user: dict, *,
    before: int | None = None, limit: int = 500, latest: bool = False,
  ) -> list[dict]:
    # Reads are independent and indexed. They must not queue behind run
    # lifecycle changes or terminal workspace scans.
    self.store.get_workspace(workspace_id, user)
    session = self.chat_store.get_session(session_id)
    if not session or session["workspaceId"] != workspace_id:
      raise StorageError("not_found", "chat session not found")
    return self.chat_store.events(session_id, after, before=before, limit=limit, latest=latest)

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
      run = self.chat_store.get_run(run_id)
      if not run or run.get("status") != "queued":
        continue
      if self.worker_registry:
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
      with self.lifecycle_locks.hold(f"run:{run_id}"):
        run = self.chat_store.get_run(run_id)
        if not run:
          return
        if run.get("status") in TERMINAL_STATES:
          return
        if run_id in self.stop_requested:
          with self.store.lock:
            metadata = self.store.load_metadata()
            self.ensure_chat_metadata(metadata)
            self.finalize_run(metadata, run, "stopped", None, remote=bool(node_id), checkpoint=False)
            self.store.save_metadata(metadata)
            self.chat_store.save_run(run)
            self.publish_run_finalization(run)
          with self.condition:
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
        with self.condition:
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
        with self.lifecycle_locks.hold(f"run:{run_id}"):
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
          with self.condition:
            self.condition.notify_all()
      with self.store.lock:
        metadata = self.store.load_metadata()
        self.ensure_chat_metadata(metadata)
        run = self.chat_store.get_run(run_id)
        if run:
          remote_status = str(run_for_worker.get("remoteStatus") or "")
          final_status = "stopped" if run_id in self.stop_requested or run["status"] == "stopping" or remote_status == "stopped" else "completed"
          self.finalize_run(
            metadata,
            run,
            final_status,
            None,
            result_files=run_for_worker.get("resultFiles") if node_id else None,
            remote=bool(node_id),
          )
          self.store.save_metadata(metadata)
          self.chat_store.save_run(run)
          self.publish_run_finalization(run)
          with self.condition:
            self.condition.notify_all()
    except Exception as exc:
      # Test and shutdown teardown may remove an ephemeral workspace root while
      # a daemon runner is finishing. There is no durable state left to repair.
      if not self.store.root.exists():
        return
      if node_id and self.worker_registry and isinstance(exc, WorkerUnavailable):
        time.sleep(max(0.0, SETTINGS.worker_run_lease_seconds))
      with self.store.lock:
        metadata = self.store.load_metadata()
        self.ensure_chat_metadata(metadata)
        run = self.chat_store.get_run(run_id)
        if run:
          remote_files = run_for_worker.get("resultFiles") if node_id and "run_for_worker" in locals() else None
          self.finalize_run(metadata, run, "failed", str(exc), result_files=remote_files, remote=bool(node_id))
          self.store.save_metadata(metadata)
          self.chat_store.save_run(run)
          self.publish_run_finalization(run)
          with self.condition:
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

  def finalize_run(
    self,
    metadata: dict,
    run: dict,
    status: str,
    error: str | None,
    *,
    result_files: dict | None = None,
    remote: bool = False,
    checkpoint: bool = True,
  ) -> None:
    workspace = metadata["workspaces"].get(run["workspaceId"])
    if not workspace:
      return
    if error:
      self.append_event(run["sessionId"], "error", error, run["id"])
    try:
      if checkpoint:
        if remote:
          if result_files is None:
            raise StorageError("missing_worker_manifest", "worker did not return the final workspace manifest")
          snapshot = self.store.refresh_workspace_metadata_from_files(metadata, workspace, status, run["id"], result_files)
        else:
          snapshot = self.store.refresh_workspace_metadata(metadata, workspace, status, run["id"])
        run["resultSnapshotId"] = snapshot["id"]
    except Exception as exc:
      self.append_event(run["sessionId"], "error", f"Checkpoint failed: {exc}", run["id"])
      if status == "completed":
        status = "failed"
    run["status"] = status
    run["updated"] = now_string()
    remaining_runs = [item for item in self.chat_store.active_runs(workspace_id=run["workspaceId"]) if item.get("id") != run["id"]]
    workspace["locked"] = bool(remaining_runs or workspace.get("activeUploadId"))
    workspace["activeRunId"] = remaining_runs[0]["id"] if remaining_runs else None

  def publish_run_finalization(self, run: dict) -> None:
    """Expose terminal chat state only after workspace and run commits succeed."""
    session = self.chat_store.get_session(run["sessionId"])
    if not session:
      return
    status = str(run["status"])
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
    persisted = self.chat_store.append_event(session_id, event)
    if self.event_callback:
      try:
        self.event_callback(session_id, persisted)
      except Exception:
        # Live notification is best-effort; persisted events remain authoritative.
        pass
    return persisted

  def set_event_callback(self, callback) -> None:
    self.event_callback = callback

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
    mode = str(user.get("providerMode") or "legacy")
    if mode == "micu":
      settings = user.get("micu") or {}
      base_url = SETTINGS.micu_inference_url
    elif mode == "custom":
      settings = user.get("customCodex") or {}
      base_url = str(settings.get("baseUrl") or "").strip()
    else:
      settings = user.get("codex") or {}
      base_url = str(settings.get("baseUrl") or SETTINGS.default_codex_base_url).strip()
    api_key = str(settings.get("apiKey") or "").strip()
    if not api_key:
      raise StorageError("codex_auth_required", "configure Codex API key before starting a run")
    if not base_url.startswith(("http://", "https://")):
      raise StorageError("codex_auth_required", "configure a valid Codex base URL before starting a run")
    return {"baseUrl": base_url, "apiKey": api_key}

  def stop_and_wait_for_users(self, usernames: set[str], actor: dict, timeout: float | None = None) -> set[str]:
    timeout = timeout if timeout is not None else SETTINGS.docker_stop_timeout_seconds + SETTINGS.process_wait_timeout_seconds + 5
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
    with self.store.lock:
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
