from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from upload_database import UploadDatabase


def main() -> None:
  parser = argparse.ArgumentParser(description="Import upload_sessions.json into SQL upload coordination")
  parser.add_argument("--storage-root", required=True, type=Path)
  parser.add_argument("--database-url", default=os.environ.get("AI_AUDIT_DATABASE_URL", ""))
  args = parser.parse_args()
  if not args.database_url:
    raise SystemExit("AI_AUDIT_DATABASE_URL or --database-url is required")
  path = args.storage_root / "upload_sessions.json"
  if not path.is_file():
    raise SystemExit(f"upload JSON not found: {path}")
  source = (json.loads(path.read_text(encoding="utf-8")) or {}).get("sessions") or {}
  database = UploadDatabase(args.storage_root, args.database_url)
  database.import_sessions(source)
  target = database.all()
  source_ids, target_ids = sorted(source), sorted(target)
  report = {"source": len(source_ids), "target": len(target_ids), "idsVerified": source_ids == target_ids}
  print(json.dumps(report, ensure_ascii=False, sort_keys=True))
  if not report["idsVerified"]:
    raise SystemExit("upload migration validation failed")


if __name__ == "__main__":
  main()
