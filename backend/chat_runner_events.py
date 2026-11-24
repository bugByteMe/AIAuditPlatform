from __future__ import annotations

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from workspace_store import now_string


class ChatRunnerEventMixin:
  def commit_runner_event(
    self,
    run_id: str,
    event: dict,
    *,
    container: str = "",
    source_event_id: int | None = None,
  ) -> dict:
    """Atomically persist one runner event and all derived run/session usage state."""
    source_id = int(source_event_id) if source_event_id is not None else None
    for _ in range(3):
      try:
        with self.engine.begin() as connection:
          if source_id is not None:
            receipt = connection.execute(select(self.receipt_table).where(
              self.receipt_table.c.run_id == run_id,
              self.receipt_table.c.source_event_id == source_id,
            )).mappings().first()
            if receipt:
              persisted = connection.execute(select(self.event_table.c.payload).where(
                self.event_table.c.session_id == receipt["session_id"],
                self.event_table.c.event_id == receipt["event_id"],
              )).scalar_one()
              return {"inserted": False, "event": dict(persisted), "run": None, "session": None, "userTotal": None}

          run_row = connection.execute(
            select(self.run_table.c.payload).where(self.run_table.c.id == run_id).with_for_update()
          ).first()
          if run_row is None:
            raise KeyError(run_id)
          run = dict(run_row.payload)
          session_id = str(run["sessionId"])
          session_row = connection.execute(
            select(self.session_table.c.payload).where(self.session_table.c.id == session_id).with_for_update()
          ).first()
          if session_row is None:
            raise KeyError(session_id)
          session = dict(session_row.payload)

          self.input_requests.ingest(connection, run, event)
          timestamp = now_string()
          if run.get("status") in {"completed", "failed", "stopped"}:
            raise ValueError("runner event for terminal run")
          if run.get("status") != "stopping":
            run["status"] = "running"
          run["updated"] = timestamp
          if container:
            run["container"] = container
          if source_id is not None:
            run["workerEventCursor"] = max(int(run.get("workerEventCursor") or 0), source_id)

          delta = 0
          user_total = None
          if "tokens" in event:
            reported = max(0, int(event.get("tokens") or 0))
            previous = max(0, int(run.get("tokens") or 0))
            delta = max(0, reported - previous)
            run["tokens"] = max(previous, reported)
            for event_key, run_key in [
              ("inputTokens", "inputTokens"),
              ("cachedInputTokens", "cachedInputTokens"),
              ("outputTokens", "outputTokens"),
            ]:
              if event_key in event:
                run[run_key] = max(int(run.get(run_key) or 0), int(event.get(event_key) or 0))
            session["totalTokens"] = int(session.get("totalTokens") or 0) + delta
            session["tokens"] = f"{session['totalTokens']:,}"
            username = str(run.get("user") or "")
            if username:
              if delta:
                updated_usage = connection.execute(update(self.usage_table).where(
                  self.usage_table.c.username == username
                ).values(total_tokens=self.usage_table.c.total_tokens + delta))
                if not updated_usage.rowcount:
                  connection.execute(insert(self.usage_table).values(username=username, total_tokens=delta))
              usage_row = connection.execute(select(self.usage_table.c.total_tokens).where(
                self.usage_table.c.username == username
              )).first()
              user_total = int(usage_row.total_tokens) if usage_row is not None else None

          if event.get("codexSessionId"):
            session["codexSessionId"] = event["codexSessionId"]
            session["codexNativeResumable"] = True
            session["codexForkPending"] = False
            session["codexForkSourceId"] = None
            session["codexHomeUser"] = run["user"]
            run["codexSessionId"] = event["codexSessionId"]
          session["updated"] = timestamp

          counter = connection.execute(select(self.counter_table.c.next_id).where(
            self.counter_table.c.session_id == session_id
          ).with_for_update()).first()
          if counter is None:
            connection.execute(insert(self.counter_table).values(session_id=session_id, next_id=2))
            event_id = 1
          else:
            event_id = int(counter.next_id)
            connection.execute(update(self.counter_table).where(
              self.counter_table.c.session_id == session_id
            ).values(next_id=event_id + 1))
          persisted = {
            "id": event_id,
            "time": timestamp,
            "type": str(event.get("type") or "progress"),
            "message": str(event.get("message") or event.get("type") or "progress"),
            "runId": run_id,
          }
          for key in ["raw", "status", "toolCallId", "inputRequest"]:
            if key in event:
              persisted[key] = event[key]
          connection.execute(insert(self.event_table).values(**self._event_row(session_id, persisted)))
          if source_id is not None:
            connection.execute(insert(self.receipt_table).values(
              run_id=run_id, source_event_id=source_id, session_id=session_id, event_id=event_id,
            ))

          self._update_run(connection, run)
          self._update_session(connection, session)
          return {
            "inserted": True, "event": persisted, "run": run, "session": session,
            "usageDelta": delta, "userTotal": user_total,
          }
      except IntegrityError:
        if source_id is None:
          continue
    raise RuntimeError("could not atomically persist runner event")

