from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import JSON, BigInteger, Boolean, Column, Float, Index, MetaData, String, Table, create_engine, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert


TERMINAL_UPLOAD_STATES = {"committed", "cancelled", "expired", "failed"}


class UploadDatabase:
  def __init__(self, root: Path, database_url: str = ""):
    url = database_url or f"sqlite:///{(root / 'upload.sqlite3').as_posix()}"
    connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    self.engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    self.metadata = MetaData()
    self.sessions = Table(
      "upload_sessions", self.metadata,
      Column("id", String(160), primary_key=True), Column("owner", String(255), nullable=False, index=True),
      Column("group_id", String(160), nullable=False, index=True), Column("workspace_id", String(160), nullable=False, index=True),
      Column("worker_id", String(160)), Column("status", String(32), nullable=False, index=True),
      Column("reserved_bytes", BigInteger, nullable=False, default=0), Column("reservation_held", Boolean, nullable=False, default=False),
      Column("created_at", Float, nullable=False), Column("updated_at", Float, nullable=False, index=True),
      Column("terminal_at", Float, index=True), Column("cleanup_pending", Boolean, nullable=False, default=False),
      Column("payload", JSON, nullable=False),
    )
    Index("upload_group_status", self.sessions.c.group_id, self.sessions.c.status)
    Index("upload_workspace_status", self.sessions.c.workspace_id, self.sessions.c.status)
    self.metadata.create_all(self.engine)

  @staticmethod
  def _values(session: dict) -> dict:
    status = str(session.get("status") or "uploading")
    terminal_at = session.get("terminalAt")
    if status in TERMINAL_UPLOAD_STATES and terminal_at is None:
      terminal_at = time.time()
      session["terminalAt"] = terminal_at
    return {
      "id": str(session["id"]), "owner": str(session.get("owner") or ""),
      "group_id": str(session.get("groupId") or ""), "workspace_id": str(session.get("workspaceId") or ""),
      "worker_id": session.get("workerId"), "status": status,
      "reserved_bytes": int(session.get("reservedBytes") or 0), "reservation_held": bool(session.get("reservationHeld")),
      "created_at": float(session.get("createdAt") or time.time()), "updated_at": float(session.get("updatedAt") or time.time()),
      "terminal_at": terminal_at, "cleanup_pending": bool(session.get("cleanupPending")), "payload": dict(session),
    }

  def get(self, upload_id: str) -> dict | None:
    with self.engine.connect() as connection:
      row = connection.execute(select(self.sessions.c.payload).where(self.sessions.c.id == upload_id)).first()
      return dict(row.payload) if row else None

  def all(self) -> dict[str, dict]:
    with self.engine.connect() as connection:
      return {row.id: dict(row.payload) for row in connection.execute(select(self.sessions))}

  def save(self, session: dict) -> None:
    values = self._values(session)
    with self.engine.begin() as connection:
      if connection.dialect.name == "postgresql":
        statement = pg_insert(self.sessions).values(**values)
        connection.execute(statement.on_conflict_do_update(
          index_elements=[self.sessions.c.id], set_={key: value for key, value in values.items() if key != "id"},
        ))
      else:
        existing = connection.execute(select(self.sessions.c.id).where(self.sessions.c.id == session["id"])).first()
        if existing:
          connection.execute(update(self.sessions).where(self.sessions.c.id == session["id"]).values(**{key: value for key, value in values.items() if key != "id"}))
        else:
          connection.execute(self.sessions.insert().values(**values))

  def active_reserved_bytes(self, group_id: str, states: set[str]) -> int:
    with self.engine.connect() as connection:
      rows = connection.execute(select(self.sessions.c.reserved_bytes).where(
        self.sessions.c.group_id == group_id, self.sessions.c.status.in_(states),
      )).scalars()
      return sum(int(value or 0) for value in rows)

  def stale_active(self, cutoff: float, states: set[str], limit: int) -> list[dict]:
    with self.engine.connect() as connection:
      rows = connection.execute(select(self.sessions.c.payload).where(
        self.sessions.c.status.in_(states), self.sessions.c.updated_at < cutoff,
      ).order_by(self.sessions.c.updated_at).limit(limit))
      return [dict(row.payload) for row in rows]

  def cleanup_pending(self, limit: int) -> list[dict]:
    with self.engine.connect() as connection:
      rows = connection.execute(select(self.sessions.c.payload).where(
        self.sessions.c.cleanup_pending.is_(True),
      ).order_by(self.sessions.c.terminal_at).limit(limit))
      return [dict(row.payload) for row in rows]

  def delete_terminal_before(self, cutoff: float, limit: int) -> list[str]:
    with self.engine.begin() as connection:
      ids = list(connection.execute(select(self.sessions.c.id).where(
        self.sessions.c.status.in_(TERMINAL_UPLOAD_STATES), self.sessions.c.terminal_at < cutoff,
        self.sessions.c.cleanup_pending.is_(False),
      ).order_by(self.sessions.c.terminal_at).limit(limit)).scalars())
      if ids:
        connection.execute(delete(self.sessions).where(self.sessions.c.id.in_(ids)))
      return ids

  def delete(self, upload_id: str) -> None:
    with self.engine.begin() as connection:
      connection.execute(delete(self.sessions).where(self.sessions.c.id == upload_id))

  def import_sessions(self, sessions: dict[str, dict]) -> None:
    if self.all():
      raise ValueError("target upload table is not empty")
    with self.engine.begin() as connection:
      for session in sessions.values():
        imported = dict(session)
        imported["reservationHeld"] = False
        connection.execute(self.sessions.insert().values(**self._values(imported)))
