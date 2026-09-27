from __future__ import annotations

import io
import posixpath
import time
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs

from config import SETTINGS

MAX_FILE_BYTES = SETTINGS.max_file_bytes
MAX_WORKSPACE_BYTES = SETTINGS.max_workspace_bytes
MAX_FILE_COUNT = SETTINGS.max_file_count
MAX_TEXT_PREVIEW_BYTES = SETTINGS.max_text_preview_bytes
BLOCKED_SUFFIXES = {suffix.lower() for suffix in SETTINGS.blocked_upload_suffixes}
TEXT_SUFFIXES = {".csv", ".css", ".html", ".js", ".json", ".log", ".md", ".py", ".txt", ".ts", ".xml", ".yaml", ".yml"}
OFFICE_SUFFIXES = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
FILE_STREAM_CHUNK_BYTES = 1024 * 1024
BUNDLE_TEMP_TTL_SECONDS = 60 * 60

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

def stream_sha256(path: Path, chunk_bytes: int = FILE_STREAM_CHUNK_BYTES) -> str:
  digest = sha256()
  with path.open("rb") as source:
    while chunk := source.read(chunk_bytes):
      digest.update(chunk)
  return digest.hexdigest()

def read_prefix(path: Path, limit: int) -> bytes:
  with path.open("rb") as source:
    return source.read(limit)

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
