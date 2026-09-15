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
    events = self.events(session_id)
    event = {**event, "id": len(events) + 1}
    path = self.events_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
      handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return event

  def public_session(self, session_id: str) -> dict:
    session = self.get_session(session_id)
    if not session:
      return {"id": session_id, "title": "Unknown session", "status": "failed", "updated": now_string(), "tokens": "0", "events": []}
    events = self.events(session_id)
    return {
      "id": session["id"],
      "title": session["title"],
      "status": session["status"],
      "updated": session["updated"],
      "tokens": session.get("tokens", "0"),
      "latestRunId": session.get("latestRunId"),
      "codexNativeResumable": bool(session.get("codexNativeResumable")),
      "events": [[event["type"], event["message"], event["message"]] for event in events],
    }

  def public_workspace_sessions(self, workspace: dict) -> list[dict]:
    sessions = self.sessions()
    items = []
    for item in workspace.get("sessions", []):
      session_id = item.get("id")
      if session_id in sessions:
        items.append(self.public_session(session_id))
      elif {"title", "status", "updated", "tokens", "events"}.issubset(item):
        items.append(item)
    return items

  def migrate_from_metadata(self, metadata: dict) -> bool:
    old_sessions = metadata.pop("chatSessions", None)
    old_runs = metadata.pop("runs", None)
    old_events = metadata.pop("events", None)
    changed = any(value is not None for value in [old_sessions, old_runs, old_events])
    if old_sessions:
      sessions = self.sessions()
      sessions.update(old_sessions)
      self.save_sessions(sessions)
    if old_runs:
      runs = self.runs()
      runs.update({run_id: self.sanitize_run(run) for run_id, run in old_runs.items()})
      self.save_runs(runs)
    if old_events:
      for session_id, events in old_events.items():
        path = self.events_path(session_id)
        if path.exists():
          continue
        for event in events:
          self.append_event(session_id, event)
    return changed

  def sanitize_run(self, run: dict) -> dict:
    cleaned = dict(run)
    cleaned.pop("codexSettings", None)
    cleaned.pop("codexHome", None)
    return cleaned
