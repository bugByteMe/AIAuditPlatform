from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from checkpoint_delta import normalize_workspace_filenames
from config import SETTINGS
from workspace_common import StorageError


WORKSPACE_ID = re.compile(r"[A-Za-z0-9_.-]+")


def main() -> None:
  parser = argparse.ArgumentParser(
    description="Repair non-UTF-8 GB18030 filenames in an active workspace (dry-run by default)",
  )
  parser.add_argument("workspace_id")
  parser.add_argument("--storage-root", type=Path, default=SETTINGS.workspace_storage_dir)
  parser.add_argument("--apply", action="store_true", help="apply the proposed renames")
  args = parser.parse_args()

  if not WORKSPACE_ID.fullmatch(args.workspace_id):
    raise SystemExit("invalid workspace id")
  workspace_path = args.storage_root / "active" / args.workspace_id
  if not workspace_path.is_dir():
    raise SystemExit(f"workspace directory is unavailable: {workspace_path}")

  try:
    report = normalize_workspace_filenames(workspace_path, apply=args.apply)
  except StorageError as exc:
    print(json.dumps({"applied": False, "error": exc.code, "message": exc.message}, ensure_ascii=True))
    raise SystemExit(1) from exc
  print(json.dumps({"workspaceId": args.workspace_id, **report}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
  main()
