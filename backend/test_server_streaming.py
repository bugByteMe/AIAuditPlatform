from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import Handler


class FakeChatRuntime:
  def events(self, workspace_id, session_id, after, user):
    return []

  def public_session(self, session_id):
    return {"id": session_id, "status": "completed"}


class ServerStreamingTest(unittest.TestCase):
  def test_event_reconciliation_reports_terminal_status_without_new_events(self) -> None:
    handler = object.__new__(Handler)
    handler.require_user = lambda: {"username": "user", "role": "user", "group": "Audit"}
    responses = []
    handler.write_json = lambda payload, status=200, headers=None: responses.append(payload)
    with patch("server.CHAT_RUNTIME", FakeChatRuntime()):
      Handler.chat_api(handler, "GET", "workspace-1", "events", "", "sessionId=chat-1&after=9")
    self.assertEqual(
      responses,
      [{"events": [], "sessionStatus": "completed", "session": {"id": "chat-1", "status": "completed"}}],
    )


if __name__ == "__main__":
  unittest.main()
