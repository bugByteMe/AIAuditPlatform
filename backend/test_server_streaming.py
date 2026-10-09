from __future__ import annotations

import sys
import asyncio
import io
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server
from server import Handler, app
from server_workloads import BoundedExecutor, WorkloadBusy
from worker_upload import UploadStreamTimeout
from workspace_store import StorageError


class FakeChatRuntime:
  def resource_status(self):
    return {"cpuTotal": 4, "cpuAvailable": 3, "memoryTotalBytes": 8_000, "memoryAvailableBytes": 6_000}

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


class RawTerminalChatRuntime(FakeChatRuntime):
  def __init__(self):
    self.stored_events = [
      {
        "id": 1, "type": "command", "message": "Run command", "runId": "run-1",
        "status": "completed", "toolCallId": "call-1", "raw": {"item": {"aggregated_output": "large output"}},
      },
      {"id": 2, "type": "completed", "message": "Run completed.", "runId": "run-1"},
    ]

  def events(self, workspace_id, session_id, after, user, *, before=None, limit=500, latest=False):
    return [event for event in self.stored_events if event["id"] > after]

  def wait_events(self, workspace_id, session_id, after, user, timeout=15):
    return self.events(workspace_id, session_id, after, user)


class FakeUploadManager:
  def __init__(self):
    self.source = None

  def receive_chunk(self, upload_id, user, index, offset, source, length):
    self.source = source
    return {"id": upload_id, "offsets": [length], "status": "uploading"}


class BusyPool:
  async def run(self, *_args):
    raise WorkloadBusy("file")


class BusyPools:
  def pool(self, _workload):
    return BusyPool()


class RoutedPools:
  def __init__(self, file_pool):
    self.file_pool = file_pool

  def pool(self, workload):
    if workload != "file":
      raise AssertionError(f"unexpected workload: {workload}")
    return self.file_pool


class ServerStreamingTest(unittest.TestCase):
  def test_server_entrypoint_uses_configured_host_and_port(self) -> None:
    with patch.object(sys, "argv", ["server.py"]), patch("uvicorn.run") as run:
      server.main()
    run.assert_called_once_with(app, host=server.SETTINGS.host, port=server.SETTINGS.port)

  def test_fastapi_health_and_options_preserve_transport_contract(self) -> None:
    with TestClient(app) as client, patch("server_asgi.workload_pools", side_effect=AssertionError("default route used dedicated pool")):
      response = client.get("/api/health", headers={"Origin": "https://audit.example"})
      self.assertEqual(response.status_code, 200)
      self.assertEqual(response.json(), {"ok": True})
      self.assertEqual(response.headers["cache-control"], "no-store")
      self.assertEqual(response.headers["access-control-allow-origin"], "https://audit.example")
      options = client.options("/api/health", headers={"Origin": "https://audit.example"})
      self.assertEqual(options.status_code, 204)
      self.assertEqual(options.headers["access-control-allow-credentials"], "true")

  def test_dedicated_pool_overload_returns_retryable_service_unavailable(self) -> None:
    with TestClient(app) as client, patch("server_asgi.workload_pools", return_value=BusyPools()):
      response = client.get(
        "/api/workspaces/workspace-1/download?mode=full",
        headers={"Origin": "https://audit.example"},
      )
    self.assertEqual(response.status_code, 503)
    self.assertEqual(response.json(), {"error": "server_busy", "workload": "file", "message": "server is busy; retry shortly"})
    self.assertEqual(response.headers["retry-after"], "2")
    self.assertEqual(response.headers["cache-control"], "no-store")
    self.assertEqual(response.headers["access-control-allow-origin"], "https://audit.example")

  def test_saturated_file_pool_does_not_block_health_or_sse(self) -> None:
    file_pool = BoundedExecutor("file", 1, 0)
    routed = RoutedPools(file_pool)
    started = threading.Event()
    release = threading.Event()
    original_dispatch = server.server_asgi.dispatch_request

    def dispatch(request, body):
      if str(request.url.path).endswith("/download"):
        started.set()
        release.wait(2)
        return Response(content=b"bundle", media_type="application/zip")
      return original_dispatch(request, body)

    try:
      with TestClient(app) as client, patch("server_asgi.workload_pools", return_value=routed), patch(
        "server_asgi.dispatch_request", side_effect=dispatch
      ), patch.object(Handler, "require_user", return_value={"username": "user"}), patch(
        "server.CHAT_RUNTIME", TerminalChatRuntime()
      ), ThreadPoolExecutor(max_workers=1) as callers:
        first = callers.submit(client.get, "/api/workspaces/workspace-1/download?mode=full")
        self.assertTrue(started.wait(1))
        health = client.get("/api/health")
        stream = client.get("/api/workspaces/workspace-1/chat/stream?sessionId=chat-1&after=0")
        rejected = client.get("/api/workspaces/workspace-1/download?mode=full")
        release.set()
        completed = first.result(timeout=2)
      self.assertEqual(health.status_code, 200)
      self.assertEqual(stream.status_code, 200)
      self.assertIn("event: completed", stream.text)
      self.assertEqual(rejected.status_code, 503)
      self.assertEqual(completed.status_code, 200)
    finally:
      release.set()
      file_pool.shutdown()

  def test_fastapi_sse_preserves_event_framing(self) -> None:
    with TestClient(app) as client, patch.object(Handler, "require_user", return_value={"username": "user"}), patch(
      "server.CHAT_RUNTIME", TerminalChatRuntime()
    ):
      response = client.get("/api/workspaces/workspace-1/chat/stream?sessionId=chat-1&after=0")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
    self.assertIn("retry:", response.text)
    self.assertIn("id: 1\nevent: completed\n", response.text)

  def test_fastapi_sse_omits_stored_raw_command_output(self) -> None:
    runtime = RawTerminalChatRuntime()
    with TestClient(app) as client, patch.object(Handler, "require_user", return_value={"username": "user"}), patch(
      "server.CHAT_RUNTIME", runtime
    ):
      response = client.get("/api/workspaces/workspace-1/chat/stream?sessionId=chat-1&after=0")
    self.assertIn('id: 1\nevent: command\ndata: {"id": 1', response.text)
    self.assertIn('"toolCallId": "call-1"', response.text)
    self.assertNotIn('"raw"', response.text)
    self.assertEqual(runtime.stored_events[0]["raw"]["item"]["aggregated_output"], "large output")

  def test_legacy_sse_omits_stored_raw_command_output(self) -> None:
    handler = object.__new__(Handler)
    handler.headers = {}
    handler.wfile = io.BytesIO()
    handler.send_response = lambda *_args: None
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None
    runtime = RawTerminalChatRuntime()
    with patch("server.CHAT_RUNTIME", runtime):
      Handler.write_sse(handler, "workspace-1", "chat-1", 0, {"username": "user"})
    stream = handler.wfile.getvalue().decode("utf-8")
    self.assertIn('id: 1\nevent: command\ndata: {"id": 1', stream)
    self.assertNotIn('"raw"', stream)
    self.assertIn("raw", runtime.stored_events[0])

  def test_fastapi_sse_emits_terminal_heartbeat_without_polling(self) -> None:
    with TestClient(app) as client, patch.object(Handler, "require_user", return_value={"username": "user"}), patch(
      "server.CHAT_RUNTIME", FakeChatRuntime()
    ), patch("server.SETTINGS.sse_wait_timeout_seconds", 0.01):
      response = client.get("/api/workspaces/workspace-1/chat/stream?sessionId=chat-1&after=9")
    self.assertEqual(response.status_code, 200)
    self.assertIn("event: heartbeat\n", response.text)
    self.assertIn('"latestEventId": 9', response.text)
    self.assertIn('"sessionStatus": "completed"', response.text)

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
      [{
        "events": [], "sessionStatus": "completed",
        "session": {"id": "chat-1", "status": "completed", "events": []},
        "latestEventId": 9, "hasMore": False,
      }],
    )

  def test_history_response_omits_raw_without_changing_stored_events(self) -> None:
    handler = object.__new__(Handler)
    handler.require_user = lambda: {"username": "user", "role": "user", "group": "Audit"}
    responses = []
    handler.write_json = lambda payload, status=200, headers=None: responses.append(payload)
    runtime = RawTerminalChatRuntime()
    with patch("server.CHAT_RUNTIME", runtime):
      Handler.chat_api(handler, "GET", "workspace-1", "events", "", "sessionId=chat-1&latest=true&limit=200")
    command = responses[0]["events"][0]
    self.assertEqual((command["id"], command["type"], command["message"], command["status"], command["toolCallId"]),
                     (1, "command", "Run command", "completed", "call-1"))
    self.assertNotIn("raw", command)
    self.assertEqual(responses[0]["latestEventId"], 2)
    self.assertFalse(responses[0]["hasMore"])
    self.assertIn("raw", runtime.stored_events[0])

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

  def test_expired_upload_is_non_retryable(self) -> None:
    handler = object.__new__(Handler)
    status = handler.status_for_storage_error(StorageError("upload_session_expired", "expired"))
    self.assertEqual(status, 408)


class AsyncRequestReaderTest(unittest.IsolatedAsyncioTestCase):
  async def test_read_times_out_when_client_stops_sending_data(self) -> None:
    reader = server.server_asgi.AsyncRequestReader(asyncio.get_running_loop(), 0.01)
    with self.assertRaisesRegex(UploadStreamTimeout, "no data was received"):
      await asyncio.to_thread(reader.read, 1)


if __name__ == "__main__":
  unittest.main()
