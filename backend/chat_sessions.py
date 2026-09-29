from __future__ import annotations

import shutil
import threading

from chat_common import RUNNING_STATES, TERMINAL_STATES
from checkpoint_delta import write_baseline
from workspace_store import StorageError, generated_id, now_string


class ChatSessionMixin:
  def public_workspace_sessions(self, workspace: dict) -> list[dict]:
    return [self.public_session(item["id"], include_events=False) for item in workspace.get("sessions", []) if self.chat_store.get_session(item.get("id"))]

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
      with self.lifecycle_locks.hold(f"workspace:{run['workspaceId']}"):
        metadata = self.store.load_workspace_metadata(str(run["workspaceId"]))
        stored = self.chat_store.get_run(str(run["id"]))
        if stored:
          self.finalize_run(
            metadata,
            stored,
            "failed",
            "control plane restarted before the run completed",
            remote=bool(node_id),
            checkpoint=not bool(node_id),
          )
          self.store.save_workspace_metadata(str(run["workspaceId"]), metadata)
          self.chat_store.save_run(stored)
          self.publish_run_finalization(stored)

  def ensure_chat_metadata(self, metadata: dict) -> None:
    for workspace in metadata.get("workspaces", {}).values():
      workspace.setdefault("sessions", [])

  def list_sessions(self, workspace_id: str, user: dict) -> list[dict]:
    self.store.get_workspace(workspace_id, user)
    metadata = self.store.load_workspace_metadata(workspace_id)
    self.ensure_chat_metadata(metadata)
    workspace = metadata["workspaces"][workspace_id]
    return [self.public_session(session["id"], include_events=False) for session in workspace.get("sessions", []) if self.chat_store.get_session(session["id"])]

  def create_session(self, workspace_id: str, user: dict, title: str | None = None) -> dict:
    with self.lifecycle_locks.hold(f"workspace:{workspace_id}"):
      metadata = self.store.load_workspace_metadata(workspace_id)
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
      self.store.save_workspace_lifecycle(workspace)
      return self.public_session(session["id"])

  def fork_session(self, workspace_id: str, session_id: str, user: dict, title: str | None = None) -> dict:
    with self.lifecycle_locks.hold(f"workspace:{workspace_id}", f"session:{session_id}"):
      metadata = self.store.load_workspace_metadata(workspace_id)
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
      self.store.save_workspace_lifecycle(workspace)
      return self.public_session(fork_id, include_events=False)

  def update_session(self, workspace_id: str, session_id: str, user: dict, title: str) -> dict:
    with self.lifecycle_locks.hold(f"session:{session_id}"):
      metadata = self.store.load_workspace_metadata(workspace_id)
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
    with self.lifecycle_locks.hold(f"workspace:{workspace_id}", f"session:{session_id}"):
      metadata = self.store.load_workspace_metadata(workspace_id)
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
      session_runs = self.chat_store.runs_for_session(session_id)
      if any(run.get("status") in RUNNING_STATES for run in session_runs.values()):
        raise StorageError("session_run_active", "stop the active chat run before deleting this session")

      workspace["sessions"] = [item for item in session_refs if str(item.get("id") or "") != session_id]
      if workspace.get("activeRunId") in session_runs:
        workspace["activeRunId"] = None
      workspace["updated"] = now_string()
      self.store.save_workspace_lifecycle(workspace)
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
    if str(user.get("providerMode") or "legacy") == "micu":
      if not self.budget_checker:
        raise StorageError("budget_provider_unavailable", "MicuAPI budget provider is not configured")
      self.budget_checker(user)
    elif "providerMode" not in user and int(user.get("budgetTokens") or 0) > 0:
      if int(user.get("usedTokens") or 0) >= int(user.get("budgetTokens") or 0):
        raise StorageError("budget_exhausted", "user token budget is exhausted")
    self.user_codex_settings(user)
    if self.worker_registry and not self.worker_registry.compatible(self.requested_cpu, self.requested_memory_bytes):
      raise StorageError("no_compatible_worker", "no configured compute node can satisfy the run resources")
    group_id = str(user.get("groupId") or "")
    with self.lifecycle_locks.hold(f"workspace:{workspace_id}", f"group:{group_id}"):
      metadata = self.store.load_workspace_metadata(workspace_id)
      self.ensure_chat_metadata(metadata)
      workspace = self.store.get_workspace_from_metadata(metadata, workspace_id, user)
      quota_owner = self.store.workspace_quota_owner(workspace, user)
      self.store.assert_group_quota(quota_owner, metadata=metadata, require_available=True)
      session_id = str(payload.get("sessionId") or "")
      if session_id and not self.chat_store.get_session(session_id):
        session_id = ""
      active_runs = self.chat_store.active_runs(workspace_id=workspace_id)
      group = self.groups.get(group_id) if group_id else None
      if group:
        live_run_limit = int(group.get("liveRunLimit", 1))
        group_live_runs = self.chat_store.active_runs(group_id=group_id)
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
      base_snapshot = metadata.get("snapshots", {}).get(run.get("baseSnapshotId")) or {"files": {}}
      run["checkpointBaseRef"] = write_baseline(
        self.store.root, run["id"], run.get("baseSnapshotId"), base_snapshot.get("files") or {},
      )
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
      self.store.save_workspace_lifecycle(workspace)
      self.queue.put(run["id"])
      with self.condition:
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
    with self.lifecycle_locks.hold(f"workspace:{workspace_id}", f"run:{run_id}"):
      metadata = self.store.load_workspace_metadata(workspace_id)
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
        self.store.save_workspace_metadata(workspace_id, metadata)
        self.chat_store.save_run(run)
        self.publish_run_finalization(run)
      with self.condition:
        self.condition.notify_all()
    if not was_queued:
      if self.worker_registry and run.get("workerId"):
        self.worker_registry.client(str(run["workerId"])).stop(str(run["id"]))
      else:
        self.runner.stop(run)
    return {"run": run}
