from __future__ import annotations

import json
from pathlib import Path

from workspace_store import generated_id, now_string


class ChatStore:
  def __init__(self, root: Path):
    self.root = root
    self.sessions_path = root / "sessions.json"
    self.runs_path = root / "runs.json"
    self.events_dir = root / "events"
    self.ensure_layout()

  def ensure_layout(self) -> None:
    self.events_dir.mkdir(parents=True, exist_ok=True)
    if not self.sessions_path.exists():
      self.save_json(self.sessions_path, {})
    if not self.runs_path.exists():
      self.save_json(self.runs_path, {})

  def load_json(self, path: Path, default):
    self.ensure_layout()
    if not path.exists():
      return default
    return json.loads(path.read_text(encoding="utf-8"))

  def save_json(self, path: Path, payload) -> None:
    self.root.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)

  def sessions(self) -> dict:
    return self.load_json(self.sessions_path, {})

  def runs(self) -> dict:
    return self.load_json(self.runs_path, {})

  def save_sessions(self, sessions: dict) -> None:
    self.save_json(self.sessions_path, sessions)

  def save_runs(self, runs: dict) -> None:
    self.save_json(self.runs_path, runs)

  def get_session(self, session_id: str) -> dict | None:
    return self.sessions().get(session_id)

  def save_session(self, session: dict) -> None:
    sessions = self.sessions()
    sessions[session["id"]] = session
    self.save_sessions(sessions)

  def get_run(self, run_id: str) -> dict | None:
    return self.runs().get(run_id)

  def save_run(self, run: dict) -> None:
    runs = self.runs()
    runs[run["id"]] = self.sanitize_run(run)
    self.save_runs(runs)

  def events_path(self, session_id: str) -> Path:
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in session_id).strip("._")
    return self.events_dir / f"{safe or generated_id('chat')}.jsonl"

  def events(self, session_id: str) -> list[dict]:
    path = self.events_path(session_id)
    if not path.exists():
      return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
      if line.strip():
        events.append(json.loads(line))
    return events

  def append_event(self, session_id: str, event: dict) -> dict:
    path = self.events_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {**event, "id": self.last_event_id(path) + 1}
    with path.open("a", encoding="utf-8") as handle:
      handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return event

  def last_event_id(self, path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
      return 0
    with path.open("rb") as handle:
      position = handle.seek(0, 2)
      tail = b""
      while position > 0:
        size = min(4096, position)
        position -= size
        handle.seek(position)
        tail = handle.read(size) + tail
        last_line = tail.rstrip().rsplit(b"\n", 1)[-1]
        if position == 0 or b"\n" in tail:
          try:
            return int(json.loads(last_line).get("id") or 0)
          except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            if position == 0:
              raise
    return 0

  def replace_events(self, session_id: str, events: list[dict]) -> None:
    path = self.events_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
      for event_id, event in enumerate(events, start=1):
        payload = {**event, "id": event_id}
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)

  def public_session(self, session_id: str, include_events: bool = True) -> dict:
    session = self.get_session(session_id)
    if not session:
      return {"id": session_id, "title": "Unknown session", "status": "failed", "updated": now_string(), "tokens": "0", "events": []}
    events = self.public_events(session_id) if include_events else []
    return {
      "id": session["id"],
      "title": session["title"],
      "status": session["status"],
      "updated": session["updated"],
      "tokens": session.get("tokens", "0"),
      "latestRunId": session.get("latestRunId"),
      "codexNativeResumable": bool(session.get("codexNativeResumable")),
      "forkedFromSessionId": session.get("forkedFromSessionId"),
      "events": [self.public_event_tuple(event) for event in events],
    }

  def public_events(self, session_id: str) -> list[dict]:
    visible = []
    tool_indexes = {}
    for event in self.events(session_id):
      key = self.tool_event_key(event)
      if key and key in tool_indexes:
        visible[tool_indexes[key]] = {**visible[tool_indexes[key]], **event}
        continue
      if key:
        tool_indexes[key] = len(visible)
      visible.append(event)
    return visible

  def public_event_tuple(self, event: dict) -> list:
    return [
      event["type"],
      event["message"],
      event["message"],
      event.get("runId") or "",
      event.get("id") or 0,
      event.get("status") or "",
      event.get("toolCallId") or "",
    ]

  def tool_event_key(self, event: dict) -> tuple | None:
    if event.get("type") not in {"command", "tool"} or not event.get("toolCallId"):
      return None
    return (event.get("runId") or "", event["type"], event["toolCallId"])

  def public_workspace_sessions(self, workspace: dict) -> list[dict]:
    sessions = self.sessions()
    items = []
    for item in workspace.get("sessions", []):
      session_id = item.get("id")
      if session_id in sessions:
        items.append(self.public_session(session_id))
      elif {"title", "status", "updated", "tokens", "events"}.issubset(item):
        if item.get("title") not in {"Workspace setup", "Fork created"}:
          items.append(item)
    return items

  def sanitize_run(self, run: dict) -> dict:
    cleaned = dict(run)
    cleaned.pop("codexSettings", None)
    cleaned.pop("codexHome", None)
    return cleaned

  def delete_workspaces(self, workspace_ids: set[str]) -> dict:
    sessions = self.sessions()
    runs = self.runs()
    removed_session_ids = {
      session_id
      for session_id, session in sessions.items()
      if str(session.get("workspaceId") or "") in workspace_ids
    }
    removed_run_ids = {
      run_id
      for run_id, run in runs.items()
      if str(run.get("workspaceId") or "") in workspace_ids
    }
    for session_id in removed_session_ids:
      sessions.pop(session_id, None)
      path = self.events_path(session_id)
      if path.exists():
        path.unlink()
    for run_id in removed_run_ids:
      runs.pop(run_id, None)
    self.save_sessions(sessions)
    self.save_runs(runs)
    return {"sessionIds": sorted(removed_session_ids), "runIds": sorted(removed_run_ids)}
