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
import weakref
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import Callable

from chat_common import KeyedLockPool
from config import SETTINGS
from postgres_workspace_database import PostgresWorkspaceDatabase
from workspace_database import WorkspaceDatabase
from workspace_common import (
  BLOCKED_SUFFIXES, BUNDLE_TEMP_TTL_SECONDS, MAX_FILE_BYTES, MAX_FILE_COUNT, MAX_TEXT_PREVIEW_BYTES, MAX_WORKSPACE_BYTES,
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
    self.coordination_locks = KeyedLockPool()
    self._gc_lock = threading.Lock()
    self._gc_candidates: dict[str, float] = {}
    self.database = PostgresWorkspaceDatabase(database_url) if database_url else WorkspaceDatabase(root)
    self.ensure_layout()
    self.cleanup_stale_bundles()
    threading.Thread(target=self._blob_gc_loop, args=(weakref.ref(self),), name="workspace-blob-gc", daemon=True).start()

  def cleanup_stale_bundles(self) -> None:
    cutoff = time.time() - BUNDLE_TEMP_TTL_SECONDS
    for bundle in self.bundle_dir.glob("download-*.zip"):
      try:
        if bundle.is_file() and bundle.stat().st_mtime < cutoff:
          bundle.unlink(missing_ok=True)
      except OSError:
        continue

  def set_chat_session_provider(self, provider: Callable[[dict], list[dict]]) -> None:
    self.chat_session_provider = provider

  def set_account_provider(self, provider: Callable[[], tuple[dict[str, dict], dict[str, dict]]]) -> None:
    self.account_provider = provider

  def usage_summaries(self, metadata: dict | None = None) -> tuple[dict[str, dict], dict[str, dict]]:
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
    workspaces = (
      metadata.get("workspaces", {}).values()
      if metadata is not None
      else self.database.list_workspace_headers(username="", group_name="", system_admin=True)
    )
    for workspace in workspaces:
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
    # Quota admission must use every workspace summary. A scoped mutation view
    # intentionally contains only one workspace and would undercount the group.
    _, group_usage = self.usage_summaries()
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
    """Read-only compatibility catalog for diagnostics and migration tests."""
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

  def save_workspace_metadata(self, workspace_id: str, metadata: dict) -> None:
    workspace = metadata.get("workspaces", {}).get(workspace_id)
    if workspace is None:
      raise StorageError("not_found", "workspace not found")
    try:
      candidates = self.database.save_workspace_state(
        workspace,
        {
          snapshot_id: snapshot
          for snapshot_id, snapshot in metadata.get("snapshots", {}).items()
          if snapshot.get("workspaceId") == workspace_id
        },
        list(metadata.get("artifacts", {}).get(workspace_id, [])),
      )
    except RuntimeError as exc:
      if str(exc) == "workspace_changed":
        raise StorageError("workspace_changed", "workspace changed during metadata commit") from exc
      raise
    self.defer_blob_cleanup(candidates)

  def delete_workspace_metadata(self, workspace_id: str) -> dict | None:
    workspace, candidates = self.database.delete_workspace_state(workspace_id)
    self.defer_blob_cleanup(candidates)
    return workspace

  def defer_blob_cleanup(self, candidates: set[str]) -> None:
    if not candidates:
      return
    with self._gc_lock:
      queued = time.time()
      for digest in candidates:
        self._gc_candidates.setdefault(digest, queued)

  def cleanup_unreferenced_blobs(self, candidates: set[str]) -> None:
    candidates = set(candidates) - (self.database.referenced_blobs(candidates) if candidates else set())
    for digest in candidates:
      blob = self.blob_path(digest)
      if blob.exists():
        blob.unlink()
    self.prune_preview_blobs(candidates)
    self.remove_empty_blob_directories()

  @staticmethod
  def _blob_gc_loop(store_ref) -> None:
    last_full_scan = time.time()
    while True:
      store = store_ref()
      if store is None:
        return
      interval = max(5, int(getattr(SETTINGS, "blob_gc_interval_seconds", 300)))
      grace = max(60, int(getattr(SETTINGS, "blob_gc_grace_seconds", 3600)))
      del store
      time.sleep(interval)
      store = store_ref()
      if store is None:
        return
      cutoff = time.time() - grace
      with store._gc_lock:
        ready = {digest for digest, queued in store._gc_candidates.items() if queued <= cutoff}
      if not ready:
        if time.time() - last_full_scan >= max(grace, 6 * 60 * 60):
          store.garbage_collect_blobs()
          last_full_scan = time.time()
      else:
        store.cleanup_unreferenced_blobs(ready)
        with store._gc_lock:
          for digest in ready:
            store._gc_candidates.pop(digest, None)
      del store

  @staticmethod
  def empty_metadata() -> dict:
    return {"workspaces": {}, "snapshots": {}, "artifacts": {}}

  def save_workspace_lifecycle(self, workspace: dict) -> None:
    with self.coordination_locks.hold(f"workspace:{workspace['id']}"):
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
      "sessionCount": int(workspace.get("sessionCount") or 0),
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
      "sessionCount": len(sessions),
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
