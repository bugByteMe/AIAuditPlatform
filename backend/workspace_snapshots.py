from __future__ import annotations

import io
import json
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable

from postgres_workspace_database import PostgresWorkspaceDatabase
from workspace_database import WorkspaceDatabase
from workspace_common import (
  BLOCKED_SUFFIXES, MAX_FILE_BYTES, MAX_FILE_COUNT, MAX_TEXT_PREVIEW_BYTES, MAX_WORKSPACE_BYTES,
  OFFICE_SUFFIXES, TEXT_SUFFIXES, StorageError, UploadedFile, ensure_under_root, generated_id, human_size,
  normalize_relative_path, now_string, stream_sha256,
)

def scan_workspace_manifest(workspace_path: Path, blob_dir: Path) -> dict:
  files = {}
  total_size = 0
  for path in sorted(item for item in workspace_path.rglob("*") if item.is_file()):
    if len(files) >= MAX_FILE_COUNT:
      raise StorageError("too_many_files", "workspace exceeds file count limit")
    relative = normalize_relative_path(path.relative_to(workspace_path).as_posix())
    stat = path.stat()
    total_size += stat.st_size
    if total_size > MAX_WORKSPACE_BYTES:
      raise StorageError("workspace_too_large", "workspace exceeds size limit")
    digest = stream_sha256(path)
    blob_path = ensure_under_root(blob_dir, blob_dir / digest[:2] / digest[2:4] / digest)
    if not blob_path.exists():
      blob_path.parent.mkdir(parents=True, exist_ok=True)
      with tempfile.NamedTemporaryFile(dir=blob_path.parent, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
      try:
        shutil.copyfile(path, temporary)
        try:
          temporary.replace(blob_path)
        except FileExistsError:
          pass
      finally:
        temporary.unlink(missing_ok=True)
    files[relative] = {
      "path": relative,
      "checksum": f"sha256:{digest}",
      "blob": digest,
      "size": stat.st_size,
      "mtime": int(stat.st_mtime),
      "mode": stat.st_mode & 0o777,
    }
  return files

class WorkspaceSnapshotMixin:
  def create_snapshot(self, metadata: dict, workspace_id: str, reason: str, actor: str, parent_id: str | None) -> dict:
    manifest = self.scan_workspace(workspace_id)
    return self.create_snapshot_from_files(metadata, workspace_id, reason, actor, parent_id, manifest)

  def create_snapshot_from_files(
    self, metadata: dict, workspace_id: str, reason: str, actor: str, parent_id: str | None, files: dict,
  ) -> dict:
    manifest = self.validate_workspace_manifest(files)
    snapshot_id = generated_id("snap")
    timestamp = now_string()
    snapshot = {
      "id": snapshot_id,
      "workspaceId": workspace_id,
      "parentSnapshotId": parent_id,
      "reason": reason,
      "actor": actor,
      "created": timestamp,
      "manifestPath": None,
      "files": manifest,
    }
    metadata["snapshots"][snapshot_id] = snapshot
    return snapshot

  def validate_workspace_manifest(self, files: dict) -> dict:
    if not isinstance(files, dict) or len(files) > MAX_FILE_COUNT:
      raise StorageError("too_many_files", "workspace exceeds file count limit")
    validated = {}
    total_size = 0
    for relative, raw_entry in files.items():
      normalized = normalize_relative_path(str(relative))
      if normalized != relative or not isinstance(raw_entry, dict):
        raise StorageError("invalid_manifest", f"invalid workspace manifest entry: {relative}")
      digest = str(raw_entry.get("blob") or "")
      checksum = str(raw_entry.get("checksum") or "")
      if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest) or checksum != f"sha256:{digest}":
        raise StorageError("invalid_manifest", f"invalid workspace checksum: {relative}")
      size = int(raw_entry.get("size") or 0)
      if size < 0:
        raise StorageError("invalid_manifest", f"invalid workspace file size: {relative}")
      total_size += size
      if total_size > MAX_WORKSPACE_BYTES:
        raise StorageError("workspace_too_large", "workspace exceeds size limit")
      blob = self.blob_path(digest)
      if not blob.is_file() or blob.stat().st_size != size:
        raise StorageError("invalid_manifest", f"workspace blob is unavailable: {relative}")
      validated[normalized] = {
        "path": normalized,
        "checksum": checksum,
        "blob": digest,
        "size": size,
        "mtime": int(raw_entry.get("mtime") or 0),
        "mode": int(raw_entry.get("mode") or 0o644) & 0o777,
      }
    return validated

  def ensure_prepared_blobs(self, workspace_id: str, files: dict) -> None:
    workspace_path = self.workspace_path(workspace_id)
    for relative, entry in files.items():
      digest = str(entry.get("blob") or "")
      source = ensure_under_root(workspace_path, workspace_path / normalize_relative_path(relative))
      if not source.is_file():
        raise StorageError("upload_incomplete", f"prepared upload file is missing: {relative}")
      actual = stream_sha256(source)
      if actual != digest or str(entry.get("checksum") or "") != f"sha256:{digest}":
        raise StorageError("upload_checksum_mismatch", f"prepared upload checksum differs: {relative}")
      blob = self.blob_path(digest)
      if not blob.exists():
        blob.parent.mkdir(parents=True, exist_ok=True)
        temporary = blob.with_suffix(".tmp")
        shutil.copyfile(source, temporary)
        try:
          temporary.replace(blob)
        except FileExistsError:
          temporary.unlink(missing_ok=True)

  def scan_workspace(self, workspace_id: str) -> dict:
    return scan_workspace_manifest(self.workspace_path(workspace_id), self.blob_dir)

  def blob_path(self, digest: str) -> Path:
    return ensure_under_root(self.blob_dir, self.blob_dir / digest[:2] / digest[2:4] / digest)

  def materialize_snapshot(self, snapshot: dict, destination: Path) -> None:
    for relative, entry in snapshot["files"].items():
      target = ensure_under_root(destination, destination / normalize_relative_path(relative))
      target.parent.mkdir(parents=True, exist_ok=True)
      shutil.copyfile(self.blob_path(entry["blob"]), target)
      os.chmod(target, int(entry.get("mode", 0o644)))

  def diff_snapshots(self, old_snapshot: dict | None, new_snapshot: dict) -> list[dict]:
    old_files = old_snapshot["files"] if old_snapshot else {}
    new_files = new_snapshot["files"]
    changes = []
    for path, entry in sorted(new_files.items()):
      previous = old_files.get(path)
      if not previous:
        status = "added"
      elif previous["checksum"] != entry["checksum"]:
        status = "modified"
      else:
        continue
      changes.append(self.artifact_from_entry(entry, status))
    for path, entry in sorted(old_files.items()):
      if path not in new_files:
        changes.append(self.artifact_from_entry(entry, "deleted"))
    return changes

  def artifact_from_entry(self, entry: dict, status: str) -> dict:
    return {
      "path": entry["path"],
      "status": status,
      "sizeBytes": entry["size"] if status != "deleted" else 0,
      "size": human_size(entry["size"] if status != "deleted" else 0),
      "checksum": entry["checksum"] if status != "deleted" else "metadata only",
      "blob": entry.get("blob"),
      "timestamp": now_string(),
    }

  def refresh_artifacts(self, workspace_id: str, user: dict) -> list[dict]:
    with self.lock:
      metadata = self.load_metadata()
      workspace = self.get_workspace_from_metadata(metadata, workspace_id, user)
      parent_id = workspace.get("latestSnapshotId")
      parent = metadata["snapshots"].get(parent_id) if parent_id else None
      snapshot = self.create_snapshot(metadata, workspace_id, "manual_refresh", user["username"], parent_id)
      workspace["latestSnapshotId"] = snapshot["id"]
      workspace["updated"] = snapshot["created"]
      workspace["fileCount"] = len(snapshot["files"])
      workspace["sizeBytes"] = sum(entry["size"] for entry in snapshot["files"].values())
      changes = self.diff_snapshots(parent, snapshot)
      metadata["artifacts"][workspace_id] = changes
      self.save_metadata(metadata)
      return changes
