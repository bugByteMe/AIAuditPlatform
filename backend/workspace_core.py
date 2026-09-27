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

class WorkspaceCoreMixin:
  def __init__(self, root: Path, database_url: str = ""):
    self.root = root
    self.active_dir = root / "active"
    self.blob_dir = root / "blobs" / "sha256"
    self.bundle_dir = root / "bundles"
    self.preview_dir = root / "previews"
    self.chat_session_provider: Callable[[dict], list[dict]] | None = None
    self.account_provider: Callable[[], tuple[dict[str, dict], dict[str, dict]]] | None = None
    self.lock = threading.RLock()
    self.database = PostgresWorkspaceDatabase(database_url) if database_url else WorkspaceDatabase(root)
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

  def load_workspace_metadata(self, workspace_id: str) -> dict:
    try:
      metadata = self.database.load_workspace(workspace_id)
    except (sqlite3.DatabaseError, RuntimeError) as exc:
      raise StorageError("metadata_corrupt", "workspace metadata database is corrupt or unsupported") from exc
    if metadata is None:
      raise StorageError("not_found", "workspace not found")
    return metadata

  def save_metadata(self, metadata: dict) -> None:
    candidates = self.database.save(metadata)
    for digest in candidates:
      blob = self.blob_path(digest)
      if blob.exists():
        blob.unlink()
    self.prune_preview_blobs(candidates)
    self.remove_empty_blob_directories()

  def save_workspace_lifecycle(self, workspace: dict) -> None:
    with self.lock:
      self.database.save_workspace_lifecycle(workspace)

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
    workspaces = self.database.list_workspace_headers(
      username=str(user.get("username") or ""),
      group_name=str(user.get("group") or ""),
      system_admin=user.get("role") == "system_admin",
    )
    return [self.public_workspace_summary(workspace) for workspace in workspaces]

  def get_workspace(self, workspace_id: str, user: dict) -> dict:
    workspace = self.database.get_workspace_header(workspace_id)
    if not workspace:
      raise StorageError("not_found", "workspace not found")
    if not self.user_can_access(user, workspace):
      raise StorageError("forbidden", "workspace access denied")
    return workspace

  def public_workspace_summary(self, workspace: dict) -> dict:
    return {
      "id": workspace["id"], "name": workspace["name"], "owner": workspace["owner"],
      "group": workspace.get("group", ""), "fileCount": workspace.get("fileCount", 0),
      "sizeBytes": workspace.get("sizeBytes", 0), "size": human_size(workspace.get("sizeBytes", 0)),
      "updated": workspace["updated"], "shared": bool(workspace.get("shared")),
      "locked": bool(workspace.get("locked")), "runLockEnabled": bool(workspace.get("runLockEnabled")),
      "activeRunCount": 1 if workspace.get("activeRunId") else 0,
      "latestSnapshotId": workspace.get("latestSnapshotId"),
      "sessions": [], "artifacts": [], "files": [], "detailLoaded": False,
    }

  def public_workspace(self, workspace: dict, metadata: dict | None = None) -> dict:
    if metadata is None:
      metadata = self.load_workspace_metadata(workspace["id"])
      workspace = metadata["workspaces"][workspace["id"]]
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
      "detailLoaded": True,
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

