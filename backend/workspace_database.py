from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path


SCHEMA_VERSION = 3


class WorkspaceDatabase:
  """Transactional workspace metadata; large file content remains in blob storage."""

  def __init__(self, root: Path):
    self.root = root
    self.path = root / "workspace.sqlite3"
    self.legacy_path = root / "metadata.json"
    self.root.mkdir(parents=True, exist_ok=True)
    self._reject_populated_legacy_store()
    self.ensure_schema()

  def _reject_populated_legacy_store(self) -> None:
    if self.path.exists() or not self.legacy_path.exists():
      return
    try:
      legacy = json.loads(self.legacy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
      raise RuntimeError("legacy workspace metadata exists but cannot be read; use a fresh workspace storage directory") from exc
    populated = any(bool(value) for value in legacy.values()) if isinstance(legacy, dict) else bool(legacy)
    if populated:
      raise RuntimeError(
        "populated legacy workspace metadata is not supported by the SQLite workspace store; "
        "configure a fresh workspace storage directory"
      )

  def connect(self) -> sqlite3.Connection:
    connection = sqlite3.connect(self.path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    return connection

  def ensure_schema(self) -> None:
    with closing(self.connect()) as connection, connection:
      connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_info (
          version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workspaces (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          owner TEXT NOT NULL,
          group_name TEXT NOT NULL DEFAULT '',
          shared INTEGER NOT NULL DEFAULT 0,
          locked INTEGER NOT NULL DEFAULT 0,
          run_lock_enabled INTEGER NOT NULL DEFAULT 0,
          created TEXT NOT NULL,
          updated TEXT NOT NULL,
          file_count INTEGER NOT NULL DEFAULT 0,
          size_bytes INTEGER NOT NULL DEFAULT 0,
          latest_snapshot_id TEXT,
          initial_snapshot_id TEXT,
          source_workspace_id TEXT,
          source_snapshot_id TEXT,
          active_run_id TEXT,
          active_upload_id TEXT
        );

        CREATE TABLE IF NOT EXISTS workspace_sessions (
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          session_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          PRIMARY KEY (workspace_id, session_id)
        );

        CREATE INDEX IF NOT EXISTS workspaces_shared_group
          ON workspaces(shared, group_name);

        CREATE TABLE IF NOT EXISTS snapshots (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          parent_snapshot_id TEXT,
          reason TEXT NOT NULL,
          actor TEXT NOT NULL,
          created TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS snapshots_workspace_created
          ON snapshots(workspace_id, created);

        CREATE TABLE IF NOT EXISTS workspace_file_versions (
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          path TEXT NOT NULL,
          slot TEXT NOT NULL CHECK(slot IN ('current', 'previous')),
          checksum TEXT NOT NULL,
          blob TEXT NOT NULL,
          size INTEGER NOT NULL,
          mtime INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL DEFAULT 0,
          ctime_ns INTEGER NOT NULL DEFAULT 0,
          device INTEGER NOT NULL DEFAULT 0,
          inode INTEGER NOT NULL DEFAULT 0,
          mode INTEGER NOT NULL,
          snapshot_id TEXT,
          PRIMARY KEY (workspace_id, path, slot)
        );

        CREATE INDEX IF NOT EXISTS workspace_file_versions_blob
          ON workspace_file_versions(blob);

        CREATE TABLE IF NOT EXISTS artifacts (
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          path TEXT NOT NULL,
          status TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          size_label TEXT NOT NULL,
          checksum TEXT NOT NULL,
          blob TEXT,
          timestamp TEXT NOT NULL,
          PRIMARY KEY (workspace_id, path)
        );
        """
      )
      row = connection.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
      if row is None:
        connection.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
      elif int(row["version"]) in {1, 2}:
        columns = {column[1] for column in connection.execute("PRAGMA table_info(workspaces)")}
        if "run_lock_enabled" not in columns:
          connection.execute("ALTER TABLE workspaces ADD COLUMN run_lock_enabled INTEGER NOT NULL DEFAULT 0")
        file_columns = {column[1] for column in connection.execute("PRAGMA table_info(workspace_file_versions)")}
        for name in ("mtime_ns", "ctime_ns", "device", "inode"):
          if name not in file_columns:
            connection.execute(f"ALTER TABLE workspace_file_versions ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        connection.execute("UPDATE schema_info SET version = ?", (SCHEMA_VERSION,))
      elif int(row["version"]) != SCHEMA_VERSION:
        raise RuntimeError(f"unsupported workspace database schema version: {row['version']}")

  @staticmethod
  def _file_entry(row: sqlite3.Row) -> dict:
    return {
      "path": row["path"],
      "checksum": row["checksum"],
      "blob": row["blob"],
      "size": int(row["size"]),
      "mtime": int(row["mtime"]),
      "mtimeNs": int(row["mtime_ns"]), "ctimeNs": int(row["ctime_ns"]),
      "device": int(row["device"]), "inode": int(row["inode"]),
      "mode": int(row["mode"]),
    }

  @staticmethod
  def _workspace_entry(row: sqlite3.Row) -> dict:
    workspace = {
      "id": row["id"], "name": row["name"], "owner": row["owner"], "group": row["group_name"],
      "shared": bool(row["shared"]), "locked": bool(row["locked"]),
      "runLockEnabled": bool(row["run_lock_enabled"]), "created": row["created"], "updated": row["updated"],
      "fileCount": int(row["file_count"]), "sizeBytes": int(row["size_bytes"]),
      "latestSnapshotId": row["latest_snapshot_id"], "initialSnapshotId": row["initial_snapshot_id"],
      "sourceWorkspaceId": row["source_workspace_id"], "sourceSnapshotId": row["source_snapshot_id"],
      "sessions": [],
    }
    if row["active_run_id"]:
      workspace["activeRunId"] = row["active_run_id"]
    if row["active_upload_id"]:
      workspace["activeUploadId"] = row["active_upload_id"]
    return workspace

  def get_workspace_header(self, workspace_id: str) -> dict | None:
    with closing(self.connect()) as connection:
      row = connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
      return self._workspace_entry(row) if row else None

  def list_workspace_headers(self, *, username: str, group_name: str, system_admin: bool) -> list[dict]:
    with closing(self.connect()) as connection:
      if system_admin:
        rows = connection.execute("SELECT * FROM workspaces ORDER BY updated DESC")
      else:
        rows = connection.execute(
          "SELECT * FROM workspaces WHERE owner = ? OR (shared = 1 AND group_name <> '' AND group_name = ?) ORDER BY updated DESC",
          (username, group_name),
        )
      return [self._workspace_entry(row) for row in rows]

  def load_workspace(self, workspace_id: str) -> dict | None:
    """Load the compatibility metadata view for one workspace only."""
    with closing(self.connect()) as connection:
      row = connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
      if not row:
        return None
      workspace = self._workspace_entry(row)
      workspace["sessions"] = [
        {"id": item["session_id"]}
        for item in connection.execute(
          "SELECT session_id FROM workspace_sessions WHERE workspace_id = ? ORDER BY ordinal", (workspace_id,)
        )
      ]
      snapshots = {
        item["id"]: {
          "id": item["id"], "workspaceId": item["workspace_id"], "parentSnapshotId": item["parent_snapshot_id"],
          "reason": item["reason"], "actor": item["actor"], "created": item["created"], "manifestPath": None, "files": {},
        }
        for item in connection.execute("SELECT * FROM snapshots WHERE workspace_id = ?", (workspace_id,))
      }
      latest = snapshots.get(workspace.get("latestSnapshotId"))
      if latest is not None:
        for item in connection.execute(
          "SELECT * FROM workspace_file_versions WHERE workspace_id = ? AND slot = 'current' ORDER BY path", (workspace_id,)
        ):
          latest["files"][item["path"]] = self._file_entry(item)
      artifacts = [{
        "path": item["path"], "status": item["status"], "sizeBytes": int(item["size_bytes"]),
        "size": item["size_label"], "checksum": item["checksum"], "blob": item["blob"], "timestamp": item["timestamp"],
      } for item in connection.execute("SELECT * FROM artifacts WHERE workspace_id = ? ORDER BY path", (workspace_id,))]
      return {"workspaces": {workspace_id: workspace}, "snapshots": snapshots, "artifacts": {workspace_id: artifacts}}

  def load(self) -> dict:
    with closing(self.connect()) as connection, connection:
      workspaces: dict[str, dict] = {}
      for row in connection.execute("SELECT * FROM workspaces"):
        workspace = {
          "id": row["id"],
          "name": row["name"],
          "owner": row["owner"],
          "group": row["group_name"],
          "shared": bool(row["shared"]),
          "locked": bool(row["locked"]),
          "runLockEnabled": bool(row["run_lock_enabled"]),
          "created": row["created"],
          "updated": row["updated"],
          "fileCount": int(row["file_count"]),
          "sizeBytes": int(row["size_bytes"]),
          "latestSnapshotId": row["latest_snapshot_id"],
          "initialSnapshotId": row["initial_snapshot_id"],
          "sourceWorkspaceId": row["source_workspace_id"],
          "sourceSnapshotId": row["source_snapshot_id"],
          "sessions": [],
        }
        if row["active_run_id"]:
          workspace["activeRunId"] = row["active_run_id"]
        if row["active_upload_id"]:
          workspace["activeUploadId"] = row["active_upload_id"]
        workspaces[row["id"]] = workspace

      for row in connection.execute("SELECT workspace_id, session_id FROM workspace_sessions ORDER BY workspace_id, ordinal"):
        workspace = workspaces.get(row["workspace_id"])
        if workspace is not None:
          workspace["sessions"].append({"id": row["session_id"]})

      snapshots = {
        row["id"]: {
          "id": row["id"],
          "workspaceId": row["workspace_id"],
          "parentSnapshotId": row["parent_snapshot_id"],
          "reason": row["reason"],
          "actor": row["actor"],
          "created": row["created"],
          "manifestPath": None,
          "files": {},
        }
        for row in connection.execute("SELECT * FROM snapshots")
      }

      latest_by_workspace = {
        workspace_id: workspace.get("latestSnapshotId")
        for workspace_id, workspace in workspaces.items()
      }
      for row in connection.execute("SELECT * FROM workspace_file_versions WHERE slot = 'current' ORDER BY workspace_id, path"):
        snapshot = snapshots.get(latest_by_workspace.get(row["workspace_id"]))
        if snapshot is not None:
          snapshot["files"][row["path"]] = self._file_entry(row)

      artifacts = {workspace_id: [] for workspace_id in workspaces}
      for row in connection.execute("SELECT * FROM artifacts ORDER BY workspace_id, path"):
        artifacts.setdefault(row["workspace_id"], []).append({
          "path": row["path"],
          "status": row["status"],
          "sizeBytes": int(row["size_bytes"]),
          "size": row["size_label"],
          "checksum": row["checksum"],
          "blob": row["blob"],
          "timestamp": row["timestamp"],
        })
      return {"workspaces": workspaces, "snapshots": snapshots, "artifacts": artifacts}

  @staticmethod
  def _entry_tuple(workspace_id: str, path: str, slot: str, entry: dict, snapshot_id: str | None) -> tuple:
    return (
      workspace_id,
      path,
      slot,
      str(entry["checksum"]),
      str(entry["blob"]),
      int(entry["size"]),
      int(entry.get("mtime") or 0),
      int(entry.get("mtimeNs") or 0), int(entry.get("ctimeNs") or 0),
      int(entry.get("device") or 0), int(entry.get("inode") or 0),
      int(entry.get("mode") or 0o644),
      snapshot_id,
    )

  def save_workspace_state(self, workspace: dict, snapshots: dict, artifacts: list[dict]) -> set[str]:
    """Persist one workspace while retaining the SQLite test adapter semantics."""
    workspace_id = str(workspace["id"])
    with closing(self.connect()) as connection, connection:
      connection.execute("BEGIN IMMEDIATE")
      existing = connection.execute(
        "SELECT latest_snapshot_id FROM workspaces WHERE id = ?", (workspace_id,)
      ).fetchone()
      latest_id = workspace.get("latestSnapshotId")
      if existing and latest_id != existing["latest_snapshot_id"]:
        expected_parent = (snapshots.get(latest_id) or {}).get("parentSnapshotId")
        if expected_parent != existing["latest_snapshot_id"]:
          raise RuntimeError("workspace_changed")
      old_references = {row[0] for row in connection.execute(
        "SELECT DISTINCT blob FROM workspace_file_versions WHERE workspace_id = ?", (workspace_id,)
      )}
      connection.execute(
        """
        INSERT INTO workspaces(
          id, name, owner, group_name, shared, locked, run_lock_enabled, created, updated, file_count, size_bytes,
          latest_snapshot_id, initial_snapshot_id, source_workspace_id, source_snapshot_id,
          active_run_id, active_upload_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          name=excluded.name, owner=excluded.owner, group_name=excluded.group_name,
          shared=excluded.shared, locked=excluded.locked, run_lock_enabled=excluded.run_lock_enabled, created=excluded.created,
          updated=excluded.updated, file_count=excluded.file_count, size_bytes=excluded.size_bytes,
          latest_snapshot_id=excluded.latest_snapshot_id, initial_snapshot_id=excluded.initial_snapshot_id,
          source_workspace_id=excluded.source_workspace_id, source_snapshot_id=excluded.source_snapshot_id,
          active_run_id=excluded.active_run_id, active_upload_id=excluded.active_upload_id
        """,
        (
          workspace_id, str(workspace.get("name") or "Untitled workspace"), str(workspace.get("owner") or ""),
          str(workspace.get("group") or ""), int(bool(workspace.get("shared"))), int(bool(workspace.get("locked"))),
          int(bool(workspace.get("runLockEnabled"))), str(workspace.get("created") or workspace.get("updated") or ""),
          str(workspace.get("updated") or workspace.get("created") or ""), int(workspace.get("fileCount") or 0),
          int(workspace.get("sizeBytes") or 0), workspace.get("latestSnapshotId"), workspace.get("initialSnapshotId"),
          workspace.get("sourceWorkspaceId"), workspace.get("sourceSnapshotId"), workspace.get("activeRunId"),
          workspace.get("activeUploadId"),
        ),
      )
      connection.execute("DELETE FROM workspace_sessions WHERE workspace_id = ?", (workspace_id,))
      for ordinal, session in enumerate(workspace.get("sessions", [])):
        if session.get("id"):
          connection.execute(
            "INSERT INTO workspace_sessions(workspace_id, session_id, ordinal) VALUES (?, ?, ?)",
            (workspace_id, str(session["id"]), ordinal),
          )

      for snapshot_id, snapshot in snapshots.items():
        if snapshot.get("workspaceId") != workspace_id:
          continue
        connection.execute(
          """
          INSERT INTO snapshots(id, workspace_id, parent_snapshot_id, reason, actor, created)
          VALUES (?, ?, ?, ?, ?, ?)
          ON CONFLICT(id) DO UPDATE SET
            workspace_id=excluded.workspace_id, parent_snapshot_id=excluded.parent_snapshot_id,
            reason=excluded.reason, actor=excluded.actor, created=excluded.created
          """,
          (
            snapshot_id,
            snapshot["workspaceId"],
            snapshot.get("parentSnapshotId"),
            str(snapshot.get("reason") or "checkpoint"),
            str(snapshot.get("actor") or ""),
            str(snapshot.get("created") or ""),
          ),
        )

      rows = list(connection.execute("SELECT * FROM workspace_file_versions WHERE workspace_id = ?", (workspace_id,)))
      current_rows = {row["path"]: row for row in rows if row["slot"] == "current"}
      previous_rows = {row["path"]: row for row in rows if row["slot"] == "previous"}
      current = {path: self._file_entry(row) for path, row in current_rows.items()}
      previous = {path: self._file_entry(row) for path, row in previous_rows.items()}
      if latest_id and latest_id not in snapshots:
        raise ValueError(f"workspace {workspace_id} references a missing latest checkpoint")
      incoming = (snapshots.get(latest_id) or {"files": {}}).get("files") or {}
      next_current: dict[str, tuple[dict, str | None]] = {}
      next_previous: dict[str, tuple[dict, str | None]] = {}
      for path in set(current) | set(previous) | set(incoming):
        old_current, old_previous, new_current = current.get(path), previous.get(path), incoming.get(path)
        if new_current is not None and old_current is not None and new_current.get("blob") == old_current.get("blob"):
          next_current[path] = (new_current, latest_id)
          if old_previous is not None and old_previous.get("blob") != new_current.get("blob"):
            next_previous[path] = (old_previous, previous_rows[path]["snapshot_id"])
        elif new_current is not None:
          next_current[path] = (new_current, latest_id)
          retained = old_current or old_previous
          if retained is not None and retained.get("blob") != new_current.get("blob"):
            retained_row = current_rows[path] if old_current is not None else previous_rows[path]
            next_previous[path] = (retained, retained_row["snapshot_id"])
        elif old_current is not None:
          next_previous[path] = (old_current, current_rows[path]["snapshot_id"])
        elif old_previous is not None:
          next_previous[path] = (old_previous, previous_rows[path]["snapshot_id"])
      connection.execute("DELETE FROM workspace_file_versions WHERE workspace_id = ?", (workspace_id,))
      for path, (entry, snapshot_id) in next_current.items():
        connection.execute(
          "INSERT INTO workspace_file_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          self._entry_tuple(workspace_id, path, "current", entry, snapshot_id),
        )
      for path, (entry, snapshot_id) in next_previous.items():
        connection.execute(
          "INSERT INTO workspace_file_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          self._entry_tuple(workspace_id, path, "previous", entry, snapshot_id),
        )

      connection.execute("DELETE FROM artifacts WHERE workspace_id = ?", (workspace_id,))
      for artifact in artifacts:
        connection.execute(
          "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          (
            workspace_id, str(artifact["path"]), str(artifact["status"]), int(artifact.get("sizeBytes") or 0),
            str(artifact.get("size") or "0 B"), str(artifact.get("checksum") or ""), artifact.get("blob"),
            str(artifact.get("timestamp") or ""),
          ),
        )
      new_references = {row[0] for row in connection.execute(
        "SELECT DISTINCT blob FROM workspace_file_versions WHERE workspace_id = ?", (workspace_id,)
      )}
      return old_references - new_references

  def delete_workspace_state(self, workspace_id: str) -> tuple[dict | None, set[str]]:
    with closing(self.connect()) as connection, connection:
      connection.execute("BEGIN IMMEDIATE")
      row = connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
      if not row:
        return None, set()
      candidates = {item[0] for item in connection.execute(
        "SELECT DISTINCT blob FROM workspace_file_versions WHERE workspace_id = ?", (workspace_id,)
      )}
      connection.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
      return self._workspace_entry(row), candidates

  def save_workspace_lifecycle(self, workspace: dict) -> None:
    """Persist only lifecycle/session-reference fields for one workspace.

    Chat admission uses this instead of rewriting the compatibility metadata
    view. SQLite may briefly serialize the row transaction, while unrelated
    filesystem scans and chat event I/O remain outside it.
    """
    with closing(self.connect()) as connection, connection:
      connection.execute("BEGIN IMMEDIATE")
      connection.execute(
        """
        UPDATE workspaces SET locked=?, updated=?, active_run_id=?, active_upload_id=?
        WHERE id=?
        """,
        (
          int(bool(workspace.get("locked"))), str(workspace.get("updated") or ""),
          workspace.get("activeRunId"), workspace.get("activeUploadId"), workspace["id"],
        ),
      )
      connection.execute("DELETE FROM workspace_sessions WHERE workspace_id = ?", (workspace["id"],))
      for ordinal, item in enumerate(workspace.get("sessions", [])):
        session_id = str(item.get("id") or "")
        if session_id:
          connection.execute(
            "INSERT INTO workspace_sessions(workspace_id, session_id, ordinal) VALUES (?, ?, ?)",
            (workspace["id"], session_id, ordinal),
          )

  def referenced_blobs(self, candidates: set[str] | None = None) -> set[str]:
    with closing(self.connect()) as connection, connection:
      if candidates is None:
        rows = connection.execute("SELECT DISTINCT blob FROM workspace_file_versions")
      elif not candidates:
        return set()
      else:
        placeholders = ",".join("?" for _ in candidates)
        rows = connection.execute(
          f"SELECT DISTINCT blob FROM workspace_file_versions WHERE blob IN ({placeholders})", tuple(candidates)
        )
      return {row[0] for row in rows}

  def file_versions(self, workspace_id: str, path: str) -> dict[str, dict]:
    with closing(self.connect()) as connection, connection:
      return {
        row["slot"]: self._file_entry(row)
        for row in connection.execute(
          "SELECT * FROM workspace_file_versions WHERE workspace_id = ? AND path = ? ORDER BY slot",
          (workspace_id, path),
        )
      }

  def apply_workspace_delta(self, workspace: dict, delta: dict, snapshot: dict, artifacts: list[dict]) -> set[str]:
    from workspace_delta_database import apply_sqlite_delta
    return apply_sqlite_delta(self, workspace, delta, snapshot, artifacts)
