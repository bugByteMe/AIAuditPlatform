from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, MetaData, String, Table, Text, create_engine, delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert


class PostgresWorkspaceDatabase:
  """PostgreSQL workspace metadata; workspace payloads remain on shared storage."""

  def __init__(self, database_url: str):
    self.engine = create_engine(database_url, pool_pre_ping=True)
    self.metadata = MetaData()
    self.workspaces = Table(
      "workspaces", self.metadata,
      Column("id", String(160), primary_key=True), Column("name", Text, nullable=False),
      Column("owner", String(255), nullable=False, index=True), Column("group_name", Text, nullable=False, default=""),
      Column("shared", Boolean, nullable=False, default=False), Column("locked", Boolean, nullable=False, default=False),
      Column("run_lock_enabled", Boolean, nullable=False, default=False), Column("created", String(32), nullable=False),
      Column("updated", String(32), nullable=False, index=True), Column("file_count", Integer, nullable=False, default=0),
      Column("size_bytes", BigInteger, nullable=False, default=0), Column("latest_snapshot_id", String(160)),
      Column("initial_snapshot_id", String(160)), Column("source_workspace_id", String(160)),
      Column("source_snapshot_id", String(160)), Column("active_run_id", String(160)), Column("active_upload_id", String(160)),
    )
    self.workspace_sessions = Table(
      "workspace_sessions", self.metadata,
      Column("workspace_id", String(160), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True),
      Column("session_id", String(160), primary_key=True), Column("ordinal", Integer, nullable=False),
    )
    self.snapshots = Table(
      "snapshots", self.metadata,
      Column("id", String(160), primary_key=True),
      Column("workspace_id", String(160), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True),
      Column("parent_snapshot_id", String(160)), Column("reason", Text, nullable=False), Column("actor", Text, nullable=False),
      Column("created", String(32), nullable=False),
    )
    Index("snapshots_workspace_created", self.snapshots.c.workspace_id, self.snapshots.c.created)
    self.file_version_rows = Table(
      "workspace_file_versions", self.metadata,
      Column("workspace_id", String(160), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True),
      Column("path", Text, primary_key=True), Column("slot", String(16), primary_key=True),
      Column("checksum", String(128), nullable=False), Column("blob", Text, nullable=False, index=True),
      Column("size", BigInteger, nullable=False), Column("mtime", BigInteger, nullable=False),
      Column("mode", Integer, nullable=False), Column("snapshot_id", String(160)),
      CheckConstraint("slot IN ('current', 'previous')", name="workspace_file_slot"),
    )
    self.artifacts = Table(
      "artifacts", self.metadata,
      Column("workspace_id", String(160), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True),
      Column("path", Text, primary_key=True), Column("status", String(32), nullable=False),
      Column("size_bytes", BigInteger, nullable=False), Column("size_label", String(64), nullable=False),
      Column("checksum", String(128), nullable=False), Column("blob", Text), Column("timestamp", String(32), nullable=False),
    )
    Index("workspace_sessions_order", self.workspace_sessions.c.workspace_id, self.workspace_sessions.c.ordinal)
    self.metadata.create_all(self.engine)

  @staticmethod
  def _entry(row) -> dict:
    return {
      "path": row["path"], "checksum": row["checksum"], "blob": row["blob"], "size": int(row["size"]),
      "mtime": int(row["mtime"]), "mode": int(row["mode"]),
    }

  @staticmethod
  def _workspace_values(workspace: dict) -> dict:
    return {
      "id": workspace["id"], "name": str(workspace.get("name") or "Untitled workspace"),
      "owner": str(workspace.get("owner") or ""), "group_name": str(workspace.get("group") or ""),
      "shared": bool(workspace.get("shared")), "locked": bool(workspace.get("locked")),
      "run_lock_enabled": bool(workspace.get("runLockEnabled")),
      "created": str(workspace.get("created") or workspace.get("updated") or ""),
      "updated": str(workspace.get("updated") or workspace.get("created") or ""),
      "file_count": int(workspace.get("fileCount") or 0), "size_bytes": int(workspace.get("sizeBytes") or 0),
      "latest_snapshot_id": workspace.get("latestSnapshotId"), "initial_snapshot_id": workspace.get("initialSnapshotId"),
      "source_workspace_id": workspace.get("sourceWorkspaceId"), "source_snapshot_id": workspace.get("sourceSnapshotId"),
      "active_run_id": workspace.get("activeRunId"), "active_upload_id": workspace.get("activeUploadId"),
    }

  def load(self) -> dict:
    with self.engine.connect() as connection:
      workspace_rows = list(connection.execute(select(self.workspaces)).mappings())
      workspaces = {}
      for row in workspace_rows:
        item = {
          "id": row["id"], "name": row["name"], "owner": row["owner"], "group": row["group_name"],
          "shared": bool(row["shared"]), "locked": bool(row["locked"]), "runLockEnabled": bool(row["run_lock_enabled"]),
          "created": row["created"], "updated": row["updated"], "fileCount": int(row["file_count"]),
          "sizeBytes": int(row["size_bytes"]), "latestSnapshotId": row["latest_snapshot_id"],
          "initialSnapshotId": row["initial_snapshot_id"], "sourceWorkspaceId": row["source_workspace_id"],
          "sourceSnapshotId": row["source_snapshot_id"], "sessions": [],
        }
        if row["active_run_id"]:
          item["activeRunId"] = row["active_run_id"]
        if row["active_upload_id"]:
          item["activeUploadId"] = row["active_upload_id"]
        workspaces[item["id"]] = item
      for row in connection.execute(select(self.workspace_sessions).order_by(self.workspace_sessions.c.workspace_id, self.workspace_sessions.c.ordinal)).mappings():
        if row["workspace_id"] in workspaces:
          workspaces[row["workspace_id"]]["sessions"].append({"id": row["session_id"]})
      snapshots = {
        row["id"]: {
          "id": row["id"], "workspaceId": row["workspace_id"], "parentSnapshotId": row["parent_snapshot_id"],
          "reason": row["reason"], "actor": row["actor"], "created": row["created"], "manifestPath": None, "files": {},
        }
        for row in connection.execute(select(self.snapshots)).mappings()
      }
      latest = {workspace_id: item.get("latestSnapshotId") for workspace_id, item in workspaces.items()}
      for row in connection.execute(select(self.file_version_rows).where(self.file_version_rows.c.slot == "current").order_by(self.file_version_rows.c.workspace_id, self.file_version_rows.c.path)).mappings():
        snapshot = snapshots.get(latest.get(row["workspace_id"]))
        if snapshot is not None:
          snapshot["files"][row["path"]] = self._entry(row)
      artifacts = {workspace_id: [] for workspace_id in workspaces}
      for row in connection.execute(select(self.artifacts).order_by(self.artifacts.c.workspace_id, self.artifacts.c.path)).mappings():
        artifacts.setdefault(row["workspace_id"], []).append({
          "path": row["path"], "status": row["status"], "sizeBytes": int(row["size_bytes"]),
          "size": row["size_label"], "checksum": row["checksum"], "blob": row["blob"], "timestamp": row["timestamp"],
        })
      return {"workspaces": workspaces, "snapshots": snapshots, "artifacts": artifacts}

  def save_workspace_lifecycle(self, workspace: dict) -> None:
    with self.engine.begin() as connection:
      connection.execute(update(self.workspaces).where(self.workspaces.c.id == workspace["id"]).values(
        locked=bool(workspace.get("locked")), updated=str(workspace.get("updated") or ""),
        active_run_id=workspace.get("activeRunId"), active_upload_id=workspace.get("activeUploadId"),
      ))
      self._replace_sessions(connection, workspace)

  def _replace_sessions(self, connection, workspace: dict) -> None:
    connection.execute(delete(self.workspace_sessions).where(self.workspace_sessions.c.workspace_id == workspace["id"]))
    rows = [{"workspace_id": workspace["id"], "session_id": str(item.get("id") or ""), "ordinal": ordinal}
            for ordinal, item in enumerate(workspace.get("sessions", [])) if item.get("id")]
    if rows:
      connection.execute(insert(self.workspace_sessions), rows)

  def save(self, metadata: dict) -> set[str]:
    with self.engine.begin() as connection:
      old_references = set(connection.execute(select(self.file_version_rows.c.blob).distinct()).scalars())
      existing = set(connection.execute(select(self.workspaces.c.id)).scalars())
      incoming = set(metadata.get("workspaces", {}))
      if existing - incoming:
        connection.execute(delete(self.workspaces).where(self.workspaces.c.id.in_(existing - incoming)))
      for workspace in metadata.get("workspaces", {}).values():
        values = self._workspace_values(workspace)
        statement = pg_insert(self.workspaces).values(**values)
        connection.execute(statement.on_conflict_do_update(index_elements=[self.workspaces.c.id], set_={key: value for key, value in values.items() if key != "id"}))
        self._replace_sessions(connection, workspace)
      existing_snapshots = set(connection.execute(select(self.snapshots.c.id)).scalars())
      incoming_snapshots = set(metadata.get("snapshots", {}))
      if existing_snapshots - incoming_snapshots:
        connection.execute(delete(self.snapshots).where(self.snapshots.c.id.in_(existing_snapshots - incoming_snapshots)))
      for snapshot in metadata.get("snapshots", {}).values():
        if snapshot.get("workspaceId") not in incoming:
          continue
        values = {
          "id": snapshot["id"], "workspace_id": snapshot["workspaceId"], "parent_snapshot_id": snapshot.get("parentSnapshotId"),
          "reason": str(snapshot.get("reason") or "checkpoint"), "actor": str(snapshot.get("actor") or ""),
          "created": str(snapshot.get("created") or ""),
        }
        statement = pg_insert(self.snapshots).values(**values)
        connection.execute(statement.on_conflict_do_update(index_elements=[self.snapshots.c.id], set_={key: value for key, value in values.items() if key != "id"}))
      for workspace_id, workspace in metadata.get("workspaces", {}).items():
        rows = list(connection.execute(select(self.file_version_rows).where(self.file_version_rows.c.workspace_id == workspace_id)).mappings())
        current = {row["path"]: row for row in rows if row["slot"] == "current"}
        previous = {row["path"]: row for row in rows if row["slot"] == "previous"}
        latest_id = workspace.get("latestSnapshotId")
        if latest_id and latest_id not in metadata.get("snapshots", {}):
          raise ValueError(f"workspace {workspace_id} references a missing latest checkpoint")
        latest_snapshot = metadata.get("snapshots", {}).get(latest_id) or {"files": {}}
        incoming_files = latest_snapshot.get("files") or {}
        next_rows = []
        for path in set(current) | set(previous) | set(incoming_files):
          old_current, old_previous, new_current = current.get(path), previous.get(path), incoming_files.get(path)
          retained = None
          if new_current is not None:
            next_rows.append(self._file_values(workspace_id, path, "current", new_current, latest_id))
            if old_current and old_current["blob"] == new_current.get("blob"):
              retained = old_previous
            elif old_current:
              retained = old_current
            elif old_previous and old_previous["blob"] != new_current.get("blob"):
              retained = old_previous
          else:
            retained = old_current or old_previous
          if retained is not None:
            next_rows.append(self._file_values(workspace_id, path, "previous", retained, retained.get("snapshot_id")))
        connection.execute(delete(self.file_version_rows).where(self.file_version_rows.c.workspace_id == workspace_id))
        if next_rows:
          connection.execute(insert(self.file_version_rows), next_rows)
      connection.execute(delete(self.artifacts))
      artifact_rows = []
      for workspace_id, items in metadata.get("artifacts", {}).items():
        if workspace_id not in incoming:
          continue
        for item in items:
          artifact_rows.append({
            "workspace_id": workspace_id, "path": str(item["path"]), "status": str(item["status"]),
            "size_bytes": int(item.get("sizeBytes") or 0), "size_label": str(item.get("size") or "0 B"),
            "checksum": str(item.get("checksum") or ""), "blob": item.get("blob"), "timestamp": str(item.get("timestamp") or ""),
          })
      if artifact_rows:
        connection.execute(insert(self.artifacts), artifact_rows)
      new_references = set(connection.execute(select(self.file_version_rows.c.blob).distinct()).scalars())
      return old_references - new_references

  @staticmethod
  def _file_values(workspace_id: str, path: str, slot: str, entry, snapshot_id: str | None) -> dict:
    return {
      "workspace_id": workspace_id, "path": path, "slot": slot, "checksum": str(entry["checksum"]),
      "blob": str(entry["blob"]), "size": int(entry["size"]), "mtime": int(entry.get("mtime") or 0),
      "mode": int(entry.get("mode") or 0o644), "snapshot_id": snapshot_id,
    }

  def referenced_blobs(self) -> set[str]:
    with self.engine.connect() as connection:
      return set(connection.execute(select(self.file_version_rows.c.blob).distinct()).scalars())

  def file_versions_for_path(self, workspace_id: str, path: str) -> dict[str, dict]:
    with self.engine.connect() as connection:
      return {
        row["slot"]: self._entry(row)
        for row in connection.execute(select(self.file_version_rows).where(
          self.file_version_rows.c.workspace_id == workspace_id, self.file_version_rows.c.path == path,
        ).order_by(self.file_version_rows.c.slot)).mappings()
      }

  def file_versions(self, workspace_id: str, path: str) -> dict[str, dict]:
    return self.file_versions_for_path(workspace_id, path)
