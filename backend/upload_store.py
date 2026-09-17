from __future__ import annotations

import json
import os
import shutil
import threading
import time
from hashlib import sha256
from pathlib import Path

from config import SETTINGS
from workspace_store import StorageError, ensure_under_root, generated_id, normalize_relative_path, now_string


ACTIVE_UPLOAD_STATES = {"uploading", "processing", "committing"}


def _atomic_json(path: Path, payload: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_suffix(path.suffix + ".tmp")
  temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
  temporary.replace(path)


class WorkerUploadStore:
  """Worker-side staging and finalization. It never writes authoritative metadata."""

  def __init__(self, root: Path, buffer_bytes: int | None = None):
    self.root = root
    self.upload_root = root / "uploads"
    self.active_root = root / "active"
    self.blob_root = root / "blobs" / "sha256"
    self.buffer_bytes = max(64 * 1024, int(buffer_bytes or SETTINGS.upload_stream_buffer_bytes))
    self.lock = threading.RLock()
    self.finalizing: set[str] = set()
    self.upload_root.mkdir(parents=True, exist_ok=True)

  def directory(self, upload_id: str) -> Path:
    if not upload_id.startswith("upload_") or not upload_id.replace("_", "").isalnum():
      raise StorageError("bad_request", "invalid upload id")
    return ensure_under_root(self.upload_root, self.upload_root / upload_id)

  def state_path(self, upload_id: str) -> Path:
    return self.directory(upload_id) / "state.json"

  def load(self, upload_id: str) -> dict:
    path = self.state_path(upload_id)
    if not path.is_file():
      raise StorageError("not_found", "upload session not found on worker")
    return json.loads(path.read_text(encoding="utf-8"))

  def save(self, state: dict) -> None:
    state["updatedAt"] = time.time()
    _atomic_json(self.state_path(state["id"]), state)

  def initialize(self, payload: dict) -> dict:
    upload_id = str(payload.get("id") or "")
    with self.lock:
      path = self.state_path(upload_id)
      if path.exists():
        return self.public(self.load(upload_id))
      files = []
      for index, item in enumerate(payload.get("files") or []):
        files.append({
          "index": index,
          "path": normalize_relative_path(str(item.get("path") or "")),
          "size": int(item.get("size") or 0),
          "lastModified": int(item.get("lastModified") or 0),
          "offset": 0,
        })
      state = {
        "id": upload_id,
        "workspaceId": str(payload.get("workspaceId") or ""),
        "mode": str(payload.get("mode") or "create"),
        "files": files,
        "parentFiles": payload.get("parentFiles") or {},
        "snapshotId": str(payload.get("snapshotId") or generated_id("snap")),
        "status": "uploading",
        "error": "",
        "processingBytes": 0,
        "totalBytes": sum(item["size"] for item in files),
        "createdAt": time.time(),
        "updatedAt": time.time(),
      }
      file_directory = self.directory(upload_id) / "files"
      file_directory.mkdir(parents=True, exist_ok=True)
      for index in range(len(files)):
        (file_directory / f"{index}.part").touch()
      self.save(state)
      return self.public(state)

  def file_path(self, upload_id: str, index: int) -> Path:
    return ensure_under_root(self.directory(upload_id), self.directory(upload_id) / "files" / f"{index}.part")

  def write_chunk(self, upload_id: str, index: int, offset: int, source, length: int) -> dict:
    if length < 0 or length > SETTINGS.upload_chunk_bytes:
      raise StorageError("bad_request", "invalid upload chunk size")
    with self.lock:
      state = self.load(upload_id)
      if state["status"] != "uploading":
        raise StorageError("upload_not_writable", "upload is no longer accepting chunks")
      if index < 0 or index >= len(state["files"]):
        raise StorageError("bad_request", "invalid upload file index")
      item = state["files"][index]
      current = int(item.get("offset") or 0)
      if offset > current or offset + length > int(item["size"]):
        raise StorageError("upload_offset_mismatch", f"expected offset {current}")
      destination = self.file_path(upload_id, index)
      destination.parent.mkdir(parents=True, exist_ok=True)
      if offset < current:
        if offset + length > current:
          raise StorageError("upload_offset_mismatch", f"expected offset {current}")
        with destination.open("rb") as existing:
          existing.seek(offset)
          remaining = length
          while remaining:
            incoming = source.read(min(self.buffer_bytes, remaining))
            if not incoming or existing.read(len(incoming)) != incoming:
              raise StorageError("upload_chunk_conflict", "retried chunk content differs")
            remaining -= len(incoming)
        return {"offset": current, **self.public(state)}
      remaining = length
      with destination.open("ab") as output:
        while remaining:
          block = source.read(min(self.buffer_bytes, remaining))
          if not block:
            raise StorageError("short_upload_chunk", "chunk ended before Content-Length")
          output.write(block)
          remaining -= len(block)
      item["offset"] = current + length
      self.save(state)
      return {"offset": item["offset"], **self.public(state)}

  def complete(self, upload_id: str) -> dict:
    with self.lock:
      state = self.load(upload_id)
      if state["status"] == "processing":
        self._start_finalize(upload_id)
        return self.public(state)
      if state["status"] == "ready":
        return self.public(state)
      if state["status"] != "uploading":
        raise StorageError("bad_request", "upload cannot be completed")
      incomplete = [item["path"] for item in state["files"] if int(item.get("offset") or 0) != int(item["size"])]
      if incomplete:
        raise StorageError("upload_incomplete", f"upload is incomplete: {incomplete[0]}")
      state["status"] = "processing"
      self.save(state)
      self._start_finalize(upload_id)
      return self.public(state)

  def status(self, upload_id: str) -> dict:
    with self.lock:
      state = self.load(upload_id)
      if state["status"] == "processing":
        self._start_finalize(upload_id)
      return self.public(state)

  def _start_finalize(self, upload_id: str) -> None:
    if upload_id in self.finalizing:
      return
    self.finalizing.add(upload_id)
    threading.Thread(target=self._finalize, args=(upload_id,), name=f"upload-finalize-{upload_id}", daemon=True).start()

  def _finalize(self, upload_id: str) -> None:
    try:
      with self.lock:
        state = self.load(upload_id)
      manifest = dict(state.get("parentFiles") or {})
      workspace = ensure_under_root(self.active_root, self.active_root / state["workspaceId"])
      workspace.mkdir(parents=True, exist_ok=True)
      processed = 0
      last_progress_save = 0
      for item in state["files"]:
        source = self.file_path(upload_id, int(item["index"]))
        digest = sha256()
        with source.open("rb") as handle:
          while True:
            block = handle.read(self.buffer_bytes)
            if not block:
              break
            digest.update(block)
            processed += len(block)
            if processed - last_progress_save >= 64 * 1024 * 1024:
              last_progress_save = processed
              with self.lock:
                current = self.load(upload_id)
                current["processingBytes"] = processed
                self.save(current)
        checksum = digest.hexdigest()
        blob = ensure_under_root(self.blob_root, self.blob_root / checksum[:2] / checksum[2:4] / checksum)
        if not blob.exists():
          blob.parent.mkdir(parents=True, exist_ok=True)
          temporary_blob = blob.with_suffix(".tmp")
          shutil.copyfile(source, temporary_blob)
          try:
            temporary_blob.replace(blob)
          except FileExistsError:
            temporary_blob.unlink(missing_ok=True)
        destination = ensure_under_root(workspace, workspace / item["path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{upload_id}.tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
        stat = destination.stat()
        manifest[item["path"]] = {
          "path": item["path"], "checksum": f"sha256:{checksum}", "blob": checksum,
          "size": stat.st_size, "mtime": int(stat.st_mtime), "mode": stat.st_mode & 0o777,
        }
      result = {"snapshotId": state["snapshotId"], "files": manifest}
      _atomic_json(self.directory(upload_id) / "result.json", result)
      with self.lock:
        state = self.load(upload_id)
        state["status"] = "ready"
        state["processingBytes"] = state["totalBytes"]
        self.save(state)
    except Exception as exc:
      with self.lock:
        try:
          state = self.load(upload_id)
          state["status"] = "failed"
          state["error"] = str(exc)
          self.save(state)
        except Exception:
          pass
    finally:
      with self.lock:
        self.finalizing.discard(upload_id)

  def result(self, upload_id: str) -> dict:
    path = self.directory(upload_id) / "result.json"
    if not path.is_file():
      raise StorageError("upload_not_ready", "upload result is not ready")
    return json.loads(path.read_text(encoding="utf-8"))

  def cancel(self, upload_id: str) -> dict:
    with self.lock:
      state = self.load(upload_id)
      if state["status"] in {"processing", "ready"}:
        raise StorageError("upload_commit_started", "upload processing has already started")
      shutil.rmtree(self.directory(upload_id), ignore_errors=True)
      return {"id": upload_id, "status": "cancelled"}

  def public(self, state: dict) -> dict:
    return {
      "id": state["id"], "status": state["status"], "error": state.get("error", ""),
      "offsets": [int(item.get("offset") or 0) for item in state["files"]],
      "uploadedBytes": sum(int(item.get("offset") or 0) for item in state["files"]),
      "processingBytes": int(state.get("processingBytes") or 0), "totalBytes": int(state.get("totalBytes") or 0),
    }


class UploadManager:
  def __init__(self, workspace_store, worker_registry=None, settings=SETTINGS, audit_callback=None):
    self.store = workspace_store
    self.worker_registry = worker_registry
    self.settings = settings
    self.audit_callback = audit_callback
    self.path = workspace_store.root / "upload_sessions.json"
    self.local_worker = WorkerUploadStore(workspace_store.root, settings.upload_stream_buffer_bytes)
    self.lock = threading.RLock()
    self.stream_slots = threading.BoundedSemaphore(max(1, settings.upload_max_concurrent_streams))
    if not self.path.exists():
      _atomic_json(self.path, {"sessions": {}})
    elif self.worker_registry:
      recovered = self.load()
      for session in recovered.get("sessions", {}).values():
        if session.get("status") in ACTIVE_UPLOAD_STATES and session.get("workerId") in self.worker_registry.nodes:
          self.worker_registry.reserve(
            session["workerId"], session["id"], settings.upload_reservation_cpus,
            settings.upload_reservation_memory_bytes, "upload",
          )
          session["reservationHeld"] = True
      self.save(recovered)
      for session in recovered.get("sessions", {}).values():
        if session.get("status") == "uploading" and session.get("reservationHeld"):
          self._schedule_idle_release(session)
    with self.lock, self.store.lock:
      persisted = self.load()
      if self._expire(persisted):
        self.save(persisted)
      for session in persisted.get("sessions", {}).values():
        if session.get("status") == "uploading":
          self._schedule_expiry(session["id"])
      metadata = self.store.load_metadata()
      changed_metadata = False
      for workspace in metadata.get("workspaces", {}).values():
        upload_id = workspace.get("activeUploadId")
        session = persisted.get("sessions", {}).get(upload_id) if upload_id else None
        if upload_id and (not session or session.get("status") not in ACTIVE_UPLOAD_STATES):
          workspace["locked"] = False
          workspace.pop("activeUploadId", None)
          changed_metadata = True
      if changed_metadata:
        self.store.save_metadata(metadata)

  def load(self) -> dict:
    return json.loads(self.path.read_text(encoding="utf-8"))

  def save(self, payload: dict) -> None:
    _atomic_json(self.path, payload)

  def create(self, user: dict, payload: dict) -> dict:
    with self.lock, self.store.lock:
      data = self.load()
      if self._expire(data):
        self.save(data)
      mode = str(payload.get("mode") or "create")
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
      upload_id = generated_id("upload")
      snapshot_id = generated_id("snap")
      metadata = self.store.load_metadata()
      parent_files, workspace_id, reserved = {}, generated_id("ws"), total
      workspace = None
      quota_user = user
      if mode == "append":
        workspace_id = str(payload.get("workspaceId") or "")
        workspace = self.store.get_workspace_from_metadata(metadata, workspace_id, user)
        if not self.store.user_can_mutate_workspace(user, workspace):
          raise StorageError("forbidden", "only the owner or system admin can add files")
        if workspace.get("locked"):
          raise StorageError("workspace_locked", "workspace has an active write lock")
        parent = metadata.get("snapshots", {}).get(workspace.get("latestSnapshotId")) or {"files": {}}
        parent_files = parent.get("files") or {}
        projected = dict((path, int(entry["size"])) for path, entry in parent_files.items())
        for item in files:
          projected[item["path"]] = item["size"]
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
      pending = sum(int(item.get("reservedBytes") or 0) for item in data["sessions"].values() if item.get("groupId") == group_id and item.get("status") in ACTIVE_UPLOAD_STATES)
      self.store.assert_group_quota(quota_user, reserved + pending, metadata)
      node_id = None
      if self.worker_registry:
        node_id = self.worker_registry.claim_upload(upload_id, self.settings.upload_reservation_cpus, self.settings.upload_reservation_memory_bytes)
        if not node_id:
          raise StorageError("no_upload_worker", "no compute worker has upload capacity")
      session = {
        "id": upload_id, "owner": user["username"], "group": user.get("group", ""), "groupId": group_id, "mode": mode,
        "workspaceId": workspace_id, "name": str(payload.get("name") or "").strip() or "Untitled workspace",
        "shared": bool(payload.get("shared")), "files": files, "parentFiles": parent_files,
        "parentSnapshotId": workspace.get("latestSnapshotId") if workspace else None,
        "snapshotId": snapshot_id, "reservedBytes": reserved, "workerId": node_id,
        "reservationHeld": bool(node_id), "status": "uploading", "createdAt": time.time(), "updatedAt": time.time(),
      }
      if workspace:
        workspace["locked"] = True
        workspace["activeUploadId"] = upload_id
        self.store.save_metadata(metadata)
      data["sessions"][upload_id] = session
      self.save(data)
      try:
        self._initialize_worker(session)
        self._schedule_idle_release(session)
        self._schedule_expiry(session["id"])
      except Exception:
        data["sessions"].pop(upload_id, None)
        self.save(data)
        if workspace:
          workspace["locked"] = False
          workspace.pop("activeUploadId", None)
          self.store.save_metadata(metadata)
        if self.worker_registry:
          self.worker_registry.release(node_id, upload_id)
        raise
      return self.public(session, {"offsets": [0] * len(files), "uploadedBytes": 0, "totalBytes": total})

  def _initialize_worker(self, session: dict) -> dict:
    payload = {key: session[key] for key in ["id", "workspaceId", "mode", "files", "parentFiles", "snapshotId"]}
    if self.worker_registry:
      return self.worker_registry.client(session["workerId"]).initialize_upload(payload)
    return self.local_worker.initialize(payload)

  def _session(self, upload_id: str, user: dict) -> tuple[dict, dict]:
    data = self.load()
    session = data.get("sessions", {}).get(upload_id)
    if not session:
      raise StorageError("not_found", "upload session not found")
    if session["owner"] != user["username"] and user.get("role") != "system_admin":
      raise StorageError("forbidden", "upload session access denied")
    return data, session

  def _ensure_worker(self, data: dict, session: dict):
    if not self.worker_registry:
      return None
    node_id = session.get("workerId")
    node = self.worker_registry.node_status(node_id) if node_id else None
    if node and node.get("healthy") and session.get("reservationHeld"):
      return self.worker_registry.client(node_id)
    self.worker_registry.release(node_id, session["id"])
    replacement = self.worker_registry.claim_upload(session["id"], self.settings.upload_reservation_cpus, self.settings.upload_reservation_memory_bytes)
    if not replacement:
      raise StorageError("no_upload_worker", "no compute worker has upload capacity")
    session["workerId"] = replacement
    session["reservationHeld"] = True
    session["updatedAt"] = time.time()
    self.save(data)
    client = self.worker_registry.client(replacement)
    payload = {key: session[key] for key in ["id", "workspaceId", "mode", "files", "parentFiles", "snapshotId"]}
    client.initialize_upload(payload)
    return client

  def _schedule_idle_release(self, session: dict) -> None:
    if not self.worker_registry or not session.get("reservationHeld"):
      return
    expected = float(session.get("updatedAt") or 0)
    timer = threading.Timer(max(1, self.settings.upload_reservation_idle_seconds), self._release_if_idle, args=(session["id"], expected))
    timer.daemon = True
    timer.start()

  def _release_if_idle(self, upload_id: str, expected_updated: float) -> None:
    with self.lock:
      data = self.load()
      session = data.get("sessions", {}).get(upload_id)
      if not session or session.get("status") != "uploading" or not session.get("reservationHeld"):
        return
      if float(session.get("updatedAt") or 0) != expected_updated:
        return
      self.worker_registry.release(session.get("workerId"), upload_id)
      session["reservationHeld"] = False
      self.save(data)

  def _schedule_expiry(self, upload_id: str, delay: float | None = None) -> None:
    timer = threading.Timer(max(1, delay if delay is not None else self.settings.upload_session_ttl_seconds), self._expire_one, args=(upload_id,))
    timer.daemon = True
    timer.start()

  def _expire_one(self, upload_id: str) -> None:
    with self.lock, self.store.lock:
      data = self.load()
      session = data.get("sessions", {}).get(upload_id)
      if not session or session.get("status") != "uploading":
        return
      remaining = float(session.get("updatedAt") or 0) + self.settings.upload_session_ttl_seconds - time.time()
      if remaining > 0:
        self._schedule_expiry(upload_id, remaining)
        return
      if self._expire(data):
        self.save(data)

  def receive_chunk(self, upload_id: str, user: dict, index: int, offset: int, source, length: int) -> dict:
    if length < 0 or length > self.settings.upload_chunk_bytes:
      raise StorageError("bad_request", "invalid upload chunk size")
    with self.stream_slots:
      with self.lock:
        data, session = self._session(upload_id, user)
        client = self._ensure_worker(data, session)
      if self.worker_registry:
        status = client.stream_upload_chunk(upload_id, index, offset, source, length, self.settings.upload_stream_buffer_bytes)
      else:
        status = self.local_worker.write_chunk(upload_id, index, offset, source, length)
      with self.lock:
        data, session = self._session(upload_id, user)
        session["updatedAt"] = time.time()
        self.save(data)
        self._schedule_idle_release(session)
      return self.public(session, status)

  def status(self, upload_id: str, user: dict) -> dict:
    with self.lock:
      data, session = self._session(upload_id, user)
      if session["status"] == "committed":
        return self.public(session, {"status": "committed", "offsets": [item["size"] for item in session["files"]], "uploadedBytes": sum(item["size"] for item in session["files"]), "totalBytes": sum(item["size"] for item in session["files"]), "processingBytes": sum(item["size"] for item in session["files"])})
      client = self._ensure_worker(data, session)
      worker_status = client.upload_status(upload_id) if self.worker_registry else self.local_worker.status(upload_id)
      session["status"] = worker_status["status"]
      session["updatedAt"] = time.time()
      if worker_status["status"] == "ready":
        session["status"] = "committing"
        self.save(data)
        result = self.local_worker.result(upload_id)
        workspace = self.store.commit_prepared_upload(user, session, result)
        session["status"] = "committed"
        session["workspace"] = workspace
        if self.worker_registry:
          self.worker_registry.release(session.get("workerId"), upload_id)
          session["reservationHeld"] = False
        self.save(data)
        if self.audit_callback and not session.get("auditRecorded"):
          event = "workspace created" if session["mode"] == "create" else "workspace files uploaded"
          self.audit_callback(session["owner"], event, session["workspaceId"])
          session["auditRecorded"] = True
      self.save(data)
      if session["status"] == "uploading":
        self._schedule_idle_release(session)
      return self.public(session, worker_status)

  def complete(self, upload_id: str, user: dict) -> dict:
    with self.lock:
      data, session = self._session(upload_id, user)
      if session["status"] == "committed":
        return self.public(session, {"status": "committed"})
      client = self._ensure_worker(data, session)
      result = client.complete_upload(upload_id) if self.worker_registry else self.local_worker.complete(upload_id)
      session["status"] = result["status"]
      session["updatedAt"] = time.time()
      self.save(data)
      return self.public(session, result)

  def cancel(self, upload_id: str, user: dict) -> dict:
    with self.lock, self.store.lock:
      data, session = self._session(upload_id, user)
      if self.worker_registry:
        self.worker_registry.client(session["workerId"]).cancel_upload(upload_id)
        self.worker_registry.release(session.get("workerId"), upload_id)
        session["reservationHeld"] = False
      else:
        self.local_worker.cancel(upload_id)
      if session["mode"] == "append":
        metadata = self.store.load_metadata()
        workspace = metadata.get("workspaces", {}).get(session["workspaceId"])
        if workspace and workspace.get("activeUploadId") == upload_id:
          workspace["locked"] = False
          workspace.pop("activeUploadId", None)
          self.store.save_metadata(metadata)
      session["status"] = "cancelled"
      self.save(data)
      return {"id": upload_id, "status": "cancelled"}

  def _expire(self, data: dict) -> bool:
    cutoff = time.time() - self.settings.upload_session_ttl_seconds
    changed = False
    for session in data.get("sessions", {}).values():
      if session.get("status") == "uploading" and float(session.get("updatedAt") or 0) < cutoff:
        changed = True
        session["status"] = "expired"
        shutil.rmtree(self.local_worker.directory(session["id"]), ignore_errors=True)
        if self.worker_registry:
          self.worker_registry.release(session.get("workerId"), session["id"])
        if session.get("mode") == "append":
          metadata = self.store.load_metadata()
          workspace = metadata.get("workspaces", {}).get(session.get("workspaceId"))
          if workspace and workspace.get("activeUploadId") == session["id"]:
            workspace["locked"] = False
            workspace.pop("activeUploadId", None)
            self.store.save_metadata(metadata)
    return changed

  def public(self, session: dict, worker_status: dict) -> dict:
    status = session.get("status") or worker_status.get("status") or "uploading"
    payload = {
      "id": session["id"], "mode": session["mode"], "workspaceId": session["workspaceId"],
      "status": status, "phase": "committing" if status == "committing" else "processing" if status in {"processing", "ready"} else "complete" if status == "committed" else "uploading",
      "chunkSizeBytes": self.settings.upload_chunk_bytes,
      "offsets": worker_status.get("offsets") or [], "uploadedBytes": int(worker_status.get("uploadedBytes") or 0),
      "processingBytes": int(worker_status.get("processingBytes") or 0), "totalBytes": int(worker_status.get("totalBytes") or sum(item["size"] for item in session["files"])),
      "error": worker_status.get("error") or "",
    }
    if session.get("workspace"):
      payload["workspace"] = session["workspace"]
    return payload
