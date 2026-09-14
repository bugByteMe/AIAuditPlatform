from __future__ import annotations

import io
import json
import mimetypes
import os
import posixpath
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs


MAX_FILE_BYTES = 250 * 1024 * 1024
MAX_WORKSPACE_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_COUNT = 10_000
MAX_TEXT_PREVIEW_BYTES = 256 * 1024
BLOCKED_SUFFIXES = {".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".ps1", ".sh"}
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
    self.manifest_dir = root / "manifests"
    self.bundle_dir = root / "bundles"
    self.preview_dir = root / "previews"
    self.metadata_path = root / "metadata.json"
    self.ensure_layout()

  def ensure_layout(self) -> None:
    for path in [self.active_dir, self.blob_dir, self.manifest_dir, self.bundle_dir, self.preview_dir]:
      path.mkdir(parents=True, exist_ok=True)
    if not self.metadata_path.exists():
      self.save_metadata({"workspaces": {}, "snapshots": {}, "artifacts": {}})

  def load_metadata(self) -> dict:
    self.ensure_layout()
    try:
      return json.loads(self.metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
      raise StorageError("metadata_corrupt", "workspace metadata is corrupt") from exc

  def save_metadata(self, metadata: dict) -> None:
    self.root.mkdir(parents=True, exist_ok=True)
    tmp = self.metadata_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(self.metadata_path)

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
    sessions = self.public_workspace_sessions(workspace, metadata) or [
      {
        "id": f"session_{workspace_id}",
        "title": "Workspace setup",
        "status": "completed",
        "updated": workspace["updated"],
        "tokens": "0",
        "events": [["completed", "工作区已创建并生成初始快照。", "Workspace created and initial snapshot generated."]],
      }
    ]
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
      "latestSnapshotId": workspace.get("latestSnapshotId"),
      "sessions": sessions,
      "artifacts": self.workspace_artifacts(workspace_id, metadata),
      "files": self.file_tree(workspace_id),
    }

  def public_workspace_sessions(self, workspace: dict, metadata: dict) -> list[dict]:
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
            "events": [[event["type"], event["message"], event["message"]] for event in events],
          }
        )
      elif {"title", "status", "updated", "tokens", "events"}.issubset(item):
        sessions.append(item)
    return sessions

  def workspace_path(self, workspace_id: str) -> Path:
    return ensure_under_root(self.active_dir, self.active_dir / workspace_id)

  def create_workspace(self, user: dict, name: str, shared: bool, files: list[UploadedFile]) -> dict:
    if not files:
      raise StorageError("empty_upload", "at least one uploaded file is required")
    workspace_id = generated_id("ws")
    workspace_path = self.workspace_path(workspace_id)
    workspace_path.mkdir(parents=True, exist_ok=False)
    total = 0
    try:
      for item in files:
        relative = normalize_relative_path(item.path)
        size = len(item.content)
        if size > MAX_FILE_BYTES:
          raise StorageError("file_too_large", f"{relative} exceeds the per-file limit")
        total += size
        if total > MAX_WORKSPACE_BYTES:
          raise StorageError("workspace_too_large", "workspace exceeds the total size limit")
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
    workspace["sessions"] = [
      {
        "id": generated_id("chat"),
        "title": "Workspace setup",
        "status": "completed",
        "updated": timestamp,
        "tokens": "0",
        "events": [["completed", "工作区已创建并生成初始快照。", "Workspace created and initial snapshot generated."]],
      }
    ]
    self.save_metadata(metadata)
    return self.public_workspace(workspace, metadata)

  def update_workspace(self, workspace_id: str, user: dict, payload: dict) -> dict:
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
    workspace["updated"] = now_string()
    self.save_metadata(metadata)
    return self.public_workspace(workspace, metadata)

  def user_can_mutate_workspace(self, user: dict, workspace: dict) -> bool:
    return user["role"] == "system_admin" or workspace["owner"] == user["username"]

  def add_files_to_workspace(self, workspace_id: str, user: dict, files: list[UploadedFile]) -> dict:
    if not files:
      raise StorageError("empty_upload", "at least one uploaded file is required")
    metadata = self.load_metadata()
    workspace = self.get_workspace_from_metadata(metadata, workspace_id, user)
    if not self.user_can_mutate_workspace(user, workspace):
      raise StorageError("forbidden", "only the owner or system admin can add files")
    if workspace.get("locked"):
      raise StorageError("workspace_locked", "workspace has an active write lock")

    workspace_path = self.workspace_path(workspace_id)
    current_size = sum(path.stat().st_size for path in workspace_path.rglob("*") if path.is_file())
    current_count = sum(1 for path in workspace_path.rglob("*") if path.is_file())
    for item in files:
      relative = normalize_relative_path(item.path)
      size = len(item.content)
      if size > MAX_FILE_BYTES:
        raise StorageError("file_too_large", f"{relative} exceeds the per-file limit")
      current_size += size
      current_count += 1
      if current_size > MAX_WORKSPACE_BYTES:
        raise StorageError("workspace_too_large", "workspace exceeds the total size limit")
      if current_count > MAX_FILE_COUNT:
        raise StorageError("too_many_files", "workspace exceeds file count limit")
      destination = ensure_under_root(workspace_path, workspace_path / relative)
      destination.parent.mkdir(parents=True, exist_ok=True)
      destination.write_bytes(item.content)

    self.refresh_workspace_metadata(metadata, workspace, "file_upload", user["username"])
    self.save_metadata(metadata)
    return self.public_workspace(workspace, metadata)

  def delete_workspace_path(self, workspace_id: str, user: dict, raw_path: str) -> dict:
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

  def fork_workspace(self, workspace_id: str, user: dict, name: str | None = None) -> dict:
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
    fork["sessions"] = [
      {
        "id": generated_id("chat"),
        "title": "Fork created",
        "status": "completed",
        "updated": timestamp,
        "tokens": "0",
        "events": [["completed", "已从源快照复刻工作区。", "Workspace forked from the source snapshot."]],
      }
    ]
    self.save_metadata(metadata)
    return self.public_workspace(fork, metadata)

  def delete_workspace(self, workspace_id: str, user: dict) -> dict:
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
        manifest = self.manifest_dir / snapshot.get("manifestPath", "")
        if manifest.exists():
          manifest.unlink()
    shutil.rmtree(self.workspace_path(workspace_id), ignore_errors=True)
    shutil.rmtree(self.preview_dir / workspace_id, ignore_errors=True)
    self.save_metadata(metadata)
    return deleted

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
      "manifestPath": f"{snapshot_id}.json",
      "files": manifest,
    }
    manifest_path = ensure_under_root(self.manifest_dir, self.manifest_dir / f"{snapshot_id}.json")
    manifest_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    metadata["snapshots"][snapshot_id] = snapshot
    return snapshot

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
        timeout=45,
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
