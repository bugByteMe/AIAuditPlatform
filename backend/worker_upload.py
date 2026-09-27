from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from hashlib import sha256
from pathlib import Path

from config import SETTINGS
from workspace_store import StorageError, UploadedFile, ensure_under_root, normalize_relative_path

def _atomic_json(path: Path, payload: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_suffix(path.suffix + ".tmp")
  temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
  temporary.replace(path)

class TrackedReader:
  def __init__(self, source):
    self.source = source
    self.bytes_read = 0

  def read(self, size=-1):
    block = self.source.read(size)
    self.bytes_read += len(block or b"")
    return block

class WorkerUploadStore:
  """Worker-side staging and finalization. It never writes authoritative metadata."""

  def __init__(self, root: Path, buffer_bytes: int | None = None):
    self.root = root
    self.upload_root = root / "uploads"
    self.active_root = root / "active"
    self.blob_root = root / "blobs" / "sha256"
    self.buffer_bytes = max(64 * 1024, int(buffer_bytes or SETTINGS.upload_stream_buffer_bytes))
    self.lock = threading.RLock()
    self.file_locks: dict[tuple[str, int], threading.RLock] = {}
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

  def file_lock(self, upload_id: str, index: int) -> threading.RLock:
    with self.lock:
      return self.file_locks.setdefault((upload_id, index), threading.RLock())

  def write_chunk(self, upload_id: str, index: int, offset: int, source, length: int) -> dict:
    if length < 0 or length > SETTINGS.upload_chunk_bytes:
      raise StorageError("bad_request", "invalid upload chunk size")
    if index < 0:
      raise StorageError("bad_request", "invalid upload file index")
    with self.file_lock(upload_id, index):
      with self.lock:
        state = self.load(upload_id)
        if state["status"] != "uploading":
          raise StorageError("upload_not_writable", "upload is no longer accepting chunks")
        if index >= len(state["files"]):
          raise StorageError("bad_request", "invalid upload file index")
        item = dict(state["files"][index])
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
      with destination.open("r+b") as output:
        output.seek(current)
        output.truncate(current)
        while remaining:
          block = source.read(min(self.buffer_bytes, remaining))
          if not block:
            raise StorageError("short_upload_chunk", "chunk ended before Content-Length")
          output.write(block)
          remaining -= len(block)
      with self.lock:
        state = self.load(upload_id)
        item = state["files"][index]
        if int(item.get("offset") or 0) != current:
          raise StorageError("upload_offset_mismatch", f"expected offset {item.get('offset') or 0}")
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
        for key in [key for key in self.file_locks if key[0] == upload_id]:
          self.file_locks.pop(key, None)

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
      locks = [self.file_lock(upload_id, int(item["index"])) for item in state["files"]]
    for lock in locks:
      lock.acquire()
    try:
      shutil.rmtree(self.directory(upload_id), ignore_errors=True)
    finally:
      for lock in reversed(locks):
        lock.release()
      with self.lock:
        for key in [key for key in self.file_locks if key[0] == upload_id]:
          self.file_locks.pop(key, None)
    return {"id": upload_id, "status": "cancelled"}

  def public(self, state: dict) -> dict:
    return {
      "id": state["id"], "status": state["status"], "error": state.get("error", ""),
      "offsets": [int(item.get("offset") or 0) for item in state["files"]],
      "uploadedBytes": sum(int(item.get("offset") or 0) for item in state["files"]),
      "processingBytes": int(state.get("processingBytes") or 0), "totalBytes": int(state.get("totalBytes") or 0),
    }
