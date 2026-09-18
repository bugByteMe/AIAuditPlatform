from __future__ import annotations

import sys
import io
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

  def fork_session(self, workspace_id, session_id, user, title):
    return {"id": "chat-fork", "title": title, "forkedFromSessionId": session_id}


class FakeUploadManager:
  def __init__(self):
    self.source = None

  def receive_chunk(self, upload_id, user, index, offset, source, length):
    self.source = source
    return {"id": upload_id, "offsets": [length], "status": "uploading"}


class ServerStreamingTest(unittest.TestCase):
  def test_chat_fork_route_returns_persisted_session(self) -> None:
    handler = object.__new__(Handler)
    handler.require_user = lambda: {"username": "user", "role": "user", "group": "Audit"}
    handler.read_json = lambda: {"title": "Branch"}
    responses = []
    handler.write_json = lambda payload, status=200, headers=None: responses.append((payload, status))
    with patch("server.CHAT_RUNTIME", FakeChatRuntime()), patch("server.add_audit"):
      Handler.chat_api(handler, "POST", "workspace-1", "sessions", "chat-source", "", "fork")
    self.assertEqual(responses[0][0]["session"]["id"], "chat-fork")
    self.assertEqual(responses[0][0]["session"]["forkedFromSessionId"], "chat-source")
    self.assertEqual(responses[0][1], 201)

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

  def test_upload_chunk_passes_request_stream_without_reading_it_into_memory(self) -> None:
    handler = object.__new__(Handler)
    handler.require_user = lambda: {"username": "user", "role": "user", "group": "Audit"}
    handler.headers = {"Content-Length": "3"}
    handler.rfile = io.BytesIO(b"abc")
    responses = []
    handler.write_json = lambda payload, status=200, headers=None: responses.append(payload)
    manager = FakeUploadManager()
    with patch("server.UPLOAD_MANAGER", manager):
      Handler.upload_api(handler, "PUT", "/api/uploads/upload_1/files/0", "offset=0")
    self.assertIs(manager.source, handler.rfile)
    self.assertEqual(handler.rfile.tell(), 0)
    self.assertEqual(responses[0]["upload"]["offsets"], [3])


if __name__ == "__main__":
  unittest.main()
