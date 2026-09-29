from __future__ import annotations

from contextlib import closing

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from workspace_common import MAX_FILE_COUNT, MAX_WORKSPACE_BYTES, StorageError


def _paths(delta: dict) -> list[str]:
  return sorted(set(delta.get("upserts") or {}) | set(delta.get("deletes") or []))


def _next_counts(workspace: dict, current: dict, delta: dict) -> tuple[int, int]:
  count = int(workspace.get("fileCount") or 0)
  size = int(workspace.get("sizeBytes") or 0)
  for path, entry in (delta.get("upserts") or {}).items():
    old = current.get(path)
    if old is None:
      count += 1
    else:
      size -= int(old.get("size") or 0)
    size += int(entry.get("size") or 0)
  for path in delta.get("deletes") or []:
    old = current.get(path)
    if old is not None:
      count -= 1
      size -= int(old.get("size") or 0)
  count, size = max(0, count), max(0, size)
  if count > MAX_FILE_COUNT:
    raise StorageError("too_many_files", "workspace exceeds file count limit")
  if size > MAX_WORKSPACE_BYTES:
    raise StorageError("workspace_too_large", "workspace exceeds size limit")
  return count, size


def _sqlite_upsert(connection, database, workspace_id: str, path: str, slot: str, entry: dict, snapshot_id: str | None) -> None:
  connection.execute(
    "INSERT OR REPLACE INTO workspace_file_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    database._entry_tuple(workspace_id, path, slot, entry, snapshot_id),
  )


def apply_sqlite_delta(database, workspace: dict, delta: dict, snapshot: dict, artifacts: list[dict]) -> set[str]:
  workspace_id, paths = str(workspace["id"]), _paths(delta)
  with closing(database.connect()) as connection, connection:
    connection.execute("BEGIN IMMEDIATE")
    header = connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if not header:
      raise RuntimeError("workspace_changed")
    placeholders = ",".join("?" for _ in paths)
    rows = list(connection.execute(
      f"SELECT * FROM workspace_file_versions WHERE workspace_id = ? AND path IN ({placeholders})", (workspace_id, *paths),
    )) if paths else []
    current_rows = {row["path"]: row for row in rows if row["slot"] == "current"}
    previous_rows = {row["path"]: row for row in rows if row["slot"] == "previous"}
    current = {path: database._file_entry(row) for path, row in current_rows.items()}
    old_refs = {row["blob"] for row in rows}
    snapshot["parentSnapshotId"] = header["latest_snapshot_id"]
    connection.execute(
      "INSERT INTO snapshots(id, workspace_id, parent_snapshot_id, reason, actor, created) VALUES (?, ?, ?, ?, ?, ?)",
      (snapshot["id"], workspace_id, snapshot.get("parentSnapshotId"), snapshot["reason"], snapshot["actor"], snapshot["created"]),
    )
    for path in paths:
      old = current.get(path)
      incoming = (delta.get("upserts") or {}).get(path)
      if incoming is None:
        if old is not None:
          _sqlite_upsert(connection, database, workspace_id, path, "previous", old, current_rows[path]["snapshot_id"])
          connection.execute("DELETE FROM workspace_file_versions WHERE workspace_id=? AND path=? AND slot='current'", (workspace_id, path))
        continue
      if old is not None and old.get("blob") != incoming.get("blob"):
        _sqlite_upsert(connection, database, workspace_id, path, "previous", old, current_rows[path]["snapshot_id"])
      elif old is None and path in previous_rows and previous_rows[path]["blob"] == incoming.get("blob"):
        connection.execute("DELETE FROM workspace_file_versions WHERE workspace_id=? AND path=? AND slot='previous'", (workspace_id, path))
      _sqlite_upsert(connection, database, workspace_id, path, "current", incoming, snapshot["id"])
    count, size = _next_counts(database._workspace_entry(header), current, delta)
    workspace.update({"fileCount": count, "sizeBytes": size, "latestSnapshotId": snapshot["id"], "updated": snapshot["created"]})
    connection.execute(
      "UPDATE workspaces SET file_count=?, size_bytes=?, latest_snapshot_id=?, updated=? WHERE id=?",
      (count, size, snapshot["id"], snapshot["created"], workspace_id),
    )
    connection.execute("DELETE FROM artifacts WHERE workspace_id = ?", (workspace_id,))
    for item in artifacts:
      connection.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
        workspace_id, item["path"], item["status"], int(item.get("sizeBytes") or 0), item.get("size") or "0 B",
        item.get("checksum") or "", item.get("blob"), item.get("timestamp") or "",
      ))
    new_refs = {row[0] for row in connection.execute(
      f"SELECT blob FROM workspace_file_versions WHERE workspace_id = ? AND path IN ({placeholders})", (workspace_id, *paths),
    )} if paths else set()
    return old_refs - new_refs


def apply_postgres_delta(database, workspace: dict, delta: dict, snapshot: dict, artifacts: list[dict]) -> set[str]:
  workspace_id, paths = str(workspace["id"]), _paths(delta)
  with database.engine.begin() as connection:
    header = connection.execute(select(database.workspaces).where(database.workspaces.c.id == workspace_id).with_for_update()).mappings().first()
    if not header:
      raise RuntimeError("workspace_changed")
    rows = list(connection.execute(select(database.file_version_rows).where(
      database.file_version_rows.c.workspace_id == workspace_id, database.file_version_rows.c.path.in_(paths),
    )).mappings()) if paths else []
    current_rows = {row["path"]: row for row in rows if row["slot"] == "current"}
    previous_rows = {row["path"]: row for row in rows if row["slot"] == "previous"}
    current = {path: database._entry(row) for path, row in current_rows.items()}
    old_refs = {row["blob"] for row in rows}
    snapshot["parentSnapshotId"] = header["latest_snapshot_id"]
    connection.execute(insert(database.snapshots).values(
      id=snapshot["id"], workspace_id=workspace_id, parent_snapshot_id=snapshot.get("parentSnapshotId"),
      reason=snapshot["reason"], actor=snapshot["actor"], created=snapshot["created"],
    ))
    for path in paths:
      old, incoming = current.get(path), (delta.get("upserts") or {}).get(path)
      if incoming is None:
        if old is not None:
          values = database._file_values(workspace_id, path, "previous", old, current_rows[path]["snapshot_id"])
          statement = pg_insert(database.file_version_rows).values(**values)
          connection.execute(statement.on_conflict_do_update(
            index_elements=[database.file_version_rows.c.workspace_id, database.file_version_rows.c.path, database.file_version_rows.c.slot],
            set_={key: value for key, value in values.items() if key not in {"workspace_id", "path", "slot"}},
          ))
          connection.execute(delete(database.file_version_rows).where(
            database.file_version_rows.c.workspace_id == workspace_id, database.file_version_rows.c.path == path,
            database.file_version_rows.c.slot == "current",
          ))
        continue
      if old is not None and old.get("blob") != incoming.get("blob"):
        previous = database._file_values(workspace_id, path, "previous", old, current_rows[path]["snapshot_id"])
        statement = pg_insert(database.file_version_rows).values(**previous)
        connection.execute(statement.on_conflict_do_update(
          index_elements=[database.file_version_rows.c.workspace_id, database.file_version_rows.c.path, database.file_version_rows.c.slot],
          set_={key: value for key, value in previous.items() if key not in {"workspace_id", "path", "slot"}},
        ))
      elif old is None and path in previous_rows and previous_rows[path]["blob"] == incoming.get("blob"):
        connection.execute(delete(database.file_version_rows).where(
          database.file_version_rows.c.workspace_id == workspace_id, database.file_version_rows.c.path == path,
          database.file_version_rows.c.slot == "previous",
        ))
      values = database._file_values(workspace_id, path, "current", incoming, snapshot["id"])
      statement = pg_insert(database.file_version_rows).values(**values)
      connection.execute(statement.on_conflict_do_update(
        index_elements=[database.file_version_rows.c.workspace_id, database.file_version_rows.c.path, database.file_version_rows.c.slot],
        set_={key: value for key, value in values.items() if key not in {"workspace_id", "path", "slot"}},
      ))
    count, size = _next_counts(database._workspace_entry(header), current, delta)
    workspace.update({"fileCount": count, "sizeBytes": size, "latestSnapshotId": snapshot["id"], "updated": snapshot["created"]})
    connection.execute(update(database.workspaces).where(database.workspaces.c.id == workspace_id).values(
      file_count=count, size_bytes=size, latest_snapshot_id=snapshot["id"], updated=snapshot["created"],
    ))
    connection.execute(delete(database.artifacts).where(database.artifacts.c.workspace_id == workspace_id))
    if artifacts:
      connection.execute(insert(database.artifacts), [{
        "workspace_id": workspace_id, "path": item["path"], "status": item["status"],
        "size_bytes": int(item.get("sizeBytes") or 0), "size_label": item.get("size") or "0 B",
        "checksum": item.get("checksum") or "", "blob": item.get("blob"), "timestamp": item.get("timestamp") or "",
      } for item in artifacts])
    new_refs = set(connection.execute(select(database.file_version_rows.c.blob).where(
      database.file_version_rows.c.workspace_id == workspace_id, database.file_version_rows.c.path.in_(paths),
    )).scalars()) if paths else set()
    return old_refs - new_refs
