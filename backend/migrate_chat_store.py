from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from chat_store import ChatStore


def main() -> None:
  parser = argparse.ArgumentParser(description="Import legacy chat JSON/JSONL into normalized storage")
  parser.add_argument("--storage-root", required=True, type=Path)
  parser.add_argument("--database-url", default=os.environ.get("AI_AUDIT_DATABASE_URL", ""))
  args = parser.parse_args()
  if not args.database_url:
    raise SystemExit("AI_AUDIT_DATABASE_URL or --database-url is required")
  store = ChatStore(args.storage_root / "chat", args.database_url)
  sessions = store.sessions()
  runs = store.runs()
  missing_sessions = sorted({str(run.get("sessionId") or "") for run in runs.values()} - set(sessions))
  report = {"sessions": len(sessions), "runs": len(runs), "missingRunSessions": missing_sessions}
  print(json.dumps(report, ensure_ascii=False, sort_keys=True))
  if missing_sessions:
    raise SystemExit("migration validation failed: runs reference missing sessions")


if __name__ == "__main__":
  main()
