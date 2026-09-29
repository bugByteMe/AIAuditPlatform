from __future__ import annotations

import json
import threading
import time

from chat_common import KeyedLockPool
from compute_nodes import WorkerUnavailable
from config import SETTINGS
from upload_database import TERMINAL_UPLOAD_STATES, UploadDatabase
from worker_upload import TrackedReader, WorkerUploadStore
from workspace_store import StorageError, generated_id, normalize_relative_path

ACTIVE_UPLOAD_STATES = {"uploading", "processing", "committing"}


class UploadManager:
  def __init__(self, workspace_store, worker_registry=None, settings=SETTINGS, audit_callback=None):
    self.store = workspace_store
    self.worker_registry = worker_registry
    self.settings = settings
    self.audit_callback = audit_callback
    self.database = UploadDatabase(workspace_store.root, getattr(settings, "database_url", ""))
    legacy_path = workspace_store.root / "upload_sessions.json"
    if not self.database.all() and legacy_path.is_file():
      legacy_sessions = (json.loads(legacy_path.read_text(encoding="utf-8")) or {}).get("sessions") or {}
      if legacy_sessions and getattr(settings, "database_url", ""):
        raise RuntimeError("upload_sessions.json requires offline migration before SQL upload coordination can start")
      if legacy_sessions:
        self.database.import_sessions(legacy_sessions)
    self.local_worker = WorkerUploadStore(workspace_store.root, settings.upload_stream_buffer_bytes)
    self.session_locks = KeyedLockPool()
    self.stream_slots = threading.BoundedSemaphore(max(1, settings.upload_max_concurrent_streams))
    self.gc_stop = threading.Event()
    for session in self.database.all().values():
      if session.get("status") in ACTIVE_UPLOAD_STATES and session.get("reservationHeld"):
        session["reservationHeld"] = False
        self.database.save(session)
    self._repair_workspace_leases()
    self.gc_thread = threading.Thread(target=self._gc_loop, name="ai-audit-upload-gc", daemon=True)
    self.gc_thread.start()

  def load(self) -> dict:
    """Compatibility/testing view; runtime mutations are row-scoped."""
    return {"sessions": self.database.all()}

  def save(self, payload: dict) -> None:
    for session in payload.get("sessions", {}).values():
      self.database.save(session)

  def shutdown(self) -> None:
    self.gc_stop.set()

  def _repair_workspace_leases(self) -> None:
    sessions = self.database.all()
    for workspace in self.store.database.list_workspace_headers(username="", group_name="", system_admin=True):
      upload_id = workspace.get("activeUploadId")
      session = sessions.get(upload_id) if upload_id else None
      if upload_id and (not session or session.get("status") not in ACTIVE_UPLOAD_STATES):
        workspace["locked"] = False
        workspace.pop("activeUploadId", None)
        self.store.save_workspace_lifecycle(workspace)

  def create(self, user: dict, payload: dict) -> dict:
    mode = str(payload.get("mode") or "create")
    group_key = f"group:{user.get('groupId') or user.get('group') or user['username']}"
    workspace_key = f"workspace:{payload.get('workspaceId')}" if mode == "append" else group_key
    with self.store.coordination_locks.hold(group_key, workspace_key):
      if mode not in {"create", "append"}:
        raise StorageError("bad_request", "upload mode must be create or append")
      raw_files = payload.get("files") or []
      if not raw_files:
        raise StorageError("empty_upload", "at least one uploaded file is required")
      if len(raw_files) > self.settings.max_file_count:
        raise StorageError("too_many_files", "workspace exceeds file count limit")
      files, seen, total = [], set(), 0
      for source in raw_files:
        path = normalize_relative_path(str(source.get("path") or ""))
        if path in seen:
          raise StorageError("bad_request", f"duplicate upload path: {path}")
        seen.add(path)
        size = int(source.get("size") or 0)
        if size < 0 or size > self.settings.max_file_bytes:
          raise StorageError("file_too_large", f"{path} exceeds the per-file limit")
        total += size
        files.append({"path": path, "size": size, "lastModified": int(source.get("lastModified") or 0)})
      upload_id, snapshot_id = generated_id("upload"), generated_id("snap")
      metadata = self.store.empty_metadata()
      parent_files, workspace_id, reserved = {}, generated_id("ws"), total
      workspace, quota_user = None, user
      if mode == "append":
        workspace_id = str(payload.get("workspaceId") or "")
        metadata = self.store.load_workspace_metadata(workspace_id)
        workspace = self.store.get_workspace_from_metadata(metadata, workspace_id, user)
        if not self.store.user_can_mutate_workspace(user, workspace):
          raise StorageError("forbidden", "only the owner or system admin can add files")
        if workspace.get("locked"):
          raise StorageError("workspace_locked", "workspace has an active write lock")
        parent = metadata.get("snapshots", {}).get(workspace.get("latestSnapshotId")) or {"files": {}}
        parent_files = parent.get("files") or {}
        projected = {path: int(entry["size"]) for path, entry in parent_files.items()}
        projected.update({item["path"]: item["size"] for item in files})
        if len(projected) > self.settings.max_file_count:
          raise StorageError("too_many_files", "workspace exceeds file count limit")
        projected_size = sum(projected.values())
        if projected_size > self.settings.max_workspace_bytes:
          raise StorageError("workspace_too_large", "workspace exceeds the total size limit")
        reserved = max(0, projected_size - int(workspace.get("sizeBytes") or 0))
        quota_user = self.store.workspace_quota_owner(workspace, user)
      elif total > self.settings.max_workspace_bytes:
        raise StorageError("workspace_too_large", "workspace exceeds the total size limit")
      group_id = str(quota_user.get("groupId") or "")
      pending = self.database.active_reserved_bytes(group_id, ACTIVE_UPLOAD_STATES)
      self.store.assert_group_quota(quota_user, reserved + pending, metadata)
      node_id = None
      if self.worker_registry:
        node_id = self.worker_registry.claim_upload(upload_id, self.settings.upload_reservation_cpus, self.settings.upload_reservation_memory_bytes)
        if not node_id:
          raise StorageError("no_upload_worker", "no compute worker has upload capacity")
      now = time.time()
      session = {
        "id": upload_id, "owner": user["username"], "group": user.get("group", ""), "groupId": group_id,
        "mode": mode, "workspaceId": workspace_id, "name": str(payload.get("name") or "").strip() or "Untitled workspace",
        "shared": bool(payload.get("shared")), "files": files, "parentFiles": parent_files,
        "parentSnapshotId": workspace.get("latestSnapshotId") if workspace else None, "snapshotId": snapshot_id,
        "reservedBytes": reserved, "workerId": node_id, "reservationHeld": bool(node_id),
        "status": "uploading", "createdAt": now, "updatedAt": now,
      }
      if workspace:
        workspace["locked"], workspace["activeUploadId"] = True, upload_id
        self.store.save_workspace_lifecycle(workspace)
      self.database.save(session)
      try:
        self._initialize_worker(session)
      except Exception:
        self.database.delete(upload_id)
        self._release_workspace(session)
        if self.worker_registry:
          self.worker_registry.release(node_id, upload_id)
        raise
      return self.public(session, {"offsets": [0] * len(files), "uploadedBytes": 0, "totalBytes": total})

  def _initialize_worker(self, session: dict) -> dict:
    payload = {key: session[key] for key in ["id", "workspaceId", "mode", "files", "parentFiles", "snapshotId"]}
    if self.worker_registry:
      return self.worker_registry.client(session["workerId"]).initialize_upload(payload)
    return self.local_worker.initialize(payload)

  def _session(self, upload_id: str, user: dict) -> dict:
    session = self.database.get(upload_id)
    if not session:
      raise StorageError("not_found", "upload session not found")
    if session["owner"] != user["username"] and user.get("role") != "system_admin":
      raise StorageError("forbidden", "upload session access denied")
    return session

  def _ensure_worker(self, session: dict):
    if not self.worker_registry:
      return None
    node_id = session.get("workerId")
    node = self.worker_registry.node_status(node_id) if node_id else None
    if node and node.get("healthy") and session.get("reservationHeld"):
      return self.worker_registry.client(node_id)
    self.worker_registry.release(node_id, session["id"])
    session["reservationHeld"] = False
    replacement = self.worker_registry.claim_upload(session["id"], self.settings.upload_reservation_cpus, self.settings.upload_reservation_memory_bytes)
    if not replacement:
      self.database.save(session)
      raise StorageError("no_upload_worker", "no compute worker has upload capacity")
    session.update({"workerId": replacement, "reservationHeld": True, "updatedAt": time.time()})
    self.database.save(session)
    client = self.worker_registry.client(replacement)
    try:
      client.initialize_upload({key: session[key] for key in ["id", "workspaceId", "mode", "files", "parentFiles", "snapshotId"]})
    except WorkerUnavailable as exc:
      self._release_failed_reservation(session, exc)
      raise StorageError("upload_worker_unavailable", str(exc)) from exc
    return client

  def _release_failed_reservation(self, session: dict, error: Exception) -> None:
    if self.worker_registry:
      self.worker_registry.release(session.get("workerId"), session["id"])
    session.update({"reservationHeld": False, "updatedAt": time.time(), "error": str(error)})
    self.database.save(session)

  def receive_chunk(self, upload_id: str, user: dict, index: int, offset: int, source, length: int) -> dict:
    if length < 0 or length > self.settings.upload_chunk_bytes:
      raise StorageError("bad_request", "invalid upload chunk size")
    with self.stream_slots, self.session_locks.hold(upload_id):
      session = self._session(upload_id, user)
      client = self._ensure_worker(session)
      tracked = TrackedReader(source)
      try:
        status = client.stream_upload_chunk(upload_id, index, offset, tracked, length, self.settings.upload_stream_buffer_bytes) if self.worker_registry else self.local_worker.write_chunk(upload_id, index, offset, tracked, length)
      except WorkerUnavailable as exc:
        remaining = max(0, length - tracked.bytes_read)
        while remaining:
          block = source.read(min(self.settings.upload_stream_buffer_bytes, remaining))
          if not block:
            break
          remaining -= len(block)
        self._release_failed_reservation(session, exc)
        raise StorageError("upload_worker_unavailable", str(exc)) from exc
      session.update({"error": "", "updatedAt": time.time()})
      self.database.save(session)
      return self.public(session, status)

  def status(self, upload_id: str, user: dict) -> dict:
    with self.session_locks.hold(upload_id):
      session = self._session(upload_id, user)
      if session["status"] == "committed":
        total = sum(item["size"] for item in session["files"])
        return self.public(session, {"status": "committed", "offsets": [item["size"] for item in session["files"]], "uploadedBytes": total, "totalBytes": total, "processingBytes": total})
      if session["status"] in TERMINAL_UPLOAD_STATES:
        return self.public(session, {"status": session["status"], "error": session.get("error", "")})
      return self._reconcile_session(session, user)

  def _reconcile_session(self, session: dict, user: dict) -> dict:
    client = self._ensure_worker(session)
    try:
      worker_status = client.upload_status(session["id"]) if self.worker_registry else self.local_worker.status(session["id"])
    except WorkerUnavailable as exc:
      self._release_failed_reservation(session, exc)
      raise StorageError("upload_worker_unavailable", str(exc)) from exc
    session.update({"status": worker_status["status"], "updatedAt": time.time()})
    if worker_status["status"] == "failed":
      self._terminal(session, "failed", worker_status.get("error") or "upload processing failed")
    elif worker_status["status"] == "ready":
      session["status"] = "committing"
      self.database.save(session)
      result = client.upload_result(session["id"]) if self.worker_registry else self.local_worker.result(session["id"])
      session["workspace"] = self.store.commit_prepared_upload(user, session, result)
      self._terminal(session, "committed")
      if self.audit_callback and not session.get("auditRecorded"):
        event = "workspace created" if session["mode"] == "create" else "workspace files uploaded"
        self.audit_callback(session["owner"], event, session["workspaceId"])
        session["auditRecorded"] = True
        self.database.save(session)
    else:
      self.database.save(session)
    return self.public(session, worker_status)

  def complete(self, upload_id: str, user: dict) -> dict:
    with self.session_locks.hold(upload_id):
      session = self._session(upload_id, user)
      if session["status"] == "committed":
        return self.public(session, {"status": "committed"})
      client = self._ensure_worker(session)
      try:
        result = client.complete_upload(upload_id) if self.worker_registry else self.local_worker.complete(upload_id)
      except WorkerUnavailable as exc:
        self._release_failed_reservation(session, exc)
        raise StorageError("upload_worker_unavailable", str(exc)) from exc
      session.update({"status": result["status"], "updatedAt": time.time()})
      self.database.save(session)
      return self.public(session, result)

  def cancel(self, upload_id: str, user: dict) -> dict:
    session = self._session(upload_id, user)
    with self.session_locks.hold(upload_id), self.store.coordination_locks.hold(f"workspace:{session['workspaceId']}"):
      session = self._session(upload_id, user)
      if self.worker_registry:
        self.worker_registry.client(session["workerId"]).cancel_upload(upload_id)
      else:
        self.local_worker.cancel(upload_id)
      self._terminal(session, "cancelled")
      return {"id": upload_id, "status": "cancelled"}

  def _terminal(self, session: dict, status: str, error: str = "") -> None:
    now = time.time()
    session.update({"status": status, "error": error, "updatedAt": now, "terminalAt": now, "cleanupPending": True})
    if self.worker_registry:
      self.worker_registry.release(session.get("workerId"), session["id"])
    session["reservationHeld"] = False
    self._release_workspace(session)
    self.database.save(session)
    self._cleanup_staging(session)

  def _release_workspace(self, session: dict) -> None:
    if session.get("mode") != "append":
      return
    workspace = self.store.database.get_workspace_header(session.get("workspaceId"))
    if workspace and workspace.get("activeUploadId") == session["id"]:
      workspace["locked"] = False
      workspace.pop("activeUploadId", None)
      self.store.save_workspace_lifecycle(workspace)

  def _cleanup_staging(self, session: dict) -> None:
    try:
      if self.worker_registry and session.get("workerId"):
        self.worker_registry.client(session["workerId"]).purge_upload(session["id"])
      else:
        self.local_worker.purge(session["id"])
      session["cleanupPending"] = False
      session["stagingCleanedAt"] = time.time()
    except Exception as exc:
      session["cleanupPending"] = True
      session["cleanupError"] = str(exc)
    self.database.save(session)

  def _gc_loop(self) -> None:
    interval = max(1, int(getattr(self.settings, "upload_gc_interval_seconds", 300)))
    while not self.gc_stop.wait(interval):
      try:
        self.collect_garbage()
      except Exception:
        continue

  def collect_garbage(self) -> dict:
    now = time.time()
    batch = max(1, int(getattr(self.settings, "upload_gc_batch_size", 100)))
    cutoff = now - int(self.settings.upload_session_ttl_seconds)
    expired = 0
    for candidate in self.database.stale_active(cutoff, {"uploading"}, batch):
      with self.session_locks.hold(candidate["id"]), self.store.coordination_locks.hold(f"workspace:{candidate['workspaceId']}"):
        current = self.database.get(candidate["id"])
        if current and current.get("status") == "uploading" and float(current.get("updatedAt") or 0) < cutoff:
          self._terminal(current, "expired", "upload session expired")
          expired += 1
    for candidate in self.database.cleanup_pending(batch):
      with self.session_locks.hold(candidate["id"]):
        current = self.database.get(candidate["id"])
        if current and current.get("cleanupPending"):
          self._cleanup_staging(current)
    retention = int(getattr(self.settings, "upload_terminal_retention_seconds", 86400))
    deleted = self.database.delete_terminal_before(now - retention, batch)
    return {"expired": expired, "deleted": len(deleted)}

  def public(self, session: dict, worker_status: dict) -> dict:
    status = session.get("status") or worker_status.get("status") or "uploading"
    payload = {
      "id": session["id"], "mode": session["mode"], "workspaceId": session["workspaceId"], "status": status,
      "phase": "committing" if status == "committing" else "processing" if status in {"processing", "ready"} else "complete" if status in TERMINAL_UPLOAD_STATES else "uploading",
      "chunkSizeBytes": self.settings.upload_chunk_bytes, "offsets": worker_status.get("offsets") or [],
      "uploadedBytes": int(worker_status.get("uploadedBytes") or 0), "processingBytes": int(worker_status.get("processingBytes") or 0),
      "totalBytes": int(worker_status.get("totalBytes") or sum(item["size"] for item in session["files"])),
      "error": worker_status.get("error") or session.get("error") or "",
    }
    if session.get("workspace"):
      payload["workspace"] = session["workspace"]
    return payload
