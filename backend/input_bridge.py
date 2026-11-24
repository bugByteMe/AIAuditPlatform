"""Run-scoped mailbox and image snapshots; no control-plane secrets enter containers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from user_input_schema import MAX_IMAGE_BYTES, validate_question

REQUEST_ID = re.compile(r"[a-f0-9]{32}\Z")


def atomic_json(path, value):
  with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as handle:
    temporary = Path(handle.name)
    json.dump(value, handle, ensure_ascii=False)
  try:
    os.chmod(temporary, 0o600)
    if os.geteuid() == 0:
      parent = path.parent.stat()
      os.chown(temporary, parent.st_uid, parent.st_gid)
    temporary.replace(path)
  finally:
    temporary.unlink(missing_ok=True)


def read_regular(path, limit):
  import stat
  descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
  with os.fdopen(descriptor, "rb") as handle:
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
      raise ValueError("expected a regular file")
    data = handle.read(limit + 1)
  if len(data) > limit:
    raise ValueError("file exceeds size limit")
  return data


def snapshot_image(workspace, source, blob_dir):
  relative = source.removeprefix("/workspace/")
  if Path(relative).is_absolute():
    raise ValueError("image must be inside /workspace")
  parts = Path(relative).parts
  if not parts or any(part in {"..", "."} for part in parts):
    raise ValueError("image escapes workspace")
  # Open every component relative to an already-open directory. A concurrent
  # workspace rename/symlink swap must not redirect a host read outside it.
  directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
  try:
    for component in parts[:-1]:
      following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
      os.close(directory)
      directory = following
    descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    import stat
    with os.fdopen(descriptor, "rb") as handle:
      if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        raise ValueError("image must be a regular file")
      data = handle.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
      raise ValueError("image exceeds 5 MiB")
  finally:
    os.close(directory)
  path = Path(relative)
  if data.startswith(b"\x89PNG\r\n\x1a\n"):
    mime = "image/png"
  elif data.startswith(b"\xff\xd8\xff"):
    mime = "image/jpeg"
  elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
    mime = "image/webp"
  else:
    raise ValueError("only PNG, JPEG and WebP images are supported")
  digest = hashlib.sha256(data).hexdigest()
  target = blob_dir / digest[:2] / digest[2:4] / digest
  target.parent.mkdir(parents=True, exist_ok=True)
  with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
    handle.write(data)
    temporary = Path(handle.name)
  temporary.replace(target)
  return {"blob": digest, "contentType": mime, "name": path.name, "size": len(data)}


class InputBridge:
  def __init__(self, root, run_id):
    if not re.fullmatch(r"[\w-]+", run_id):
      raise ValueError("invalid run id")
    self.path = Path(root) / "input_bridge" / run_id

  def prepare(self, uid, gid):
    self.path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(self.path, 0o700)
    if os.geteuid() == 0:
      os.chown(self.path, uid, gid)

  def requests(self, workspace, blob_dir, seen):
    for path in sorted(self.path.glob("*.request.json")):
      request_id = path.name.removesuffix(".request.json")
      if not REQUEST_ID.fullmatch(request_id) or request_id in seen:
        continue
      try:
        question = validate_question(json.loads(read_regular(path, 1024 * 1024)))
        images = [snapshot_image(workspace, source, blob_dir) for source in question.pop("images")]
        yield {"type": "user_input_requested", "message": question["title"],
               "inputRequest": {"id": request_id, "question": question, "images": images}}
      except (ValueError, OSError) as exc:
        self.respond(request_id, {"requestId": request_id, "status": "error", "message": str(exc)})
      seen.add(request_id)

  def respond(self, request_id, result):
    if not REQUEST_ID.fullmatch(request_id):
      raise ValueError("invalid input request id")
    request = self.path / f"{request_id}.request.json"
    if not request.is_file() or request.is_symlink():
      raise ValueError("input request is no longer available")
    response = self.path / f"{request_id}.response.json"
    if response.exists():
      if json.loads(read_regular(response, 64 * 1024)) != result:
        raise ValueError("conflicting input response")
      return
    atomic_json(response, result)

  def pending(self, seen):
    return any(not (self.path / f"{request_id}.response.json").exists() for request_id in seen)
