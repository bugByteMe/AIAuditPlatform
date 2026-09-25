from __future__ import annotations

import sys
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import Handler, app


class FakeChatRuntime:
  def events(self, workspace_id, session_id, after, user):
    return []

  def public_session(self, session_id, include_events=True):
    return {"id": session_id, "status": "completed", "events": [] if not include_events else []}

  def fork_session(self, workspace_id, session_id, user, title):
    return {"id": "chat-fork", "title": title, "forkedFromSessionId": session_id}

  def delete_session(self, workspace_id, session_id, user):
    return {"id": session_id, "runIds": ["run-1"]}


class TerminalChatRuntime(FakeChatRuntime):
  def events(self, workspace_id, session_id, after, user):
    if after:
      return []
    return [{"id": 1, "type": "completed", "message": "Run completed.", "runId": "run-1"}]


class FakeUploadManager:
  def __init__(self):
    self.source = None

  def receive_chunk(self, upload_id, user, index, offset, source, length):
    self.source = source
    return {"id": upload_id, "offsets": [length], "status": "uploading"}


class ServerStreamingTest(unittest.TestCase):
  def test_fastapi_health_and_options_preserve_transport_contract(self) -> None:
    with TestClient(app) as client:
      response = client.get("/api/health", headers={"Origin": "https://audit.example"})
      self.assertEqual(response.status_code, 200)
      self.assertEqual(response.json(), {"ok": True})
      self.assertEqual(response.headers["cache-control"], "no-store")
      self.assertEqual(response.headers["access-control-allow-origin"], "https://audit.example")
      options = client.options("/api/health", headers={"Origin": "https://audit.example"})
      self.assertEqual(options.status_code, 204)
      self.assertEqual(options.headers["access-control-allow-credentials"], "true")

  def test_fastapi_sse_preserves_event_framing(self) -> None:
    with TestClient(app) as client, patch.object(Handler, "require_user", return_value={"username": "user"}), patch(
      "server.CHAT_RUNTIME", TerminalChatRuntime()
    ):
      response = client.get("/api/workspaces/workspace-1/chat/stream?sessionId=chat-1&after=0")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
    self.assertIn("retry:", response.text)
    self.assertIn("id: 1\nevent: completed\n", response.text)

  def test_fastapi_upload_chunk_uses_stream_reader(self) -> None:
    manager = FakeUploadManager()
    with TestClient(app) as client, patch.object(Handler, "require_user", return_value={"username": "user"}), patch(
      "server.UPLOAD_MANAGER", manager
    ):
      response = client.put("/api/uploads/upload-1/files/0?offset=0", content=b"abc")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["upload"]["offsets"], [3])
    self.assertIsNotNone(manager.source)
    self.assertNotIsInstance(manager.source, io.BytesIO)

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

  def test_chat_delete_route_removes_persisted_session_and_audits(self) -> None:
    handler = object.__new__(Handler)
    handler.require_user = lambda: {"username": "owner", "role": "user", "group": "Audit"}
    responses = []
    handler.write_json = lambda payload, status=200, headers=None: responses.append((payload, status))
    with patch("server.CHAT_RUNTIME", FakeChatRuntime()), patch("server.add_audit") as add_audit:
      Handler.chat_api(handler, "DELETE", "workspace-1", "sessions", "chat-source", "")
    self.assertEqual(responses[0][0], {"deleted": {"id": "chat-source", "runIds": ["run-1"]}})
    add_audit.assert_called_once_with("owner", "chat session deleted", "workspace-1 chat-source runs=1")

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
