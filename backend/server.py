#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import secrets
import time
import traceback
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from account_store import AccountStore
from chat_runtime import ChatRuntime
from config import SETTINGS
from workspace_store import StorageError, WorkspaceStore, parse_multipart, parse_query, parse_urlencoded_paths


FRONTEND_DIR = SETTINGS.frontend_dir
WORKSPACE_STORAGE_DIR = SETTINGS.workspace_storage_dir
SESSION_COOKIE = SETTINGS.session_cookie
SESSION_TTL_SECONDS = SETTINGS.session_ttl_seconds
PBKDF2_ITERATIONS = SETTINGS.pbkdf2_iterations
WORKSPACE_STORE = WorkspaceStore(WORKSPACE_STORAGE_DIR)


def static_content_type(file_path: Path) -> str:
  if file_path.suffix.lower() in {".js", ".mjs"}:
    return "text/javascript"
  return mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"


def hash_password(password: str, salt: str | None = None) -> str:
  salt = salt or secrets.token_hex(16)
  digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS)
  return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
  try:
    scheme, iterations, salt, expected = encoded.split("$", 3)
    if scheme != "pbkdf2_sha256":
      return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(digest.hex(), expected)
  except ValueError:
    return False


SEED_USERS = {
  "chen.audit": {
    "username": "chen.audit",
    "displayName": "陈审计",
    "role": "system_admin",
    "group": "审计一组",
    "budgetTokens": 3_000_000,
    "usedTokens": 1_900_000,
    "enabled": True,
      "maxSessions": 2,
      "codex": {
        "baseUrl": SETTINGS.default_codex_base_url,
        "apiKey": SETTINGS.default_codex_api_key,
      },
      "passwordHash": hash_password("audit123", "00112233445566778899aabbccddeeff"),
  },
  "li.review": {
    "username": "li.review",
    "displayName": "李复核",
    "role": "group_admin",
    "group": "审计一组",
    "budgetTokens": 2_000_000,
    "usedTokens": 940_000,
    "enabled": True,
      "maxSessions": 1,
      "codex": {
        "baseUrl": SETTINGS.default_codex_base_url,
        "apiKey": SETTINGS.default_codex_api_key,
      },
      "passwordHash": hash_password("review123", "ffeeddccbbaa99887766554433221100"),
  },
}

ACCOUNT_STORE = AccountStore(WORKSPACE_STORAGE_DIR / "accounts.json", SEED_USERS)
USERS = ACCOUNT_STORE.users
CHAT_RUNTIME = ChatRuntime(WORKSPACE_STORE, USERS, capacity=SETTINGS.local_run_capacity, save_users=ACCOUNT_STORE.save)

SESSIONS: dict[str, dict] = {}
AUDIT_LOGS = [
  {"time": "2026-09-12 23:12", "actor": "chen.audit", "event": "login success", "detail": "seed audit log"},
]


class RequestStopped(Exception):
  pass


def public_user(user: dict) -> dict:
  public = {key: value for key, value in user.items() if key not in {"passwordHash", "codex", "inviteToken"}}
  codex = user.get("codex") or {}
  public["codex"] = {
    "baseUrl": codex.get("baseUrl", ""),
    "apiKeyConfigured": bool(codex.get("apiKey")),
  }
  return public


def admin_account(account: dict) -> dict:
  public = {key: value for key, value in account.items() if key not in {"passwordHash", "codex"}}
  if public.get("status") == "active" and not public.get("enabled", True):
    public["status"] = "disabled"
  return public


def user_by_username(username: str) -> dict | None:
  normalized = username.strip().casefold()
  return next((user for name, user in USERS.items() if name.casefold() == normalized), None)


def user_by_identifier(identifier: str) -> dict | None:
  user = USERS.get(identifier)
  if user:
    return user
  return next((item for item in USERS.values() if item.get("id") == identifier), None)


def add_audit(actor: str, event: str, detail: str = "") -> None:
  AUDIT_LOGS.insert(
    0,
    {
      "time": time.strftime("%Y-%m-%d %H:%M:%S"),
      "actor": actor,
      "event": event,
      "detail": detail,
    },
  )
  del AUDIT_LOGS[100:]


def active_sessions_for(username: str) -> int:
  now = time.time()
  return sum(1 for session in SESSIONS.values() if session["username"] == username and session["expiresAt"] > now)


def ascii_download_filename(filename: str, fallback: str = "download") -> str:
  safe = "".join(char if ord(char) < 128 and (char.isalnum() or char in {".", "-", "_"}) else "_" for char in filename)
  safe = safe.strip("._")
  return safe or fallback


class Handler(BaseHTTPRequestHandler):
  server_version = "AIAuditBackend/0.1"

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

  def do_DELETE(self) -> None:
    parsed = urlparse(self.path)
    self.handle_api("DELETE", parsed.path, parsed.query)

  def do_OPTIONS(self) -> None:
    self.send_response(HTTPStatus.NO_CONTENT)
    self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
    self.send_header("Access-Control-Allow-Credentials", "true")
    self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
    self.end_headers()

  def handle_api(self, method: str, path: str, query: str = "") -> None:
    try:
      if method == "GET" and path == "/api/health":
        self.write_json({"ok": True})
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
      elif method == "POST" and path.startswith("/api/accounts/") and path.endswith("/revoke-invite"):
        self.revoke_invite(path.split("/")[-2])
      elif method == "POST" and path == "/api/accounts":
        self.create_account()
      elif method == "PATCH" and path.startswith("/api/accounts/"):
        self.update_account(path.rsplit("/", 1)[-1])
      elif method == "GET" and path == "/api/audit-logs":
        self.audit_logs()
      elif method == "GET" and path == "/api/codex-settings":
        self.codex_settings()
      elif method == "PATCH" and path == "/api/codex-settings":
        self.update_codex_settings()
      elif method == "GET" and path == "/api/workspaces":
        self.list_workspaces()
      elif method == "POST" and path == "/api/workspaces":
        self.create_workspace()
      elif path.startswith("/api/workspaces/"):
        self.workspace_api(method, path, query)
      else:
        self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
    except StorageError as exc:
      self.write_json({"error": exc.code, "message": exc.message}, self.status_for_storage_error(exc))
    except ValueError as exc:
      self.write_json({"error": "bad_request", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
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
    if exc.code in {"workspace_locked"}:
      return HTTPStatus.CONFLICT
    if exc.code in {"budget_exhausted"}:
      return HTTPStatus.PAYMENT_REQUIRED
    if exc.code in {"codex_auth_required"}:
      return HTTPStatus.PRECONDITION_REQUIRED
    if exc.code in {"file_too_large", "workspace_too_large", "too_many_files"}:
      return HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    return HTTPStatus.BAD_REQUEST

  def login(self) -> None:
    payload = self.read_json()
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    user = user_by_username(username)
    if not user or not verify_password(password, user["passwordHash"]):
      add_audit(username or "anonymous", "login failed", "invalid credentials")
      self.write_json({"error": "invalid_credentials"}, HTTPStatus.UNAUTHORIZED)
      return
    self.start_session(user, "login success")

  def register(self) -> None:
    payload = self.read_json()
    invite_token = str(payload.get("inviteToken") or "").strip()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not invite_token or not username or not password:
      raise ValueError("invite token, username, and password are required")
    if len(password) < 8:
      raise ValueError("password must be at least 8 characters")
    try:
      user = ACCOUNT_STORE.activate(
        invite_token,
        username,
        hash_password(password),
        {
          "baseUrl": SETTINGS.default_codex_base_url,
          "apiKey": SETTINGS.default_codex_api_key,
        },
      )
    except ValueError as exc:
      add_audit("anonymous", "registration failed", str(exc))
      raise
    add_audit(username, "account activated", str(user.get("id") or ""))
    self.start_session(user, "registration login success")

  def start_session(self, user: dict, audit_event: str) -> None:
    username = str(user["username"])
    if not user["enabled"]:
      add_audit(username, "login blocked", "account disabled")
      self.write_json({"error": "account_disabled"}, HTTPStatus.FORBIDDEN)
      return
    if active_sessions_for(username) >= int(user.get("maxSessions", 1)):
      add_audit(username, "login blocked", "concurrent session limit")
      self.write_json({"error": "session_limit"}, HTTPStatus.FORBIDDEN)
      return

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"username": username, "createdAt": time.time(), "expiresAt": time.time() + SESSION_TTL_SECONDS}
    add_audit(username, audit_event)
    self.write_json(
      {"user": public_user(user), "sessionToken": token},
      headers={"Set-Cookie": f"{SESSION_COOKIE}={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}"},
    )

  def logout(self) -> None:
    token = self.session_token()
    session = SESSIONS.pop(token, None) if token else None
    add_audit(session["username"] if session else "anonymous", "logout")
    self.write_json({"ok": True}, headers={"Set-Cookie": f"{SESSION_COOKIE}=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0"})

  def accounts(self) -> None:
    self.require_admin()
    active = [admin_account(user) for user in USERS.values()]
    pending = [admin_account(account) for account in ACCOUNT_STORE.pending_accounts.values()]
    self.write_json({"accounts": active + pending, "groups": list(ACCOUNT_STORE.groups.values())})

  def create_account_batch(self) -> None:
    actor = self.require_admin()
    payload = self.read_json()
    group, accounts = ACCOUNT_STORE.create_batch(
      group_id=str(payload.get("groupId") or ""),
      new_group_name=str(payload.get("newGroupName") or ""),
      count=int(payload.get("count") or 0),
      budget_tokens=int(payload.get("budgetTokens") or 0),
      max_sessions=int(payload["maxSessions"]) if "maxSessions" in payload else 1,
    )
    if payload.get("newGroupName"):
      add_audit(actor["username"], "group created", str(group["id"]))
    add_audit(actor["username"], "account invitations created", f"{group['id']} count={len(accounts)}")
    self.write_json({"group": group, "accounts": [admin_account(account) for account in accounts]}, HTTPStatus.CREATED)

  def revoke_invite(self, user_id: str) -> None:
    actor = self.require_admin()
    account = ACCOUNT_STORE.revoke_invite(unquote(user_id))
    if not account:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
      return
    add_audit(actor["username"], "account invitation revoked", str(account["id"]))
    self.write_json({"account": admin_account(account)})

  def create_account(self) -> None:
    actor = self.require_admin()
    payload = self.read_json()
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()
    if not username or not password:
      raise ValueError("username and password are required")
    if ACCOUNT_STORE.username_exists(username):
      self.write_json({"error": "account_exists"}, HTTPStatus.CONFLICT)
      return
    group_name = str(payload.get("group") or "").strip()
    group = ACCOUNT_STORE.group_by_name(group_name) if group_name else None
    if group_name and not group:
      group = ACCOUNT_STORE.create_group(group_name)
    USERS[username] = {
      "id": f"usr_{secrets.token_urlsafe(12)}",
      "username": username,
      "displayName": str(payload.get("displayName") or username),
      "role": str(payload.get("role") or "user"),
      "groupId": str(group.get("id") if group else ""),
      "group": group_name,
      "budgetTokens": int(payload.get("budgetTokens") or 0),
      "usedTokens": 0,
      "enabled": bool(payload.get("enabled", True)),
      "maxSessions": int(payload.get("maxSessions") or 1),
      "status": "active",
      "codex": self.codex_payload_from_request(payload, {}),
      "passwordHash": hash_password(password),
    }
    ACCOUNT_STORE.save()
    add_audit(actor["username"], "account created", username)
    self.write_json({"account": public_user(USERS[username])}, HTTPStatus.CREATED)

  def update_account(self, raw_username: str) -> None:
    actor = self.require_admin()
    username = unquote(raw_username)
    user = user_by_identifier(username)
    if not user:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
      return
    payload = self.read_json()
    for key in ["displayName", "role", "group", "enabled", "budgetTokens", "maxSessions"]:
      if key in payload:
        user[key] = payload[key]
    if "codexBaseUrl" in payload or "codexApiKey" in payload or "clearCodexApiKey" in payload:
      user["codex"] = self.codex_payload_from_request(payload, user.get("codex") or {})
    if "password" in payload and payload["password"]:
      user["passwordHash"] = hash_password(str(payload["password"]))
    ACCOUNT_STORE.save()
    add_audit(actor["username"], "account updated", username)
    self.write_json({"account": public_user(user)})

  def audit_logs(self) -> None:
    self.require_admin()
    self.write_json({"logs": AUDIT_LOGS})

  def codex_settings(self) -> None:
    user = self.require_user()
    codex = user.get("codex") or {}
    self.write_json({"settings": {"baseUrl": codex.get("baseUrl", ""), "apiKeyConfigured": bool(codex.get("apiKey"))}})

  def update_codex_settings(self) -> None:
    user = self.require_user()
    payload = self.read_json()
    user["codex"] = self.codex_payload_from_request(payload, user.get("codex") or {})
    ACCOUNT_STORE.save()
    add_audit(user["username"], "codex settings updated", "api key configured" if user["codex"].get("apiKey") else "api key cleared")
    self.write_json({"settings": {"baseUrl": user["codex"].get("baseUrl", ""), "apiKeyConfigured": bool(user["codex"].get("apiKey"))}})

  def codex_payload_from_request(self, payload: dict, current: dict) -> dict:
    base_url = str(payload.get("codexBaseUrl") or payload.get("baseUrl") or current.get("baseUrl") or SETTINGS.default_codex_base_url).strip()
    api_key = str(current.get("apiKey") or "")
    if payload.get("clearCodexApiKey"):
      api_key = ""
    if "codexApiKey" in payload or "apiKey" in payload:
      api_key = str(payload.get("codexApiKey") or payload.get("apiKey") or "").strip()
    if not base_url.startswith(("http://", "https://")):
      raise ValueError("codex base URL must start with http:// or https://")
    return {"baseUrl": base_url.rstrip("/"), "apiKey": api_key}

  def list_workspaces(self) -> None:
    user = self.require_user()
    self.write_json({"workspaces": WORKSPACE_STORE.list_workspaces(user)})

  def create_workspace(self) -> None:
    user = self.require_user()
    length = int(self.headers.get("Content-Length") or 0)
    fields, files = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(length))
    workspace = WORKSPACE_STORE.create_workspace(
      user,
      fields.get("name", "Untitled workspace"),
      fields.get("shared", "").lower() in {"1", "true", "yes", "on"},
      files,
    )
    add_audit(user["username"], "workspace created", workspace["id"])
    self.write_json({"workspace": workspace}, HTTPStatus.CREATED)

  def workspace_api(self, method: str, path: str, query: str) -> None:
    user = self.require_user()
    parts = [unquote(part) for part in path.split("/") if part]
    if len(parts) < 3:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
      return
    workspace_id = parts[2]
    action = parts[3] if len(parts) > 3 else ""
    subaction = parts[4] if len(parts) > 4 else ""
    tail = parts[5] if len(parts) > 5 else ""
    if action == "chat":
      self.chat_api(method, workspace_id, subaction, tail, query)
      return
    if method == "GET" and not action:
      workspace = WORKSPACE_STORE.get_workspace(workspace_id, user)
      self.write_json({"workspace": WORKSPACE_STORE.public_workspace(workspace)})
    elif method == "PATCH" and not action:
      workspace = WORKSPACE_STORE.update_workspace(workspace_id, user, self.read_json())
      add_audit(user["username"], "workspace updated", workspace_id)
      self.write_json({"workspace": workspace})
    elif method == "DELETE" and not action:
      deleted = WORKSPACE_STORE.delete_workspace(workspace_id, user)
      add_audit(user["username"], "workspace deleted", workspace_id)
      self.write_json({"deleted": deleted})
    elif method == "POST" and action == "fork":
      payload = self.read_json()
      workspace = WORKSPACE_STORE.fork_workspace(workspace_id, user, str(payload.get("name") or "") or None)
      add_audit(user["username"], "workspace forked", f"{workspace_id} -> {workspace['id']}")
      self.write_json({"workspace": workspace}, HTTPStatus.CREATED)
    elif method == "GET" and action == "files" and not subaction:
      WORKSPACE_STORE.get_workspace(workspace_id, user)
      self.write_json({"files": WORKSPACE_STORE.file_tree(workspace_id)})
    elif method == "POST" and action == "files" and not subaction:
      length = int(self.headers.get("Content-Length") or 0)
      fields, files = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(length))
      workspace = WORKSPACE_STORE.add_files_to_workspace(workspace_id, user, files)
      add_audit(user["username"], "workspace files uploaded", f"{workspace_id} {len(files)} files")
      self.write_json({"workspace": workspace, "paths": [item.path for item in files]})
    elif method == "DELETE" and action == "files" and not subaction:
      params = parse_query(query)
      path_to_delete = (params.get("path") or [""])[0]
      workspace = WORKSPACE_STORE.delete_workspace_path(workspace_id, user, path_to_delete)
      add_audit(user["username"], "workspace path deleted", f"{workspace_id} {path_to_delete}")
      self.write_json({"workspace": workspace, "deletedPath": path_to_delete})
    elif method == "GET" and action == "files" and subaction == "preview":
      WORKSPACE_STORE.get_workspace(workspace_id, user)
      params = parse_query(query)
      self.write_json({"preview": WORKSPACE_STORE.preview_metadata(workspace_id, (params.get("path") or [""])[0])})
    elif method == "GET" and action == "files" and subaction == "raw":
      WORKSPACE_STORE.get_workspace(workspace_id, user)
      params = parse_query(query)
      path = (params.get("path") or [""])[0]
      file_path = WORKSPACE_STORE.workspace_file_path(workspace_id, path)
      metadata = WORKSPACE_STORE.file_metadata(workspace_id, path)
      disposition = "attachment" if (params.get("download") or [""])[0] in {"1", "true", "yes"} else "inline"
      self.write_file(file_path, metadata["contentType"], metadata["name"], disposition)
    elif method == "GET" and action == "files" and subaction == "rendered":
      WORKSPACE_STORE.get_workspace(workspace_id, user)
      params = parse_query(query)
      path = (params.get("path") or [""])[0]
      file_path = WORKSPACE_STORE.rendered_preview_path(workspace_id, path)
      self.write_file(file_path, "application/pdf", f"{Path(path).stem or 'preview'}.pdf", "inline")
    elif method == "GET" and action == "artifacts":
      WORKSPACE_STORE.get_workspace(workspace_id, user)
      self.write_json({"artifacts": WORKSPACE_STORE.workspace_artifacts(workspace_id)})
    elif method == "POST" and action == "refresh-artifacts":
      artifacts = WORKSPACE_STORE.refresh_artifacts(workspace_id, user)
      add_audit(user["username"], "workspace artifacts refreshed", workspace_id)
      self.write_json({"artifacts": artifacts})
    elif method == "GET" and action == "download":
      params = parse_query(query)
      filename, body = WORKSPACE_STORE.build_download_zip(
        workspace_id,
        user,
        (params.get("mode") or ["changes"])[0],
        parse_urlencoded_paths(params.get("paths", [])),
      )
      add_audit(user["username"], "workspace downloaded", f"{workspace_id} {filename}")
      ascii_filename = ascii_download_filename(filename, "workspace.zip")
      self.write_binary(
        body,
        "application/zip",
        headers={
          "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}",
          "Content-Transfer-Encoding": "binary",
          "Connection": "close",
        },
      )
    else:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

  def chat_api(self, method: str, workspace_id: str, action: str, tail: str, query: str) -> None:
    user = self.require_user()
    if method == "GET" and action == "sessions" and not tail:
      self.write_json({"sessions": CHAT_RUNTIME.list_sessions(workspace_id, user)})
    elif method == "POST" and action == "sessions" and not tail:
      payload = self.read_json()
      session = CHAT_RUNTIME.create_session(workspace_id, user, str(payload.get("title") or "") or None)
      add_audit(user["username"], "chat session created", f"{workspace_id} {session['id']}")
      self.write_json({"session": session}, HTTPStatus.CREATED)
    elif method == "POST" and action == "runs" and not tail:
      result = CHAT_RUNTIME.start_run(workspace_id, user, self.read_json())
      add_audit(user["username"], "chat run queued", f"{workspace_id} {result['run']['id']}")
      self.write_json(result, HTTPStatus.CREATED)
    elif method == "POST" and action == "runs" and tail:
      parts = [unquote(part) for part in urlparse(self.path).path.split("/") if part]
      if len(parts) == 7 and parts[6] == "stop":
        result = CHAT_RUNTIME.stop_run(workspace_id, parts[5], user)
        add_audit(user["username"], "chat run stop requested", f"{workspace_id} {parts[5]}")
        self.write_json(result)
      else:
        self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
    elif method == "GET" and action == "events" and not tail:
      params = parse_query(query)
      session_id = (params.get("sessionId") or [""])[0]
      after = int((params.get("after") or ["0"])[0] or 0)
      self.write_json({"events": CHAT_RUNTIME.events(workspace_id, session_id, after, user)})
    elif method == "GET" and action == "stream" and not tail:
      params = parse_query(query)
      session_id = (params.get("sessionId") or [""])[0]
      after = int((params.get("after") or ["0"])[0] or 0)
      self.write_sse(workspace_id, session_id, after, user)
    else:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

  def write_sse(self, workspace_id: str, session_id: str, after: int, user: dict) -> None:
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Connection", "keep-alive")
    origin = self.headers.get("Origin")
    if origin:
      self.send_header("Access-Control-Allow-Origin", origin)
      self.send_header("Access-Control-Allow-Credentials", "true")
    self.end_headers()
    last_id = after
    idle_rounds = 0
    while idle_rounds < 24:
      events = CHAT_RUNTIME.wait_events(workspace_id, session_id, last_id, user, timeout=5)
      if not events:
        idle_rounds += 1
        try:
          self.wfile.write(b": keepalive\n\n")
          self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
          return
        continue
      idle_rounds = 0
      for event in events:
        last_id = int(event["id"])
        payload = json.dumps(event, ensure_ascii=False)
        try:
          self.wfile.write(f"id: {last_id}\nevent: {event['type']}\ndata: {payload}\n\n".encode("utf-8"))
          self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
          return
      if events[-1]["type"] in {"completed", "stopped", "failed"}:
        return

  def require_session_response(self) -> None:
    user = self.current_user()
    if not user:
      self.write_json({"user": None}, HTTPStatus.UNAUTHORIZED)
      return
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
    session = SESSIONS.get(token)
    if not session or session["expiresAt"] <= time.time():
      SESSIONS.pop(token, None)
      return None
    session["expiresAt"] = time.time() + SESSION_TTL_SECONDS
    return USERS.get(session["username"])

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


def main() -> None:
  parser = argparse.ArgumentParser(description="AI Audit prototype backend")
  parser.add_argument("--host", default=SETTINGS.host)
  parser.add_argument("--port", type=int, default=SETTINGS.port)
  args = parser.parse_args()
  httpd = ThreadingHTTPServer((args.host, args.port), Handler)
  print(f"AI Audit backend serving http://{args.host}:{args.port}", flush=True)
  httpd.serve_forever()


if __name__ == "__main__":
  main()
