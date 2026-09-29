from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from workspace_common import (
  MAX_FILE_COUNT, MAX_WORKSPACE_BYTES, StorageError, ensure_under_root, normalize_relative_path, stream_sha256,
)


CHECKPOINT_PROTOCOL_VERSION = 2
FINGERPRINT_FIELDS = ("size", "mtimeNs", "ctimeNs", "device", "inode", "mode")


def stat_fields(stat) -> dict:
  return {
    "size": int(stat.st_size),
    "mtime": int(stat.st_mtime),
    "mtimeNs": int(stat.st_mtime_ns),
    "ctimeNs": int(stat.st_ctime_ns),
    "device": int(stat.st_dev),
    "inode": int(stat.st_ino),
    "mode": int(stat.st_mode) & 0o777,
  }


def same_fingerprint(left: dict | None, right: dict | None) -> bool:
  return bool(left and right) and all(int(left.get(key) or 0) == int(right.get(key) or 0) for key in FINGERPRINT_FIELDS)


def trusted_baseline(entry: dict | None, observed: dict | None) -> bool:
  if not entry or not observed or any(not int(entry.get(key) or 0) for key in ("mtimeNs", "ctimeNs", "inode")):
    return False
  # st_dev can differ between mounts of the same shared filesystem. A mismatch
  # makes the entry dirty, but a zero legacy value never counts as trusted.
  return same_fingerprint(entry, observed)


def _atomic_json(path: Path, payload: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_suffix(path.suffix + ".tmp")
  temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
  temporary.replace(path)


def checkpoint_ref(run_id: str, kind: str) -> str:
  if kind not in {"baselines", "pre", "results"}:
    raise ValueError("invalid checkpoint state kind")
  safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in run_id).strip("._")
  if not safe or safe != run_id:
    raise ValueError("invalid run id")
  return f"checkpoint_state/{kind}/{safe}.json"


def resolve_ref(root: Path, reference: str) -> Path:
  relative = normalize_relative_path(reference)
  if not relative.startswith("checkpoint_state/"):
    raise StorageError("invalid_manifest", "invalid checkpoint state reference")
  return ensure_under_root(root, root / relative)


def write_baseline(root: Path, run_id: str, snapshot_id: str | None, files: dict) -> str:
  reference = checkpoint_ref(run_id, "baselines")
  _atomic_json(resolve_ref(root, reference), {
    "version": CHECKPOINT_PROTOCOL_VERSION,
    "baseSnapshotId": snapshot_id,
    "files": files,
  })
  return reference


def read_json_ref(root: Path, reference: str) -> dict:
  path = resolve_ref(root, reference)
  if not path.is_file():
    raise StorageError("missing_checkpoint_baseline", "checkpoint state file is unavailable")
  value = json.loads(path.read_text(encoding="utf-8"))
  if not isinstance(value, dict):
    raise StorageError("invalid_manifest", "checkpoint state must be an object")
  return value


def capture_pre_run(root: Path, workspace_path: Path, run: dict) -> str | None:
  baseline_ref = str(run.get("checkpointBaseRef") or "")
  if not baseline_ref:
    return None
  baseline = read_json_ref(root, baseline_ref)
  if int(baseline.get("version") or 0) != CHECKPOINT_PROTOCOL_VERSION or not isinstance(baseline.get("files"), dict):
    raise StorageError("invalid_manifest", "invalid checkpoint baseline")
  observed = {}
  total_size = 0
  for path in sorted(item for item in workspace_path.rglob("*") if item.is_file() and not item.is_symlink()):
    if len(observed) >= MAX_FILE_COUNT:
      raise StorageError("too_many_files", "workspace exceeds file count limit")
    relative = normalize_relative_path(path.relative_to(workspace_path).as_posix())
    entry = stat_fields(path.stat())
    total_size += entry["size"]
    if total_size > MAX_WORKSPACE_BYTES:
      raise StorageError("workspace_too_large", "workspace exceeds size limit")
    observed[relative] = entry
  reference = checkpoint_ref(str(run["id"]), "pre")
  _atomic_json(resolve_ref(root, reference), {
    "version": CHECKPOINT_PROTOCOL_VERSION,
    "baseSnapshotId": baseline.get("baseSnapshotId"),
    "files": observed,
  })
  return reference


def _store_blob(path: Path, blob_dir: Path, digest: str) -> bool:
  blob = ensure_under_root(blob_dir, blob_dir / digest[:2] / digest[2:4] / digest)
  if blob.exists():
    return False
  blob.parent.mkdir(parents=True, exist_ok=True)
  with tempfile.NamedTemporaryFile(dir=blob.parent, suffix=".tmp", delete=False) as handle:
    temporary = Path(handle.name)
  try:
    shutil.copyfile(path, temporary)
    try:
      temporary.replace(blob)
    except FileExistsError:
      return False
    return True
  finally:
    temporary.unlink(missing_ok=True)


def _hash_stable_file(path: Path, blob_dir: Path) -> tuple[dict, bool]:
  for _ in range(3):
    before = stat_fields(path.stat())
    digest = stream_sha256(path)
    after = stat_fields(path.stat())
    if same_fingerprint(before, after):
      copied = _store_blob(path, blob_dir, digest)
      return {"path": "", "checksum": f"sha256:{digest}", "blob": digest, **after}, copied
  raise StorageError("workspace_changed", f"file changed repeatedly during checkpoint: {path.name}")


def scan_delta(root: Path, workspace_path: Path, blob_dir: Path, run: dict) -> dict | None:
  baseline_ref = str(run.get("checkpointBaseRef") or "")
  pre_ref = str(run.get("checkpointPreRef") or "")
  if not baseline_ref or not pre_ref:
    return None
  started = time.monotonic()
  baseline = read_json_ref(root, baseline_ref)
  pre = read_json_ref(root, pre_ref)
  if int(baseline.get("version") or 0) != CHECKPOINT_PROTOCOL_VERSION or int(pre.get("version") or 0) != CHECKPOINT_PROTOCOL_VERSION:
    raise StorageError("invalid_manifest", "unsupported checkpoint state version")
  before = pre.get("files") or {}
  base_files = baseline.get("files") or {}
  if not isinstance(before, dict) or not isinstance(base_files, dict):
    raise StorageError("invalid_manifest", "checkpoint state files must contain file maps")
  upserts, current_paths = {}, set()
  total_size = hashed_bytes = copied_bytes = 0
  for path in sorted(item for item in workspace_path.rglob("*") if item.is_file() and not item.is_symlink()):
    if len(current_paths) >= MAX_FILE_COUNT:
      raise StorageError("too_many_files", "workspace exceeds file count limit")
    relative = normalize_relative_path(path.relative_to(workspace_path).as_posix())
    current_paths.add(relative)
    observed = stat_fields(path.stat())
    total_size += observed["size"]
    if total_size > MAX_WORKSPACE_BYTES:
      raise StorageError("workspace_too_large", "workspace exceeds size limit")
    baseline_entry = base_files.get(relative)
    pre_entry = before.get(relative)
    if trusted_baseline(baseline_entry, pre_entry) and same_fingerprint(pre_entry, observed):
      digest = str(baseline_entry.get("blob") or "")
      blob = ensure_under_root(blob_dir, blob_dir / digest[:2] / digest[2:4] / digest) if digest else None
      if blob and blob.is_file():
        continue
    entry, copied = _hash_stable_file(path, blob_dir)
    entry["path"] = relative
    upserts[relative] = entry
    hashed_bytes += entry["size"]
    if copied:
      copied_bytes += entry["size"]
  deletes = sorted(set(base_files) - current_paths)
  return {
    "version": CHECKPOINT_PROTOCOL_VERSION,
    "baseSnapshotId": baseline.get("baseSnapshotId"),
    "upserts": upserts,
    "deletes": deletes,
    "observedFileCount": len(current_paths),
    "observedSizeBytes": total_size,
    "stats": {
      "scannedFiles": len(current_paths), "hashedBytes": hashed_bytes, "copiedBytes": copied_bytes,
      "upsertedPaths": len(upserts), "deletedPaths": len(deletes),
      "durationMs": int((time.monotonic() - started) * 1000),
    },
  }


def write_result(root: Path, run_id: str, result: dict) -> str:
  reference = checkpoint_ref(run_id, "results")
  _atomic_json(resolve_ref(root, reference), result)
  return reference


def cleanup_refs(root: Path, *references: str | None) -> None:
  for reference in references:
    if reference:
      resolve_ref(root, reference).unlink(missing_ok=True)
