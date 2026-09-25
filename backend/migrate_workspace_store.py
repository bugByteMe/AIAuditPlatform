from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from sqlalchemy import func, insert, select

from postgres_workspace_database import PostgresWorkspaceDatabase


TABLES = (
  "workspaces",
  "workspace_sessions",
  "snapshots",
  "workspace_file_versions",
  "artifacts",
)


def read_source(path: Path) -> dict[str, list[dict]]:
  if not path.is_file():
    raise SystemExit(f"workspace database not found: {path}")
  connection = sqlite3.connect(path)
  connection.row_factory = sqlite3.Row
  try:
    connection.execute("PRAGMA query_only = ON")
    connection.execute("BEGIN")
    return {
      table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
      for table in TABLES
    }
  finally:
    connection.close()


def canonical_rows(rows: list[dict]) -> list[str]:
  return sorted(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)


def main() -> None:
  parser = argparse.ArgumentParser(description="Import SQLite workspace metadata into PostgreSQL")
  parser.add_argument("--storage-root", required=True, type=Path)
  parser.add_argument("--database-url", default=os.environ.get("AI_AUDIT_DATABASE_URL", ""))
  args = parser.parse_args()
  if not args.database_url:
    raise SystemExit("AI_AUDIT_DATABASE_URL or --database-url is required")

  source = read_source(args.storage_root / "workspace.sqlite3")
  database = PostgresWorkspaceDatabase(args.database_url)
  targets = {
    "workspaces": database.workspaces,
    "workspace_sessions": database.workspace_sessions,
    "snapshots": database.snapshots,
    "workspace_file_versions": database.file_version_rows,
    "artifacts": database.artifacts,
  }
  source["workspaces"] = [
    {**row, "shared": bool(row["shared"]), "locked": bool(row["locked"]), "run_lock_enabled": bool(row["run_lock_enabled"])}
    for row in source["workspaces"]
  ]

  with database.engine.begin() as connection:
    populated = {
      name: int(connection.execute(select(func.count()).select_from(table)).scalar_one())
      for name, table in targets.items()
    }
    if any(populated.values()):
      raise SystemExit(f"target workspace tables are not empty: {json.dumps(populated, sort_keys=True)}")
    for name in TABLES:
      if source[name]:
        connection.execute(insert(targets[name]), source[name])

  with database.engine.connect() as connection:
    target_rows = {
      name: [dict(row) for row in connection.execute(select(table)).mappings()]
      for name, table in targets.items()
    }
    target_counts = {
      name: len(rows) for name, rows in target_rows.items()
    }
  source_counts = {name: len(rows) for name, rows in source.items()}
  content_verified = all(canonical_rows(source[name]) == canonical_rows(target_rows[name]) for name in TABLES)
  report = {"source": source_counts, "target": target_counts, "contentVerified": content_verified}
  print(json.dumps(report, ensure_ascii=False, sort_keys=True))
  if source_counts != target_counts or not content_verified:
    raise SystemExit("migration validation failed: source and target workspace metadata differ")


if __name__ == "__main__":
  main()
