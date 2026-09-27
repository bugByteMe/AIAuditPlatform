from __future__ import annotations

import json
import threading
import traceback
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from server_state import *
from server_utils import static_content_type
from wechat_pay import WechatPayError
from workspace_store import StorageError, parse_query


class RequestStopped(Exception):
  pass


class SseBroker:
  """Wake async SSE streams when runner threads persist a new event."""

  def __init__(self):
    self.loop: asyncio.AbstractEventLoop | None = None
    self.events: dict[str, asyncio.Event] = {}

  def bind(self, loop: asyncio.AbstractEventLoop) -> None:
    self.loop = loop

  def unbind(self) -> None:
    self.loop = None
    for event in self.events.values():
      event.set()
    self.events.clear()

  def persisted(self, session_id: str, _event: dict) -> None:
    loop = self.loop
    if loop and not loop.is_closed():
      try:
        loop.call_soon_threadsafe(self._set, session_id)
      except RuntimeError:
        pass

  def _set(self, session_id: str) -> None:
    self.events.setdefault(session_id, asyncio.Event()).set()

  def prepare(self, session_id: str) -> asyncio.Event:
    event = self.events.setdefault(session_id, asyncio.Event())
    event.clear()
    return event


SSE_BROKER = SseBroker()


class BaseHandler(BaseHTTPRequestHandler):
  def log_message(self, fmt: str, *args) -> None:
    return

  def do_GET(self) -> None:
    parsed = urlparse(self.path)
    if parsed.path.startswith("/api/"):
      self.handle_api("GET", parsed.path, parsed.query)
      return
    self.serve_static(parsed.path)

  def do_POST(self) -> None:
    parsed = urlparse(self.path)
    self.handle_api("POST", parsed.path, parsed.query)

  def do_PATCH(self) -> None:
    parsed = urlparse(self.path)
    self.handle_api("PATCH", parsed.path, parsed.query)

  def do_PUT(self) -> None:
    parsed = urlparse(self.path)
    self.handle_api("PUT", parsed.path, parsed.query)

  def do_DELETE(self) -> None:
    parsed = urlparse(self.path)
    self.handle_api("DELETE", parsed.path, parsed.query)

  def do_OPTIONS(self) -> None:
    self.send_response(HTTPStatus.NO_CONTENT)
    self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
    self.send_header("Access-Control-Allow-Credentials", "true")
    self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
    self.end_headers()

  def handle_api(self, method: str, path: str, query: str = "") -> None:
    try:
      if method == "GET" and path == "/api/health":
        self.write_json({"ok": True})
      elif method == "GET" and path == "/api/runtime-config":
        self.write_json(
          {
            "chatPollIntervalMs": SETTINGS.chat_poll_interval_ms,
            "sseRetryMs": SETTINGS.sse_retry_ms,
            "registrationMinPasswordLength": SETTINGS.registration_min_password_length,
            "batchInviteMaxCount": SETTINGS.batch_invite_max_count,
            "accountMaxSessionsLimit": SETTINGS.account_max_sessions_limit,
            "uploadChunkBytes": SETTINGS.upload_chunk_bytes,
          }
        )
      elif method == "GET" and path == "/api/session":
        self.require_session_response()
      elif method == "POST" and path == "/api/login":
        self.login()
      elif method == "POST" and path == "/api/register":
        self.register()
      elif method == "POST" and path == "/api/logout":
        self.logout()
      elif method == "GET" and path == "/api/accounts":
        self.accounts()
      elif method == "POST" and path == "/api/accounts/batch":
        self.create_account_batch()
      elif method == "POST" and path == "/api/admin/recharge-imports/preview":
        self.preview_recharge_import()
      elif method == "POST" and path == "/api/admin/recharge-imports":
        self.apply_recharge_import()
      elif method == "POST" and path.startswith("/api/accounts/") and path.endswith("/reset-budget"):
        self.reset_account_budget(path.split("/")[-2])
      elif method == "POST" and path.startswith("/api/accounts/") and path.endswith("/revoke-invite"):
        self.revoke_invite(path.split("/")[-2])
      elif method == "POST" and path == "/api/accounts":
        self.create_account()
      elif method == "DELETE" and path.startswith("/api/accounts/"):
        self.delete_account(path.rsplit("/", 1)[-1])
      elif method == "PATCH" and path.startswith("/api/accounts/"):
        self.update_account(path.rsplit("/", 1)[-1])
      elif method == "POST" and path == "/api/groups":
        self.create_group()
      elif method == "PATCH" and path.startswith("/api/groups/"):
        self.update_group(path.rsplit("/", 1)[-1])
      elif method == "DELETE" and path.startswith("/api/groups/"):
        self.delete_group(path.rsplit("/", 1)[-1])
      elif method == "GET" and path == "/api/audit-logs":
        self.audit_logs()
      elif method == "GET" and path == "/api/workers":
        self.workers()
      elif method == "GET" and path == "/api/codex-settings":
        self.codex_settings()
      elif method == "PATCH" and path == "/api/codex-settings":
        self.update_codex_settings()
      elif method == "GET" and path == "/api/recharge":
        self.recharge_info()
      elif method == "POST" and path == "/api/recharge/orders":
        self.create_recharge_order()
      elif path.startswith("/api/recharge/orders/"):
        self.recharge_order_api(method, path)
      elif method == "POST" and path == "/api/payments/wechat/notify":
        self.wechat_payment_notify()
      elif method == "GET" and path == "/api/workspaces":
        self.list_workspaces()
      elif method == "POST" and path == "/api/workspaces":
        raise StorageError("upload_sessions_required", "use /api/uploads for workspace creation")
      elif path == "/api/uploads" or path.startswith("/api/uploads/"):
        self.upload_api(method, path, query)
      elif path.startswith("/api/workspaces/"):
        self.workspace_api(method, path, query)
      else:
        self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
    except StorageError as exc:
      self.write_json({"error": exc.code, "message": exc.message}, self.status_for_storage_error(exc))
    except ValueError as exc:
      self.write_json({"error": "bad_request", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
    except WechatPayError as exc:
      status = HTTPStatus.SERVICE_UNAVAILABLE if exc.code == "wechat_pay_unavailable" else HTTPStatus.BAD_REQUEST
      self.write_json({"error": exc.code, "message": exc.message}, status)
    except RequestStopped:
      return
    except Exception:
      traceback.print_exc()
      self.write_json({"error": "internal_error", "message": "internal server error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

  def status_for_storage_error(self, exc: StorageError) -> HTTPStatus:
    if exc.code == "not_found":
      return HTTPStatus.NOT_FOUND
    if exc.code == "forbidden":
      return HTTPStatus.FORBIDDEN
    if exc.code in {
      "workspace_locked", "workspace_changed", "upload_offset_mismatch", "upload_chunk_conflict",
      "upload_commit_started", "runs_not_stopped", "protected_admin", "session_run_active",
      "concurrent_confirmation_required", "session_not_forkable",
      "group_live_run_limit_reached",
    }:
      return HTTPStatus.CONFLICT
    if exc.code in {"budget_exhausted"}:
      return HTTPStatus.PAYMENT_REQUIRED
    if exc.code == "budget_provider_unavailable":
      return HTTPStatus.SERVICE_UNAVAILABLE
    if exc.code in {"codex_auth_required", "no_compatible_worker", "no_upload_worker"}:
      return HTTPStatus.PRECONDITION_REQUIRED
    if exc.code == "upload_worker_unavailable":
      return HTTPStatus.SERVICE_UNAVAILABLE
    if exc.code in {"file_too_large", "workspace_too_large", "too_many_files", "group_disk_quota_exceeded"}:
      return HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    return HTTPStatus.BAD_REQUEST

  def write_sse(self, workspace_id: str, session_id: str, after: int, user: dict) -> None:
    header_last_id = int(self.headers.get("Last-Event-ID") or 0)
    last_id = max(after, header_last_id)
    CHAT_RUNTIME.events(workspace_id, session_id, last_id, user)
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Connection", "keep-alive")
    self.send_header("X-Accel-Buffering", "no")
    origin = self.headers.get("Origin")
    if origin:
      self.send_header("Access-Control-Allow-Origin", origin)
      self.send_header("Access-Control-Allow-Credentials", "true")
    self.end_headers()
    try:
      self.wfile.write(f"retry: {SETTINGS.sse_retry_ms}\n\n".encode("utf-8"))
      self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
      return
    idle_rounds = 0
    while idle_rounds < SETTINGS.sse_max_idle_rounds:
      events = CHAT_RUNTIME.wait_events(workspace_id, session_id, last_id, user, timeout=SETTINGS.sse_wait_timeout_seconds)
      if not events:
        public_session = CHAT_RUNTIME.public_session(session_id, include_events=False)
        heartbeat = {
          "sessionId": session_id,
          "latestEventId": last_id,
          "sessionStatus": public_session.get("status"),
        }
        idle_rounds += 1
        try:
          self.wfile.write(f"event: heartbeat\ndata: {json.dumps(heartbeat, ensure_ascii=False)}\n\n".encode("utf-8"))
          self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
          return
        if public_session.get("status") in {"completed", "stopped", "failed"}:
          return
        continue
      idle_rounds = 0
      for event in events:
        last_id = int(event["id"])
        payload = json.dumps(event, ensure_ascii=False)
        try:
          self.wfile.write(f"id: {last_id}\nevent: {event['type']}\ndata: {payload}\n\n".encode("utf-8"))
          self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
          return
      if events[-1]["type"] in {"completed", "stopped", "failed"}:
        return

  def require_session_response(self) -> None:
    user = self.current_user()
    if not user:
      self.write_json({"user": None}, HTTPStatus.UNAUTHORIZED)
      return
    refresh_micu_balance(user)
    self.write_json({"user": public_user(user)})

  def require_admin(self) -> dict:
    user = self.current_user()
    if not user:
      self.write_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
      raise RequestStopped()
    if user["role"] != "system_admin":
      self.write_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
      raise RequestStopped()
    return user

  def require_user(self) -> dict:
    user = self.current_user()
    if not user:
      self.write_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
      raise RequestStopped()
    return user

  def current_user(self) -> dict | None:
    token = self.session_token()
    if not token:
      return None
    with SESSION_LOCK:
      session = SESSIONS.get(token)
      now = time.time()
      if not session or session["expiresAt"] <= now:
        SESSIONS.pop(token, None)
        return None
      session["expiresAt"] = now + SESSION_TTL_SECONDS
      username = session["username"]
    return USERS.get(username)

  def session_token(self) -> str | None:
    authorization = self.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
      return authorization.split(" ", 1)[1].strip() or None
    parsed = urlparse(self.path)
    query_token = (parse_query(parsed.query).get("access_token") or [""])[0]
    if query_token:
      return query_token
    cookie_header = self.headers.get("Cookie")
    if not cookie_header:
      return None
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel else None

  def read_json(self) -> dict:
    length = int(self.headers.get("Content-Length") or 0)
    if length == 0:
      return {}
    raw = self.rfile.read(length).decode("utf-8")
    return json.loads(raw)

  def write_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK, headers: dict | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    self.write_binary(body, "application/json; charset=utf-8", status, headers, no_store=True)

  def write_binary(
    self,
    body: bytes,
    content_type: str,
    status: HTTPStatus = HTTPStatus.OK,
    headers: dict | None = None,
    no_store: bool = False,
  ) -> None:
    self.send_response(status)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(len(body)))
    if no_store:
      self.send_header("Cache-Control", "no-store")
    origin = self.headers.get("Origin")
    if origin:
      self.send_header("Access-Control-Allow-Origin", origin)
      self.send_header("Access-Control-Allow-Credentials", "true")
    for key, value in (headers or {}).items():
      self.send_header(key, value)
    self.end_headers()
    self.wfile.write(body)
    if headers and str(headers.get("Connection", "")).lower() == "close":
      self.wfile.flush()
      self.close_connection = True

  def write_file(self, file_path: Path, content_type: str, filename: str, disposition: str) -> None:
    ascii_filename = ascii_download_filename(filename)
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Disposition", f"{disposition}; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}")
    self.send_header("X-Content-Type-Options", "nosniff")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Connection", "close")
    origin = self.headers.get("Origin")
    if origin:
      self.send_header("Access-Control-Allow-Origin", origin)
      self.send_header("Access-Control-Allow-Credentials", "true")
    self.end_headers()
    with file_path.open("rb") as source:
      while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
          break
        self.wfile.write(chunk)
    self.wfile.flush()
    self.close_connection = True

  def serve_static(self, request_path: str) -> None:
    relative = unquote(request_path.lstrip("/")) or "index.html"
    candidate = (FRONTEND_DIR / relative).resolve()
    if not str(candidate).startswith(str(FRONTEND_DIR.resolve())) or not candidate.is_file():
      candidate = FRONTEND_DIR / "index.html"
    content_type = static_content_type(candidate)
    body = candidate.read_bytes()
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    self.wfile.write(body)

