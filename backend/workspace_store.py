from __future__ import annotations

import io
import json
import mimetypes
import os
import posixpath
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

from config import SETTINGS
from workspace_database import WorkspaceDatabase


MAX_FILE_BYTES = SETTINGS.max_file_bytes
MAX_WORKSPACE_BYTES = SETTINGS.max_workspace_bytes
MAX_FILE_COUNT = SETTINGS.max_file_count
MAX_TEXT_PREVIEW_BYTES = SETTINGS.max_text_preview_bytes
BLOCKED_SUFFIXES = {suffix.lower() for suffix in SETTINGS.blocked_upload_suffixes}
TEXT_SUFFIXES = {
  ".csv",
  ".css",
  ".html",
  ".js",
  ".json",
  ".log",
  ".md",
  ".py",
  ".txt",
  ".ts",
  ".xml",
  ".yaml",
  ".yml",
}
OFFICE_SUFFIXES = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}


class StorageError(ValueError):
  def __init__(self, code: str, message: str):
    super().__init__(message)
    self.code = code
    self.message = message


def now_string() -> str:
  return time.strftime("%Y-%m-%d %H:%M:%S")


def generated_id(prefix: str) -> str:
  return f"{prefix}_{uuid.uuid4().hex[:12]}"


def human_size(value: int) -> str:
  units = ["B", "KB", "MB", "GB", "TB"]
  size = float(value)
  for unit in units:
    if size < 1024 or unit == units[-1]:
      if unit == "B":
        return f"{int(size)} B"
      return f"{size:.1f} {unit}".replace(".0 ", " ")
    size /= 1024
  return f"{value} B"


def normalize_relative_path(raw_path: str) -> str:
  path = raw_path.replace("\\", "/").strip()
  if not path:
    raise StorageError("unsafe_path", "file path is required")
  path = posixpath.normpath(path)
  if path in {"", "."} or path.startswith("/") or path.startswith("../") or "/../" in path or path == "..":
    raise StorageError("unsafe_path", f"unsafe path: {raw_path}")
  parts = path.split("/")
  if any(part in {"", ".", ".."} for part in parts):
    raise StorageError("unsafe_path", f"unsafe path: {raw_path}")
  if Path(parts[-1]).suffix.lower() in BLOCKED_SUFFIXES:
    raise StorageError("blocked_file_type", f"blocked file type: {raw_path}")
  return path


def ensure_under_root(root: Path, candidate: Path) -> Path:
  resolved_root = root.resolve()
  resolved = candidate.resolve()
  if resolved != resolved_root and resolved_root not in resolved.parents:
    raise StorageError("unsafe_path", "resolved path escapes storage root")
  return resolved


@dataclass
class UploadedFile:
  path: str
  content: bytes


class WorkspaceStore:
  def __init__(self, root: Path):
    self.root = root
    self.active_dir = root / "active"
    self.blob_dir = root / "blobs" / "sha256"
    self.bundle_dir = root / "bundles"
    self.preview_dir = root / "previews"
    self.chat_session_provider: Callable[[dict], list[dict]] | None = None
    self.account_provider: Callable[[], tuple[dict[str, dict], dict[str, dict]]] | None = None
    self.lock = threading.RLock()
    self.database = WorkspaceDatabase(root)
    self.ensure_layout()

  def set_chat_session_provider(self, provider: Callable[[dict], list[dict]]) -> None:
    self.chat_session_provider = provider

  def set_account_provider(self, provider: Callable[[], tuple[dict[str, dict], dict[str, dict]]]) -> None:
    self.account_provider = provider

  def usage_summaries(self, metadata: dict | None = None) -> tuple[dict[str, dict], dict[str, dict]]:
    metadata = metadata or self.load_metadata()
    users, groups = self.account_provider() if self.account_provider else ({}, {})
    user_usage = {
      str(user.get("id") or username): {"diskUsageBytes": 0, "workspaceCount": 0}
      for username, user in users.items()
    }
    group_usage = {
      group_id: {"diskUsageBytes": 0, "workspaceCount": 0, "userCount": 0}
      for group_id in groups
    }
    users_by_name = {username: user for username, user in users.items()}
    for user in users.values():
      group_id = str(user.get("groupId") or "")
      if group_id in group_usage:
        group_usage[group_id]["userCount"] += 1
    for workspace in metadata.get("workspaces", {}).values():
      owner = users_by_name.get(str(workspace.get("owner") or ""))
      if not owner:
        continue
      size = max(0, int(workspace.get("sizeBytes") or 0))
      user_id = str(owner.get("id") or owner.get("username") or "")
      summary = user_usage.setdefault(user_id, {"diskUsageBytes": 0, "workspaceCount": 0})
      summary["diskUsageBytes"] += size
      summary["workspaceCount"] += 1
      group_id = str(owner.get("groupId") or "")
      if group_id in group_usage:
        group_usage[group_id]["diskUsageBytes"] += size
        group_usage[group_id]["workspaceCount"] += 1
    return user_usage, group_usage

  def assert_group_quota(self, user: dict, added_bytes: int = 0, metadata: dict | None = None, require_available: bool = False) -> None:
    if not self.account_provider:
      return
    _, groups = self.account_provider()
    group_id = str(user.get("groupId") or "")
    group = groups.get(group_id)
    if not group:
      return
    limit = group.get("diskLimitBytes")
    if limit is None:
      return
    if not require_available and int(added_bytes) <= 0:
      return
    _, group_usage = self.usage_summaries(metadata)
    used = int(group_usage.get(group_id, {}).get("diskUsageBytes") or 0)
    projected = used + max(0, int(added_bytes))
    if projected > int(limit) or (require_available and projected >= int(limit)):
      raise StorageError("group_disk_quota_exceeded", "group workspace disk limit is exceeded")

  def workspace_quota_owner(self, workspace: dict, fallback: dict) -> dict:
    if not self.account_provider:
      return fallback
    users, _ = self.account_provider()
    return users.get(str(workspace.get("owner") or ""), fallback)

  def ensure_layout(self) -> None:
    for path in [self.active_dir, self.blob_dir, self.bundle_dir, self.preview_dir]:
      path.mkdir(parents=True, exist_ok=True)

  def load_metadata(self) -> dict:
    self.ensure_layout()
    try:
      return self.database.load()
    except (sqlite3.DatabaseError, RuntimeError) as exc:
      raise StorageError("metadata_corrupt", "workspace metadata database is corrupt or unsupported") from exc

  def save_metadata(self, metadata: dict) -> None:
    candidates = self.database.save(metadata)
    for digest in candidates:
      blob = self.blob_path(digest)
      if blob.exists():
        blob.unlink()
    self.prune_preview_blobs(candidates)
    self.remove_empty_blob_directories()

  def prune_preview_blobs(self, digests: set[str]) -> None:
    if not digests or not self.preview_dir.exists():
      return
    for workspace_previews in self.preview_dir.iterdir():
      if not workspace_previews.is_dir():
        continue
      for digest in digests:
        shutil.rmtree(workspace_previews / digest, ignore_errors=True)

  def user_can_access(self, user: dict, workspace: dict) -> bool:
    if user["role"] == "system_admin":
      return True
    if workspace["owner"] == user["username"]:
      return True
    return bool(workspace.get("shared")) and workspace.get("group") and workspace.get("group") == user.get("group")

  def list_workspaces(self, user: dict) -> list[dict]:
    metadata = self.load_metadata()
    items = []
    for workspace in metadata["workspaces"].values():
      if self.user_can_access(user, workspace):
        items.append(self.public_workspace(workspace, metadata))
    return sorted(items, key=lambda item: item["updated"], reverse=True)

  def get_workspace(self, workspace_id: str, user: dict) -> dict:
    metadata = self.load_metadata()
    workspace = metadata["workspaces"].get(workspace_id)
    if not workspace:
      raise StorageError("not_found", "workspace not found")
    if not self.user_can_access(user, workspace):
      raise StorageError("forbidden", "workspace access denied")
    return workspace

  def public_workspace(self, workspace: dict, metadata: dict | None = None) -> dict:
    metadata = metadata or self.load_metadata()
    workspace_id = workspace["id"]
    sessions = self.public_workspace_sessions(workspace, metadata)
    active_run_count = sum(1 for session in sessions if session.get("status") in {"queued", "starting", "running", "stopping"})
    return {
      "id": workspace_id,
      "name": workspace["name"],
      "owner": workspace["owner"],
      "group": workspace.get("group", ""),
      "fileCount": workspace.get("fileCount", 0),
      "sizeBytes": workspace.get("sizeBytes", 0),
      "size": human_size(workspace.get("sizeBytes", 0)),
      "updated": workspace["updated"],
      "shared": bool(workspace.get("shared")),
      "locked": bool(workspace.get("locked")),
      "runLockEnabled": bool(workspace.get("runLockEnabled")),
      "activeRunCount": active_run_count,
      "latestSnapshotId": workspace.get("latestSnapshotId"),
      "sessions": sessions,
      "artifacts": self.workspace_artifacts(workspace_id, metadata),
      "files": self.file_tree(workspace_id),
    }

  def public_workspace_sessions(self, workspace: dict, metadata: dict) -> list[dict]:
    if self.chat_session_provider:
      return self.chat_session_provider(workspace)
    chat_sessions = metadata.get("chatSessions", {})
    events_by_session = metadata.get("events", {})
    sessions = []
    for item in workspace.get("sessions", []):
      session_id = item.get("id")
      if session_id in chat_sessions:
        session = chat_sessions[session_id]
        events = events_by_session.get(session_id, [])
        sessions.append(
          {
            "id": session["id"],
            "title": session["title"],
            "status": session["status"],
            "updated": session["updated"],
            "tokens": session.get("tokens", "0"),
            "latestRunId": session.get("latestRunId"),
            "codexNativeResumable": bool(session.get("codexNativeResumable")),
            "events": [[event["type"], event["message"], event["message"], event.get("runId") or ""] for event in events],
          }
        )
      elif {"title", "status", "updated", "tokens", "events"}.issubset(item):
        if item.get("title") not in {"Workspace setup", "Fork created"}:
          sessions.append(item)
    return sessions

  def workspace_path(self, workspace_id: str) -> Path:
    return ensure_under_root(self.active_dir, self.active_dir / workspace_id)

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

  def create_snapshot(self, metadata: dict, workspace_id: str, reason: str, actor: str, parent_id: str | None) -> dict:
    snapshot_id = generated_id("snap")
    manifest = self.scan_workspace(workspace_id)
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

  def ensure_prepared_blobs(self, workspace_id: str, files: dict) -> None:
    workspace_path = self.workspace_path(workspace_id)
    for relative, entry in files.items():
      digest = str(entry.get("blob") or "")
      source = ensure_under_root(workspace_path, workspace_path / normalize_relative_path(relative))
      if not source.is_file():
        raise StorageError("upload_incomplete", f"prepared upload file is missing: {relative}")
      actual = sha256(source.read_bytes()).hexdigest()
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
    workspace_path = self.workspace_path(workspace_id)
    files = {}
    count = 0
    for path in sorted(item for item in workspace_path.rglob("*") if item.is_file()):
      count += 1
      if count > MAX_FILE_COUNT:
        raise StorageError("too_many_files", "workspace exceeds file count limit")
      relative = path.relative_to(workspace_path).as_posix()
      normalized = normalize_relative_path(relative)
      stat = path.stat()
      digest = sha256(path.read_bytes()).hexdigest()
      blob_path = self.blob_path(digest)
      if not blob_path.exists():
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, blob_path)
      files[normalized] = {
        "path": normalized,
        "checksum": f"sha256:{digest}",
        "blob": digest,
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "mode": stat.st_mode & 0o777,
      }
    return files

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

  def workspace_artifacts(self, workspace_id: str, metadata: dict | None = None) -> list[dict]:
    metadata = metadata or self.load_metadata()
    if workspace_id in metadata.get("artifacts", {}):
      return metadata["artifacts"][workspace_id]
    workspace = metadata["workspaces"].get(workspace_id)
    if not workspace:
      return []
    initial = metadata["snapshots"].get(workspace.get("initialSnapshotId"))
    latest = metadata["snapshots"].get(workspace.get("latestSnapshotId"))
    if not latest:
      return []
    return self.diff_snapshots(initial if initial and initial["id"] != latest["id"] else None, latest)

  def file_tree(self, workspace_id: str) -> list[dict]:
    workspace_path = self.workspace_path(workspace_id)
    if not workspace_path.exists():
      return []
    rows = []
    seen_dirs = set()
    for item in sorted(workspace_path.rglob("*")):
      relative = item.relative_to(workspace_path).as_posix()
      parts = relative.split("/")
      if item.is_dir():
        seen_dirs.add(relative)
        rows.append({"name": item.name, "path": relative, "type": "folder", "level": len(parts) - 1})
      elif item.is_file():
        parent = posixpath.dirname(relative)
        if parent and parent not in seen_dirs:
          for index in range(1, len(parts)):
            folder = "/".join(parts[:index])
            if folder not in seen_dirs:
              seen_dirs.add(folder)
              rows.append({"name": parts[index - 1], "path": folder, "type": "folder", "level": index - 1})
        rows.append({"name": item.name, "path": relative, "type": "file", "level": len(parts) - 1, "size": human_size(item.stat().st_size)})
    return rows

  def workspace_file_path(self, workspace_id: str, raw_path: str) -> Path:
    workspace_path = self.workspace_path(workspace_id)
    relative = normalize_relative_path(raw_path)
    file_path = ensure_under_root(workspace_path, workspace_path / relative)
    if not file_path.is_file():
      raise StorageError("not_found", "file not found")
    return file_path

  def file_metadata(self, workspace_id: str, raw_path: str) -> dict:
    file_path = self.workspace_file_path(workspace_id, raw_path)
    relative = file_path.relative_to(self.workspace_path(workspace_id)).as_posix()
    stat = file_path.stat()
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    digest = sha256(file_path.read_bytes()).hexdigest()
    return {
      "path": relative,
      "name": file_path.name,
      "sizeBytes": stat.st_size,
      "size": human_size(stat.st_size),
      "contentType": content_type,
      "checksum": f"sha256:{digest}",
      "blob": digest,
      "suffix": file_path.suffix.lower(),
    }

  def preview_metadata(self, workspace_id: str, raw_path: str) -> dict:
    metadata = self.file_metadata(workspace_id, raw_path)
    file_path = self.workspace_file_path(workspace_id, raw_path)
    suffix = metadata["suffix"]
    content_type = metadata["contentType"]
    if suffix in TEXT_SUFFIXES or content_type.startswith("text/"):
      raw = file_path.read_bytes()[:MAX_TEXT_PREVIEW_BYTES]
      return {
        **metadata,
        "mode": "text",
        "text": raw.decode("utf-8", errors="replace"),
        "truncated": metadata["sizeBytes"] > MAX_TEXT_PREVIEW_BYTES,
      }
    if content_type.startswith("image/"):
      return {**metadata, "mode": "image"}
    if content_type == "application/pdf" or suffix == ".pdf":
      return {**metadata, "mode": "pdf"}
    if suffix in OFFICE_SUFFIXES:
      return {**metadata, "mode": "office"}
    return {**metadata, "mode": "unsupported"}

  def rendered_preview_path(self, workspace_id: str, raw_path: str) -> Path:
    metadata = self.file_metadata(workspace_id, raw_path)
    if metadata["suffix"] not in OFFICE_SUFFIXES:
      raise StorageError("unsupported_preview", "only office files can be rendered")
    source = self.workspace_file_path(workspace_id, raw_path)
    cache_dir = ensure_under_root(self.preview_dir, self.preview_dir / workspace_id / metadata["blob"])
    cached_pdf = ensure_under_root(cache_dir, cache_dir / "preview.pdf")
    if cached_pdf.exists():
      return cached_pdf
    converter = shutil.which("libreoffice") or shutil.which("soffice")
    if not converter:
      raise StorageError("converter_unavailable", "LibreOffice is not available")
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=self.preview_dir) as temp_name:
      temp_dir = Path(temp_name)
      temp_source = temp_dir / f"source{metadata['suffix']}"
      shutil.copyfile(source, temp_source)
      result = subprocess.run(
        [converter, "--headless", "--convert-to", "pdf", "--outdir", str(temp_dir), str(temp_source)],
        capture_output=True,
        timeout=SETTINGS.office_preview_timeout_seconds,
        check=False,
      )
      rendered = temp_dir / "source.pdf"
      if result.returncode != 0 or not rendered.exists():
        detail = result.stderr.decode("utf-8", errors="replace") or result.stdout.decode("utf-8", errors="replace")
        raise StorageError("preview_failed", detail.strip() or "Office preview conversion failed")
      shutil.copyfile(rendered, cached_pdf)
    return cached_pdf

  def build_download_zip(self, workspace_id: str, user: dict, mode: str, selected_paths: list[str]) -> tuple[str, bytes]:
    metadata = self.load_metadata()
    workspace = self.get_workspace_from_metadata(metadata, workspace_id, user)
    mode = mode or "changes"
    if mode not in {"changes", "full"}:
      raise StorageError("bad_request", "mode must be changes or full")
    selected = [normalize_relative_path(path) for path in selected_paths if path]
    latest = metadata["snapshots"].get(workspace.get("latestSnapshotId"))
    if not latest:
      raise StorageError("bad_request", "workspace has no snapshot")

    if mode == "full":
      entries = [self.artifact_from_entry(entry, "current") for entry in latest["files"].values()]
    else:
      entries = [entry for entry in self.workspace_artifacts(workspace_id, metadata) if entry["status"] in {"added", "modified"}]
    if selected:
      allowed = {entry["path"] for entry in entries}
      for path in selected:
        if path not in allowed:
          raise StorageError("bad_request", f"selected path is not downloadable: {path}")
      entries = [entry for entry in entries if entry["path"] in set(selected)]

    manifest = {
      "workspaceId": workspace_id,
      "workspaceName": workspace["name"],
      "baseSnapshotId": workspace.get("initialSnapshotId"),
      "resultSnapshotId": workspace.get("latestSnapshotId"),
      "mode": mode,
      "included": entries,
      "deleted": [entry for entry in self.workspace_artifacts(workspace_id, metadata) if entry["status"] == "deleted"],
      "created": now_string(),
    }
    root_name = f"workspace-{workspace_id}-{mode}"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
      archive.writestr(f"{root_name}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
      for entry in entries:
        source_entry = latest["files"].get(entry["path"])
        if not source_entry:
          continue
        archive.write(self.blob_path(source_entry["blob"]), f"{root_name}/files/{entry['path']}")
    return f"{root_name}.zip", buffer.getvalue()


def parse_query(query: str) -> dict[str, list[str]]:
  return parse_qs(query, keep_blank_values=False)


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], list[UploadedFile]]:
  if "multipart/form-data" not in content_type:
    raise StorageError("bad_request", "multipart/form-data is required")
  marker = "boundary="
  if marker not in content_type:
    raise StorageError("bad_request", "multipart boundary is missing")
  boundary = content_type.split(marker, 1)[1].split(";", 1)[0].strip().strip('"')
  if not boundary:
    raise StorageError("bad_request", "multipart boundary is empty")
  delimiter = b"--" + boundary.encode("utf-8")
  fields: dict[str, str] = {}
  files: list[UploadedFile] = []
  path_queue: list[str] = []
  for raw_part in body.split(delimiter):
    part = raw_part
    if part.startswith(b"\r\n"):
      part = part[2:]
    if not part or part in {b"--", b"--\r\n"}:
      continue
    if part.endswith(b"--\r\n"):
      part = part[:-4]
    elif part.endswith(b"--"):
      part = part[:-2]
    headers_raw, separator, content = part.partition(b"\r\n\r\n")
    if not separator:
      continue
    headers = {}
    for line in headers_raw.decode("utf-8", errors="replace").split("\r\n"):
      key, _, value = line.partition(":")
      headers[key.lower()] = value.strip()
    disposition = headers.get("content-disposition", "")
    params = _parse_disposition(disposition)
    name = params.get("name", "")
    filename = params.get("filename")
    if content.endswith(b"\r\n"):
      content = content[:-2]
    if filename is not None:
      relative = path_queue.pop(0) if path_queue else params.get("relativePath") or params.get("webkitRelativePath") or fields.get(f"{name}Path") or filename
      files.append(UploadedFile(relative, content))
    elif name:
      if name == "paths":
        path_queue.append(content.decode("utf-8", errors="replace"))
      fields[name] = content.decode("utf-8", errors="replace")
  return fields, files


def _parse_disposition(value: str) -> dict[str, str]:
  params: dict[str, str] = {}
  for item in value.split(";"):
    item = item.strip()
    if "=" not in item:
      continue
    key, raw = item.split("=", 1)
    params[key.strip()] = raw.strip().strip('"')
  return params


def parse_urlencoded_paths(values: list[str]) -> list[str]:
  paths: list[str] = []
  for value in values:
    paths.extend(item for item in value.split(",") if item)
  return paths
