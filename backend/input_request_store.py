"""Transactional question state and durable response delivery outbox."""
from __future__ import annotations

from sqlalchemy import JSON, Column, String, Table, func, insert, select, update

from user_input_schema import validate_answer, validate_question
from workspace_store import StorageError, now_string


class InputRequestStore:
  def __init__(self, chat):
    self.chat = chat
    self.table = Table(
      "chat_input_requests", chat.metadata,
      Column("id", String(32), primary_key=True),
      Column("run_id", String(160), nullable=False, index=True),
      Column("session_id", String(160), nullable=False, index=True),
      Column("status", String(32), nullable=False, index=True),
      Column("delivery", String(32), nullable=False, index=True),
      Column("payload", JSON, nullable=False),
    )

  def get(self, request_id, connection=None):
    if connection is None:
      with self.chat.engine.connect() as opened:
        return self.get(request_id, opened)
    row = connection.execute(select(self.table.c.payload).where(self.table.c.id == request_id)).first()
    return dict(row.payload) if row else None

  def list(self, session_id):
    with self.chat.engine.connect() as connection:
      return [dict(row.payload) for row in connection.execute(select(self.table.c.payload).where(
        self.table.c.session_id == session_id,
      ))]

  def pending_counts(self, session_ids):
    with self.chat.engine.connect() as connection:
      return dict(connection.execute(select(self.table.c.session_id, func.count()).where(
        self.table.c.session_id.in_(session_ids), self.table.c.status == "pending",
      ).group_by(self.table.c.session_id)).all())

  def pending_run_ids(self):
    with self.chat.engine.connect() as connection:
      return list(connection.execute(select(self.table.c.run_id).where(
        self.table.c.status == "pending",
      ).distinct()).scalars())

  def ingest(self, connection, run, event):
    if event.get("type") != "user_input_requested":
      return
    incoming = event["inputRequest"]
    existing = self.get(incoming["id"], connection)
    if existing:
      if existing["runId"] != run["id"]:
        raise ValueError("input request id belongs to another run")
      event["inputRequest"] = self.public(existing)
      return
    question = validate_question({**incoming["question"], "images": []})
    question.pop("images")
    payload = {
      "id": incoming["id"], "runId": run["id"], "sessionId": run["sessionId"],
      "workspaceId": run["workspaceId"], "initiator": run["user"], "question": question,
      "images": incoming.get("images", []), "status": "pending", "created": now_string(),
    }
    connection.execute(insert(self.table).values(
      id=payload["id"], run_id=run["id"], session_id=run["sessionId"], status="pending",
      delivery="none", payload=payload,
    ))
    event["inputRequest"] = self.public(payload)

  @staticmethod
  def public(payload):
    return {key: value for key, value in payload.items() if key != "images"} | {
      "images": [{"index": index, "name": image["name"], "contentType": image["contentType"]}
                 for index, image in enumerate(payload.get("images", []))],
    }

  def append_event(self, connection, payload):
    chat = self.chat
    session_id = payload["sessionId"]
    counter = connection.execute(select(chat.counter_table.c.next_id).where(
      chat.counter_table.c.session_id == session_id,
    ).with_for_update()).scalar_one()
    connection.execute(update(chat.counter_table).where(chat.counter_table.c.session_id == session_id).values(next_id=counter + 1))
    event = {
      "id": counter, "time": now_string(), "runId": payload["runId"],
      "type": "user_input_resolved", "message": f"Input {payload['status']}.",
      "inputRequest": self.public(payload),
    }
    connection.execute(insert(chat.event_table).values(**chat._event_row(session_id, event)))
    return event

  def resolve(self, request_id, username, answer):
    request = self.get(request_id)
    if not request:
      raise StorageError("not_found", "input request not found")
    with self.chat.engine.begin() as connection:
      if self.chat.engine.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
      # All response, runner-event and terminal transitions lock the run first.
      run = connection.execute(select(self.chat.run_table.c.payload).where(
        self.chat.run_table.c.id == request["runId"],
      ).with_for_update()).scalar_one()
      current = self.get(request_id, connection)
      if username != current["initiator"]:
        raise StorageError("forbidden", "only the run initiator can answer")
      if run["status"] not in {"starting", "running"}:
        raise StorageError("input_conflict", "the run is no longer accepting input")
      result = {"requestId": request_id, **validate_answer(current["question"], answer)}
      if current["status"] != "pending":
        if current.get("result") == result:
          return current, None
        raise StorageError("input_conflict", "input has already been resolved")
      current.update(status=result["status"], result=result, responder=username, resolved=now_string())
      changed = connection.execute(update(self.table).where(
        self.table.c.id == request_id, self.table.c.status == "pending",
      ).values(status=current["status"], delivery="pending", payload=current))
      if not changed.rowcount:
        raise StorageError("input_conflict", "input has already been resolved")
      return current, self.append_event(connection, current)

  def cancel_run(self, run_id):
    events = []
    with self.chat.engine.begin() as connection:
      connection.execute(select(self.chat.run_table.c.id).where(
        self.chat.run_table.c.id == run_id,
      ).with_for_update()).first()
      rows = connection.execute(select(self.table.c.payload).where(
        self.table.c.run_id == run_id, self.table.c.status == "pending",
      )).all()
      for row in rows:
        payload = dict(row.payload)
        payload.update(status="cancelled", resolved=now_string())
        connection.execute(update(self.table).where(self.table.c.id == payload["id"]).values(
          status="cancelled", delivery="none", payload=payload,
        ))
        events.append(self.append_event(connection, payload))
    return events

  def outbox(self):
    with self.chat.engine.connect() as connection:
      return [dict(row.payload) for row in connection.execute(select(self.table.c.payload).where(
        self.table.c.delivery == "pending",
      ))]

  def delivered(self, request_id):
    with self.chat.engine.begin() as connection:
      connection.execute(update(self.table).where(self.table.c.id == request_id).values(delivery="delivered"))
