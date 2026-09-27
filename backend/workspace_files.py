from __future__ import annotations

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

from config import SETTINGS
from postgres_workspace_database import PostgresWorkspaceDatabase
from workspace_database import WorkspaceDatabase
from workspace_common import (
  BLOCKED_SUFFIXES, MAX_FILE_BYTES, MAX_FILE_COUNT, MAX_TEXT_PREVIEW_BYTES, MAX_WORKSPACE_BYTES,
  OFFICE_SUFFIXES, TEXT_SUFFIXES, StorageError, UploadedFile, ensure_under_root, generated_id, human_size,
  normalize_relative_path, now_string, read_prefix, stream_sha256,
)

class WorkspaceFilesMixin:
  def workspace_artifacts(self, workspace_id: str, metadata: dict | None = None) -> list[dict]:
    metadata = metadata or self.load_workspace_metadata(workspace_id)
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

  def file_tree(self, workspace_id: str, parent: str = "", depth: int = 3) -> list[dict]:
    workspace_path = self.workspace_path(workspace_id)
    if not workspace_path.exists():
      return []
    if depth < 1 or depth > 3:
      raise StorageError("bad_request", "file tree depth must be between 1 and 3")
    parent_path = normalize_relative_path(parent) if parent else ""
    tree_root = ensure_under_root(workspace_path, workspace_path / parent_path) if parent_path else workspace_path
    if not tree_root.exists():
      raise StorageError("not_found", "folder not found")
    if not tree_root.is_dir():
      raise StorageError("bad_request", "file tree path must be a folder")

    rows = []

    def visible_children(directory: Path) -> list[Path]:
      return sorted(
        (item for item in directory.iterdir() if not item.is_symlink()),
        key=lambda item: (not item.is_dir(), item.name.casefold(), item.name),
      )

    def append_children(directory: Path, remaining_depth: int) -> None:
      for item in visible_children(directory):
        ensure_under_root(workspace_path, item)
        relative = item.relative_to(workspace_path).as_posix()
        parts = relative.split("/")
        if item.is_dir():
          has_children = bool(visible_children(item))
          rows.append({
            "name": item.name,
            "path": relative,
            "type": "folder",
            "level": len(parts) - 1,
            "hasChildren": has_children,
            "childrenLoaded": not has_children or remaining_depth > 1,
          })
          if has_children and remaining_depth > 1:
            append_children(item, remaining_depth - 1)
        elif item.is_file():
          rows.append({"name": item.name, "path": relative, "type": "file", "level": len(parts) - 1, "size": human_size(item.stat().st_size)})

    append_children(tree_root, depth)
    return rows

  def workspace_file_path(self, workspace_id: str, raw_path: str) -> Path:
    workspace_path = self.workspace_path(workspace_id)
    relative = normalize_relative_path(raw_path)
    file_path = ensure_under_root(workspace_path, workspace_path / relative)
    if not file_path.is_file():
      raise StorageError("not_found", "file not found")
    return file_path

  def file_metadata(self, workspace_id: str, raw_path: str) -> dict:
    metadata = self.file_response_metadata(workspace_id, raw_path)
    digest = stream_sha256(self.workspace_file_path(workspace_id, raw_path))
    return {**metadata, "checksum": f"sha256:{digest}", "blob": digest}

  def file_response_metadata(self, workspace_id: str, raw_path: str) -> dict:
    file_path = self.workspace_file_path(workspace_id, raw_path)
    relative = file_path.relative_to(self.workspace_path(workspace_id)).as_posix()
    stat = file_path.stat()
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return {
      "path": relative,
      "name": file_path.name,
      "sizeBytes": stat.st_size,
      "size": human_size(stat.st_size),
      "contentType": content_type,
      "suffix": file_path.suffix.lower(),
    }

  def preview_metadata(self, workspace_id: str, raw_path: str) -> dict:
    metadata = self.file_metadata(workspace_id, raw_path)
    file_path = self.workspace_file_path(workspace_id, raw_path)
    suffix = metadata["suffix"]
    content_type = metadata["contentType"]
    if suffix in TEXT_SUFFIXES or content_type.startswith("text/"):
      raw = read_prefix(file_path, MAX_TEXT_PREVIEW_BYTES)
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

  def build_download_zip(self, workspace_id: str, user: dict, mode: str, selected_paths: list[str]) -> tuple[str, Path]:
    self.get_workspace(workspace_id, user)
    metadata = self.load_workspace_metadata(workspace_id)
    workspace = metadata["workspaces"][workspace_id]
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
      selected_entries = [
        entry
        for entry in entries
        if any(entry["path"] == path or entry["path"].startswith(f"{path}/") for path in selected)
      ]
      matched = {
        path
        for path in selected
        if any(entry["path"] == path or entry["path"].startswith(f"{path}/") for entry in entries)
      }
      missing = next(
        (
          path
          for path in selected
          if path not in matched and not ensure_under_root(self.workspace_path(workspace_id), self.workspace_path(workspace_id) / path).is_dir()
        ),
        None,
      )
      if missing:
        raise StorageError("bad_request", f"selected path is not downloadable: {missing}")
      entries = selected_entries

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
    self.bundle_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="download-", suffix=".zip", dir=self.bundle_dir, delete=False) as handle:
      bundle_path = Path(handle.name)
    try:
      with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{root_name}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        for entry in entries:
          source_entry = latest["files"].get(entry["path"])
          if not source_entry:
            continue
          archive.write(self.blob_path(source_entry["blob"]), f"{root_name}/files/{entry['path']}")
      return f"{root_name}.zip", bundle_path
    except Exception:
      bundle_path.unlink(missing_ok=True)
      raise
