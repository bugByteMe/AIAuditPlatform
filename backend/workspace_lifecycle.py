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
from hashlib import sha256
from pathlib import Path
from typing import Callable

from postgres_workspace_database import PostgresWorkspaceDatabase
from workspace_database import WorkspaceDatabase
from workspace_common import (
  BLOCKED_SUFFIXES, MAX_FILE_BYTES, MAX_FILE_COUNT, MAX_TEXT_PREVIEW_BYTES, MAX_WORKSPACE_BYTES,
  OFFICE_SUFFIXES, TEXT_SUFFIXES, StorageError, UploadedFile, ensure_under_root, generated_id, human_size,
  normalize_relative_path, now_string,
)

class WorkspaceLifecycleMixin:
  def create_workspace(self, user: dict, name: str, shared: bool, files: list[UploadedFile]) -> dict:
    with self.lock:
      upload_sizes = {normalize_relative_path(item.path): len(item.content) for item in files}
      self.assert_group_quota(user, sum(upload_sizes.values()))
      return self._create_workspace(user, name, shared, files)

  def _create_workspace(self, user: dict, name: str, shared: bool, files: list[UploadedFile]) -> dict:
    if not files:
      raise StorageError("empty_upload", "at least one uploaded file is required")
    workspace_id = generated_id("ws")
    workspace_path = self.workspace_path(workspace_id)
    workspace_path.mkdir(parents=True, exist_ok=False)
    sizes: dict[str, int] = {}
    total = 0
    try:
      for item in files:
        relative = normalize_relative_path(item.path)
        size = len(item.content)
        if size > MAX_FILE_BYTES:
          raise StorageError("file_too_large", f"{relative} exceeds the per-file limit")
        total += size - sizes.get(relative, 0)
        sizes[relative] = size
        if total > MAX_WORKSPACE_BYTES:
          raise StorageError("workspace_too_large", "workspace exceeds the total size limit")
        if len(sizes) > MAX_FILE_COUNT:
          raise StorageError("too_many_files", "workspace exceeds file count limit")
        destination = ensure_under_root(workspace_path, workspace_path / relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.content)
    except Exception:
      shutil.rmtree(workspace_path, ignore_errors=True)
      raise

    metadata = self.load_metadata()
    timestamp = now_string()
    workspace = {
      "id": workspace_id,
      "name": name.strip() or "Untitled workspace",
      "owner": user["username"],
      "group": user.get("group", ""),
      "shared": shared,
      "locked": False,
      "runLockEnabled": False,
      "created": timestamp,
      "updated": timestamp,
      "fileCount": 0,
      "sizeBytes": 0,
      "latestSnapshotId": None,
      "initialSnapshotId": None,
      "sourceWorkspaceId": None,
      "sourceSnapshotId": None,
      "sessions": [],
    }
    metadata["workspaces"][workspace_id] = workspace
    snapshot = self.create_snapshot(metadata, workspace_id, "upload", user["username"], None)
    workspace["initialSnapshotId"] = snapshot["id"]
    workspace["latestSnapshotId"] = snapshot["id"]
    workspace["fileCount"] = len(snapshot["files"])
    workspace["sizeBytes"] = sum(entry["size"] for entry in snapshot["files"].values())
    workspace["sessions"] = []
    self.save_metadata(metadata)
    return self.public_workspace(workspace, metadata)

  def update_workspace(self, workspace_id: str, user: dict, payload: dict) -> dict:
    with self.lock:
      metadata = self.load_metadata()
      workspace = self.get_workspace_from_metadata(metadata, workspace_id, user)
      if workspace["owner"] != user["username"] and user["role"] != "system_admin":
        raise StorageError("forbidden", "only the owner or system admin can update workspace settings")
      if "name" in payload:
        name = str(payload["name"]).strip()
        if not name:
          raise StorageError("bad_request", "workspace name cannot be empty")
        workspace["name"] = name
      if "shared" in payload:
        workspace["shared"] = bool(payload["shared"])
      if "runLockEnabled" in payload:
        if workspace["owner"] != user["username"]:
          raise StorageError("forbidden", "only the workspace owner can change the exclusive run lock")
        requested = bool(payload["runLockEnabled"])
        if requested != bool(workspace.get("runLockEnabled")):
          if workspace.get("locked"):
            raise StorageError("workspace_locked", "exclusive run lock cannot change while workspace jobs are active")
          workspace["runLockEnabled"] = requested
      workspace["updated"] = now_string()
      self.save_metadata(metadata)
      return self.public_workspace(workspace, metadata)

  def user_can_mutate_workspace(self, user: dict, workspace: dict) -> bool:
    return user["role"] == "system_admin" or workspace["owner"] == user["username"]

  def add_files_to_workspace(self, workspace_id: str, user: dict, files: list[UploadedFile]) -> dict:
    with self.lock:
      metadata = self.load_metadata()
      workspace = self.get_workspace_from_metadata(metadata, workspace_id, user)
      workspace_path = self.workspace_path(workspace_id)
      sizes = {
        path.relative_to(workspace_path).as_posix(): path.stat().st_size
        for path in workspace_path.rglob("*")
        if path.is_file()
      }
      for item in files:
        sizes[normalize_relative_path(item.path)] = len(item.content)
      projected_size = sum(sizes.values())
      quota_owner = self.workspace_quota_owner(workspace, user)
      self.assert_group_quota(quota_owner, max(0, projected_size - int(workspace.get("sizeBytes") or 0)), metadata)
      return self._add_files_to_workspace(workspace_id, user, files)

  def _add_files_to_workspace(self, workspace_id: str, user: dict, files: list[UploadedFile]) -> dict:
    if not files:
      raise StorageError("empty_upload", "at least one uploaded file is required")
    metadata = self.load_metadata()
    workspace = self.get_workspace_from_metadata(metadata, workspace_id, user)
    if not self.user_can_mutate_workspace(user, workspace):
      raise StorageError("forbidden", "only the owner or system admin can add files")
    if workspace.get("locked"):
      raise StorageError("workspace_locked", "workspace has an active write lock")

    workspace_path = self.workspace_path(workspace_id)
    sizes = {
      path.relative_to(workspace_path).as_posix(): path.stat().st_size
      for path in workspace_path.rglob("*")
      if path.is_file()
    }
    total_size = sum(sizes.values())
    normalized_files: list[tuple[str, UploadedFile]] = []
    for item in files:
      relative = normalize_relative_path(item.path)
      size = len(item.content)
      if size > MAX_FILE_BYTES:
        raise StorageError("file_too_large", f"{relative} exceeds the per-file limit")
      total_size += size - sizes.get(relative, 0)
      sizes[relative] = size
      if total_size > MAX_WORKSPACE_BYTES:
        raise StorageError("workspace_too_large", "workspace exceeds the total size limit")
      if len(sizes) > MAX_FILE_COUNT:
        raise StorageError("too_many_files", "workspace exceeds file count limit")
      normalized_files.append((relative, item))
    for relative, item in normalized_files:
      destination = ensure_under_root(workspace_path, workspace_path / relative)
      destination.parent.mkdir(parents=True, exist_ok=True)
      destination.write_bytes(item.content)

    self.refresh_workspace_metadata(metadata, workspace, "file_upload", user["username"])
    self.save_metadata(metadata)
    return self.public_workspace(workspace, metadata)

  def delete_workspace_path(self, workspace_id: str, user: dict, raw_path: str) -> dict:
    with self.lock:
      metadata = self.load_metadata()
      workspace = self.get_workspace_from_metadata(metadata, workspace_id, user)
      if not self.user_can_mutate_workspace(user, workspace):
        raise StorageError("forbidden", "only the owner or system admin can delete files")
      if workspace.get("locked"):
        raise StorageError("workspace_locked", "workspace has an active write lock")

      workspace_path = self.workspace_path(workspace_id)
      relative = normalize_relative_path(raw_path)
      target = ensure_under_root(workspace_path, workspace_path / relative)
      if not target.exists():
        raise StorageError("not_found", "file or folder not found")
      if target.is_dir():
        shutil.rmtree(target)
      else:
        target.unlink()

      self.refresh_workspace_metadata(metadata, workspace, "file_delete", user["username"])
      self.save_metadata(metadata)
      return self.public_workspace(workspace, metadata)

  def refresh_workspace_metadata(self, metadata: dict, workspace: dict, reason: str, actor: str) -> dict:
    parent_id = workspace.get("latestSnapshotId")
    parent = metadata["snapshots"].get(parent_id) if parent_id else None
    snapshot = self.create_snapshot(metadata, workspace["id"], reason, actor, parent_id)
    workspace["latestSnapshotId"] = snapshot["id"]
    workspace["updated"] = snapshot["created"]
    workspace["fileCount"] = len(snapshot["files"])
    workspace["sizeBytes"] = sum(entry["size"] for entry in snapshot["files"].values())
    metadata["artifacts"][workspace["id"]] = self.diff_snapshots(parent, snapshot)
    return snapshot

  def commit_prepared_upload(self, user: dict, session: dict, result: dict) -> dict:
    """Commit a worker-prepared manifest while keeping metadata control-plane-owned."""
    with self.lock:
      metadata = self.load_metadata()
      workspace_id = str(session["workspaceId"])
      snapshot_id = str(result.get("snapshotId") or session["snapshotId"])
      files = result.get("files") or {}
      if len(files) > MAX_FILE_COUNT or sum(int(entry.get("size") or 0) for entry in files.values()) > MAX_WORKSPACE_BYTES:
        raise StorageError("workspace_too_large", "prepared upload exceeds workspace limits")
      timestamp = now_string()
      if session["mode"] == "create":
        workspace = metadata["workspaces"].get(workspace_id)
        if workspace:
          return self.public_workspace(workspace, metadata)
        workspace = {
          "id": workspace_id,
          "name": str(session.get("name") or "Untitled workspace"),
          "owner": session["owner"],
          "group": session.get("group", ""),
          "shared": bool(session.get("shared")),
          "locked": False,
          "runLockEnabled": False,
          "created": timestamp,
          "updated": timestamp,
          "fileCount": len(files),
          "sizeBytes": sum(int(entry["size"]) for entry in files.values()),
          "latestSnapshotId": snapshot_id,
          "initialSnapshotId": snapshot_id,
          "sourceWorkspaceId": None,
          "sourceSnapshotId": None,
          "sessions": [],
        }
        metadata["workspaces"][workspace_id] = workspace
        parent_id = None
      else:
        workspace = self.get_workspace_from_metadata(metadata, workspace_id, user)
        if workspace.get("latestSnapshotId") == snapshot_id:
          return self.public_workspace(workspace, metadata)
        if workspace.get("activeUploadId") != session["id"]:
          raise StorageError("workspace_locked", "workspace upload lease is not owned by this upload")
        if workspace.get("latestSnapshotId") != session.get("parentSnapshotId"):
          raise StorageError("workspace_changed", "workspace changed during upload")
        parent_id = workspace.get("latestSnapshotId")
      snapshot = {
        "id": snapshot_id,
        "workspaceId": workspace_id,
        "parentSnapshotId": parent_id,
        "reason": "upload" if session["mode"] == "create" else "file_upload",
        "actor": user["username"],
        "created": timestamp,
        "manifestPath": None,
        "files": files,
      }
      self.ensure_prepared_blobs(workspace_id, files)
      parent = metadata["snapshots"].get(parent_id) if parent_id else None
      metadata["snapshots"][snapshot_id] = snapshot
      workspace["latestSnapshotId"] = snapshot_id
      workspace["updated"] = timestamp
      workspace["fileCount"] = len(files)
      workspace["sizeBytes"] = sum(int(entry["size"]) for entry in files.values())
      workspace["locked"] = False
      workspace.pop("activeUploadId", None)
      if session["mode"] == "create":
        metadata["artifacts"][workspace_id] = []
      else:
        metadata["artifacts"][workspace_id] = self.diff_snapshots(parent, snapshot)
      self.save_metadata(metadata)
      return self.public_workspace(workspace, metadata)

  def fork_workspace(self, workspace_id: str, user: dict, name: str | None = None) -> dict:
    with self.lock:
      metadata = self.load_metadata()
      source = self.get_workspace_from_metadata(metadata, workspace_id, user)
      if source.get("locked"):
        raise StorageError("workspace_locked", "workspace has an active write lock")
      self.assert_group_quota(user, int(source.get("sizeBytes") or 0), metadata)
      return self._fork_workspace(workspace_id, user, name)

  def _fork_workspace(self, workspace_id: str, user: dict, name: str | None = None) -> dict:
    metadata = self.load_metadata()
    source = self.get_workspace_from_metadata(metadata, workspace_id, user)
    source_snapshot_id = source.get("latestSnapshotId")
    if not source_snapshot_id:
      raise StorageError("bad_request", "source workspace has no snapshot")
    source_snapshot = metadata["snapshots"][source_snapshot_id]
    fork_id = generated_id("ws")
    fork_path = self.workspace_path(fork_id)
    fork_path.mkdir(parents=True, exist_ok=False)
    self.materialize_snapshot(source_snapshot, fork_path)
    timestamp = now_string()
    fork = {
      "id": fork_id,
      "name": name or f"{source['name']} Copy",
      "owner": user["username"],
      "group": user.get("group", ""),
      "shared": False,
      "locked": False,
      "runLockEnabled": False,
      "created": timestamp,
      "updated": timestamp,
      "fileCount": 0,
      "sizeBytes": 0,
      "latestSnapshotId": None,
      "initialSnapshotId": None,
      "sourceWorkspaceId": source["id"],
      "sourceSnapshotId": source_snapshot_id,
      "sessions": [],
    }
    metadata["workspaces"][fork_id] = fork
    snapshot = self.create_snapshot(metadata, fork_id, "fork", user["username"], None)
    fork["initialSnapshotId"] = snapshot["id"]
    fork["latestSnapshotId"] = snapshot["id"]
    fork["fileCount"] = len(snapshot["files"])
    fork["sizeBytes"] = sum(entry["size"] for entry in snapshot["files"].values())
    fork["sessions"] = []
    self.save_metadata(metadata)
    return self.public_workspace(fork, metadata)

  def delete_workspace(self, workspace_id: str, user: dict) -> dict:
    with self.lock:
      metadata = self.load_metadata()
      workspace = self.get_workspace_from_metadata(metadata, workspace_id, user)
      if workspace["owner"] != user["username"] and user["role"] != "system_admin":
        raise StorageError("forbidden", "only the owner or system admin can delete a workspace")
      if workspace.get("locked"):
        raise StorageError("workspace_locked", "workspace has an active write lock")

      deleted = {
        "id": workspace["id"],
        "name": workspace["name"],
        "owner": workspace["owner"],
        "deleted": now_string(),
      }
      metadata["workspaces"].pop(workspace_id, None)
      metadata.get("artifacts", {}).pop(workspace_id, None)
      for snapshot_id, snapshot in list(metadata.get("snapshots", {}).items()):
        if snapshot.get("workspaceId") == workspace_id:
          metadata["snapshots"].pop(snapshot_id, None)
      self.save_metadata(metadata)
      shutil.rmtree(self.workspace_path(workspace_id), ignore_errors=True)
      shutil.rmtree(self.preview_dir / workspace_id, ignore_errors=True)
      self.garbage_collect_blobs()
      return deleted

  def delete_owned_workspaces(self, usernames: set[str]) -> list[dict]:
    with self.lock:
      metadata = self.load_metadata()
      targets = [workspace_id for workspace_id, workspace in metadata.get("workspaces", {}).items() if workspace.get("owner") in usernames]
      deleted = []
      for workspace_id in targets:
        workspace = metadata["workspaces"].pop(workspace_id)
        deleted.append({"id": workspace_id, "name": workspace.get("name", ""), "owner": workspace.get("owner", "")})
        metadata.get("artifacts", {}).pop(workspace_id, None)
        for snapshot_id, snapshot in list(metadata.get("snapshots", {}).items()):
          if snapshot.get("workspaceId") != workspace_id:
            continue
          metadata["snapshots"].pop(snapshot_id, None)
        shutil.rmtree(self.workspace_path(workspace_id), ignore_errors=True)
        shutil.rmtree(self.preview_dir / workspace_id, ignore_errors=True)
      self.save_metadata(metadata)
      self.garbage_collect_blobs(metadata)
      return deleted

  def garbage_collect_blobs(self, metadata: dict | None = None) -> int:
    referenced = self.database.referenced_blobs()
    removed = 0
    removed_digests: set[str] = set()
    if self.blob_dir.exists():
      for path in self.blob_dir.rglob("*"):
        if path.is_file() and not path.name.endswith(".tmp") and path.name not in referenced:
          removed_digests.add(path.name)
          path.unlink()
          removed += 1
      self.prune_preview_blobs(removed_digests)
      self.remove_empty_blob_directories()
    return removed

  def remove_empty_blob_directories(self) -> None:
    if not self.blob_dir.exists():
      return
    for path in sorted((item for item in self.blob_dir.rglob("*") if item.is_dir()), reverse=True):
      try:
        path.rmdir()
      except OSError:
        pass

  def get_workspace_from_metadata(self, metadata: dict, workspace_id: str, user: dict) -> dict:
    workspace = metadata["workspaces"].get(workspace_id)
    if not workspace:
      raise StorageError("not_found", "workspace not found")
    if not self.user_can_access(user, workspace):
      raise StorageError("forbidden", "workspace access denied")
    return workspace

