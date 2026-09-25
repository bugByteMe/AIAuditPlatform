from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import JSON, BigInteger, Column, Index, MetaData, String, Table, create_engine, delete, func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from workspace_store import generated_id, now_string


class ChatStore:
  """Normalized chat persistence for SQLite development and PostgreSQL production."""

  def __init__(self, root: Path, database_url: str = ""):
    self.root = root
    self.sessions_path = root / "sessions.json"
    self.runs_path = root / "runs.json"
    self.events_dir = root / "events"
    self.database_url = database_url or f"sqlite:///{(root / 'chat.sqlite3').as_posix()}"
    connect_args = {"check_same_thread": False, "timeout": 30} if self.database_url.startswith("sqlite") else {}
    self.engine: Engine = create_engine(self.database_url, pool_pre_ping=True, connect_args=connect_args)
    self.metadata = MetaData()
    self.session_table = Table(
      "chat_sessions", self.metadata,
      Column("id", String(160), primary_key=True), Column("workspace_id", String(160), nullable=False, index=True),
      Column("updated", String(32), nullable=False, index=True), Column("payload", JSON, nullable=False),
    )
    self.run_table = Table(
      "chat_runs", self.metadata,
      Column("id", String(160), primary_key=True), Column("workspace_id", String(160), nullable=False, index=True),
      Column("session_id", String(160), nullable=False, index=True), Column("group_id", String(160), nullable=False, index=True),
      Column("status", String(32), nullable=False, index=True), Column("updated", String(32), nullable=False, index=True),
      Column("payload", JSON, nullable=False),
    )
    self.counter_table = Table(
      "chat_event_counters", self.metadata,
      Column("session_id", String(160), primary_key=True), Column("next_id", BigInteger, nullable=False),
    )
    self.event_table = Table(
      "chat_events", self.metadata,
      Column("session_id", String(160), primary_key=True), Column("event_id", BigInteger, primary_key=True),
      Column("event_type", String(48), nullable=False), Column("run_id", String(160), nullable=False),
      Column("time", String(32), nullable=False), Column("payload", JSON, nullable=False),
    )
    Index("chat_events_session_cursor", self.event_table.c.session_id, self.event_table.c.event_id)
    self.ensure_layout()

  def ensure_layout(self) -> None:
    self.root.mkdir(parents=True, exist_ok=True)
    self.events_dir.mkdir(parents=True, exist_ok=True)
    self.metadata.create_all(self.engine)
    self.import_legacy_if_empty()

  @contextmanager
  def transaction(self):
    with self.engine.begin() as connection:
      yield connection

  @staticmethod
  def _payload(row) -> dict | None:
    return dict(row.payload) if row is not None and row.payload is not None else None

  def import_legacy_if_empty(self) -> None:
    with self.engine.begin() as connection:
      if connection.execute(select(func.count()).select_from(self.session_table)).scalar_one():
        return
      sessions = self._load_legacy_json(self.sessions_path)
      runs = self._load_legacy_json(self.runs_path)
      for session in sessions.values():
        self._upsert_session(connection, session)
      for run in runs.values():
        self._upsert_run(connection, run)
      for session_id in sessions:
        events = self._legacy_events(self.events_path(session_id))
        if events:
          connection.execute(insert(self.event_table), [self._event_row(session_id, item) for item in events])
        next_id = max([int(item.get("id") or 0) for item in events], default=0) + 1
        connection.execute(update(self.counter_table).where(self.counter_table.c.session_id == session_id).values(next_id=next_id))

  @staticmethod
  def _load_legacy_json(path: Path) -> dict:
    if not path.exists():
      return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}

  @staticmethod
  def _legacy_events(path: Path) -> list[dict]:
    if not path.exists():
      return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

  def sessions(self) -> dict:
    with self.engine.connect() as connection:
      return {row.id: dict(row.payload) for row in connection.execute(select(self.session_table))}

  def runs(self) -> dict:
    with self.engine.connect() as connection:
      return {row.id: dict(row.payload) for row in connection.execute(select(self.run_table))}

  def get_session(self, session_id: str) -> dict | None:
    with self.engine.connect() as connection:
      return self._payload(connection.execute(select(self.session_table.c.payload).where(self.session_table.c.id == session_id)).first())

  def get_run(self, run_id: str) -> dict | None:
    with self.engine.connect() as connection:
      return self._payload(connection.execute(select(self.run_table.c.payload).where(self.run_table.c.id == run_id)).first())

  def _upsert_session(self, connection, session: dict) -> None:
    payload = dict(session)
    values = {"workspace_id": str(payload.get("workspaceId") or ""), "updated": str(payload.get("updated") or now_string()), "payload": payload}
    if connection.execute(select(self.session_table.c.id).where(self.session_table.c.id == payload["id"])).first():
      connection.execute(update(self.session_table).where(self.session_table.c.id == payload["id"]).values(**values))
    else:
      connection.execute(insert(self.session_table).values(id=payload["id"], **values))
      connection.execute(insert(self.counter_table).values(session_id=payload["id"], next_id=1))

  def save_session(self, session: dict) -> None:
    with self.engine.begin() as connection:
      self._upsert_session(connection, session)

  def _upsert_run(self, connection, run: dict) -> None:
    payload = self.sanitize_run(run)
    values = {
      "workspace_id": str(payload.get("workspaceId") or ""), "session_id": str(payload.get("sessionId") or ""),
      "group_id": str(payload.get("groupId") or ""), "status": str(payload.get("status") or ""),
      "updated": str(payload.get("updated") or now_string()), "payload": payload,
    }
    if connection.execute(select(self.run_table.c.id).where(self.run_table.c.id == payload["id"])).first():
      connection.execute(update(self.run_table).where(self.run_table.c.id == payload["id"]).values(**values))
    else:
      connection.execute(insert(self.run_table).values(id=payload["id"], **values))

  def save_run(self, run: dict) -> None:
    with self.engine.begin() as connection:
      self._upsert_run(connection, run)

  @staticmethod
  def sanitize_run(run: dict) -> dict:
    return {key: value for key, value in run.items() if key not in {"codexSettings", "codexHome"}}

  def events_path(self, session_id: str) -> Path:
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in session_id).strip("._")
    return self.events_dir / f"{safe or generated_id('chat')}.jsonl"

  @staticmethod
  def _event_row(session_id: str, event: dict) -> dict:
    payload = dict(event)
    return {
      "session_id": session_id, "event_id": int(payload["id"]), "event_type": str(payload.get("type") or "progress"),
      "run_id": str(payload.get("runId") or ""), "time": str(payload.get("time") or now_string()), "payload": payload,
    }

  def append_event(self, session_id: str, event: dict) -> dict:
    for _ in range(3):
      try:
        with self.engine.begin() as connection:
          counter = connection.execute(select(self.counter_table.c.next_id).where(self.counter_table.c.session_id == session_id).with_for_update()).first()
          if counter is None:
            connection.execute(insert(self.counter_table).values(session_id=session_id, next_id=2))
            event_id = 1
          else:
            event_id = int(counter.next_id)
            connection.execute(update(self.counter_table).where(self.counter_table.c.session_id == session_id).values(next_id=event_id + 1))
          persisted = {**event, "id": event_id}
          connection.execute(insert(self.event_table).values(**self._event_row(session_id, persisted)))
          return persisted
      except IntegrityError:
        continue
    raise RuntimeError("could not allocate a chat event id")

  def events(self, session_id: str, after: int = 0, *, before: int | None = None, limit: int | None = None, latest: bool = False) -> list[dict]:
    page_size = min(500, max(1, int(limit or 500)))
    statement = select(self.event_table.c.payload).where(self.event_table.c.session_id == session_id)
    if latest:
      statement = statement.order_by(self.event_table.c.event_id.desc()).limit(page_size)
      with self.engine.connect() as connection:
        return [dict(row.payload) for row in reversed(list(connection.execute(statement)))]
    if before is not None:
      statement = statement.where(self.event_table.c.event_id < int(before)).order_by(self.event_table.c.event_id.desc()).limit(page_size)
      with self.engine.connect() as connection:
        return [dict(row.payload) for row in reversed(list(connection.execute(statement)))]
    statement = statement.where(self.event_table.c.event_id > int(after)).order_by(self.event_table.c.event_id).limit(page_size)
    with self.engine.connect() as connection:
      return [dict(row.payload) for row in connection.execute(statement)]

  def replace_events(self, session_id: str, events: list[dict]) -> None:
    with self.engine.begin() as connection:
      connection.execute(delete(self.event_table).where(self.event_table.c.session_id == session_id))
      rows = [self._event_row(session_id, {**event, "id": event_id}) for event_id, event in enumerate(events, start=1)]
      if rows:
        connection.execute(insert(self.event_table), rows)
      connection.execute(update(self.counter_table).where(self.counter_table.c.session_id == session_id).values(next_id=len(rows) + 1))

  def public_session(self, session_id: str, include_events: bool = True) -> dict:
    session = self.get_session(session_id)
    if not session:
      return {"id": session_id, "title": "Unknown session", "status": "failed", "updated": now_string(), "tokens": "0", "events": []}
    events = self.public_events(session_id) if include_events else []
    return {
      "id": session["id"], "title": session["title"], "status": session["status"], "updated": session["updated"],
      "tokens": session.get("tokens", "0"), "latestRunId": session.get("latestRunId"),
      "codexNativeResumable": bool(session.get("codexNativeResumable")), "forkedFromSessionId": session.get("forkedFromSessionId"),
      "events": [self.public_event_tuple(event) for event in events],
    }

  def public_events(self, session_id: str) -> list[dict]:
    visible, tool_indexes = [], {}
    for event in self.events(session_id, limit=500, latest=True):
      key = self.tool_event_key(event)
      if key and key in tool_indexes:
        visible[tool_indexes[key]] = {**visible[tool_indexes[key]], **event}
      else:
        if key:
          tool_indexes[key] = len(visible)
        visible.append(event)
    return visible

  @staticmethod
  def public_event_tuple(event: dict) -> list:
    return [event["type"], event["message"], event["message"], event.get("runId") or "", event.get("id") or 0, event.get("status") or "", event.get("toolCallId") or ""]

  @staticmethod
  def tool_event_key(event: dict) -> tuple | None:
    if event.get("type") not in {"command", "tool"} or not event.get("toolCallId"):
      return None
    return (event.get("runId") or "", event["type"], event["toolCallId"])

  def delete_resources(self, workspace_ids: set[str]) -> dict:
    with self.engine.begin() as connection:
      session_ids = set(connection.execute(select(self.session_table.c.id).where(self.session_table.c.workspace_id.in_(workspace_ids))).scalars())
      run_ids = set(connection.execute(select(self.run_table.c.id).where(self.run_table.c.workspace_id.in_(workspace_ids))).scalars())
      if session_ids:
        connection.execute(delete(self.event_table).where(self.event_table.c.session_id.in_(session_ids)))
        connection.execute(delete(self.counter_table).where(self.counter_table.c.session_id.in_(session_ids)))
      connection.execute(delete(self.run_table).where(self.run_table.c.workspace_id.in_(workspace_ids)))
      connection.execute(delete(self.session_table).where(self.session_table.c.workspace_id.in_(workspace_ids)))
    return {"sessionIds": sorted(session_ids), "runIds": sorted(run_ids)}

  def delete_workspaces(self, workspace_ids: set[str]) -> dict:
    return self.delete_resources(workspace_ids)

  def delete_session(self, session_id: str) -> dict | None:
    session = self.get_session(session_id)
    if not session:
      return None
    with self.engine.begin() as connection:
      runs = {row.id: dict(row.payload) for row in connection.execute(select(self.run_table).where(self.run_table.c.session_id == session_id))}
      connection.execute(delete(self.event_table).where(self.event_table.c.session_id == session_id))
      connection.execute(delete(self.counter_table).where(self.counter_table.c.session_id == session_id))
      connection.execute(delete(self.run_table).where(self.run_table.c.session_id == session_id))
      connection.execute(delete(self.session_table).where(self.session_table.c.id == session_id))
    legacy_path = self.events_path(session_id)
    if legacy_path.exists():
      legacy_path.unlink()
    return {"session": session, "runs": runs}
