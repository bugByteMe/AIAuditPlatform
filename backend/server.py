#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import io
import json
import mimetypes
import secrets
import threading
import time
import traceback
from contextlib import asynccontextmanager, contextmanager
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from account_store import AccountStore
from chat_runtime import ChatRuntime
from config import SETTINGS
from micu_api import MicuApiClient, MicuApiError, parse_cny
from recharge_import import MAX_WORKBOOK_BYTES, parse_recharge_workbook, payment_key
from upload_store import UploadManager
from workspace_store import StorageError, WorkspaceStore, parse_multipart, parse_query, parse_urlencoded_paths


FRONTEND_DIR = SETTINGS.frontend_dir
WORKSPACE_STORAGE_DIR = SETTINGS.workspace_storage_dir
SESSION_COOKIE = SETTINGS.session_cookie
SESSION_TTL_SECONDS = SETTINGS.session_ttl_seconds
PBKDF2_ITERATIONS = SETTINGS.pbkdf2_iterations
WORKSPACE_STORE = WorkspaceStore(WORKSPACE_STORAGE_DIR)


class KeyedLockPool:
  def __init__(self):
    self.guard = threading.Lock()
    self.entries: dict[str, dict] = {}

  @contextmanager
  def hold(self, *raw_keys: str):
    keys = sorted(set(raw_keys))
    with self.guard:
      entries = []
      for key in keys:
        entry = self.entries.setdefault(key, {"lock": threading.RLock(), "references": 0})
        entry["references"] += 1
        entries.append((key, entry))
    try:
      for _, entry in entries:
        entry["lock"].acquire()
      yield
    finally:
      for _, entry in reversed(entries):
        entry["lock"].release()
      with self.guard:
        for key, entry in entries:
          entry["references"] -= 1
          if entry["references"] == 0:
            self.entries.pop(key, None)


REGISTRATION_LOCKS = KeyedLockPool()


def invite_digest(token: str) -> str:
  return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
MICU_CLIENT = MicuApiClient(
  SETTINGS.micu_management_url,
  SETTINGS.micu_inference_url,
  SETTINGS.micu_management_token,
  SETTINGS.micu_user_id,
  SETTINGS.micu_token_group,
  SETTINGS.micu_quota_per_cny,
  SETTINGS.micu_request_timeout_seconds,
)
WORKSPACE_STORE.set_account_provider(lambda: (ACCOUNT_STORE.users, ACCOUNT_STORE.groups))


def check_micu_budget(user: dict) -> None:
  binding = user.get("micu") or {}
  if not binding.get("tokenId"):
    raise StorageError("budget_provider_unavailable", "MicuAPI account is not provisioned")
  try:
    balance = MICU_CLIENT.balance(binding)
  except MicuApiError as exc:
    raise StorageError("budget_provider_unavailable", str(exc)) from exc
  establishes_baseline = "rechargeBaselineQuota" not in binding
  binding.update(
    {
      "lastBalanceCny": balance["remainingCny"],
      "lastRemainingPercent": balance["remainingPercent"],
      "rechargeBaselineQuota": balance["referenceQuota"],
      "lastSyncedAt": int(time.time()),
      "status": balance["status"],
      "lastError": "",
    }
  )
  if establishes_baseline:
    ACCOUNT_STORE.save()
  if balance["status"] == "exhausted":
    raise StorageError("budget_exhausted", "MicuAPI balance is exhausted")
  if balance["status"] != "ready":
    raise StorageError("budget_provider_unavailable", "MicuAPI token is not enabled")


CHAT_RUNTIME = ChatRuntime(
  WORKSPACE_STORE,
  USERS,
  capacity=SETTINGS.local_run_capacity,
  save_users=ACCOUNT_STORE.save,
  groups=ACCOUNT_STORE.groups,
  budget_checker=check_micu_budget,
)

SESSIONS: dict[str, dict] = {}
SESSION_LOCK = threading.RLock()
AUDIT_LOGS = [
  {"time": "2026-09-12 23:12", "actor": "chen.audit", "event": "login success", "detail": "seed audit log"},
]


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


def public_user(user: dict) -> dict:
  public = {
    key: value
    for key, value in user.items()
    if key not in {
      "passwordHash", "codex", "customCodex", "micu", "inviteToken", "initialBudgetCny", "activationInviteDigest"
    }
  }
  mode = str(user.get("providerMode") or "legacy")
  custom = user.get("customCodex") or user.get("codex") or {}
  public["codex"] = {
    "mode": mode,
    "baseUrl": SETTINGS.micu_inference_url if mode == "micu" else custom.get("baseUrl", ""),
    "apiKeyConfigured": bool((user.get("micu") or {}).get("apiKey")) if mode == "micu" else bool(custom.get("apiKey")),
  }
  public["budget"] = budget_summary(user)
  return public


def admin_account(account: dict) -> dict:
  public = {
    key: value
    for key, value in account.items()
    if key not in {"passwordHash", "codex", "customCodex", "micu", "activationInviteDigest"}
  }
  public["budget"] = budget_summary(account, include_amount=True)
  if public.get("status") == "active" and not public.get("enabled", True):
    public["status"] = "disabled"
  return public


def budget_summary(user: dict, *, include_amount: bool = False) -> dict:
  mode = str(user.get("providerMode") or "micu")
  if mode == "custom":
    return {"source": "custom", "remainingPercent": None, "status": "not_applicable"}
  binding = user.get("micu") or {}
  summary = {
    "source": "micu",
    "remainingPercent": binding.get("lastRemainingPercent"),
    "status": str(binding.get("status") or ("provisioning" if user.get("status") == "pending" else "unavailable")),
  }
  if include_amount:
    summary.update({"currency": "CNY", "remaining": binding.get("lastBalanceCny")})
  return summary


def refresh_micu_balance(user: dict) -> None:
  if str(user.get("providerMode") or "micu") != "micu" or not (user.get("micu") or {}).get("tokenId"):
    return
  binding = user["micu"]
  try:
    establishes_baseline = "rechargeBaselineQuota" not in binding
    balance = MICU_CLIENT.balance(binding)
    binding.update({"lastBalanceCny": balance["remainingCny"], "lastRemainingPercent": balance["remainingPercent"], "rechargeBaselineQuota": balance["referenceQuota"], "lastSyncedAt": int(time.time()), "status": balance["status"], "lastError": ""})
    if establishes_baseline:
      ACCOUNT_STORE.save()
  except MicuApiError as exc:
    binding.update({"status": "unavailable", "lastError": str(exc)})


def refresh_micu_balances(users) -> None:
  managed = [
    user
    for user in users
    if str(user.get("providerMode") or "micu") == "micu" and (user.get("micu") or {}).get("tokenId")
  ]
  if not managed:
    return
  try:
    tokens_by_id = {int(token.get("id") or 0): token for token in MICU_CLIENT.all_tokens() if token.get("id")}
  except MicuApiError as exc:
    for user in managed:
      user["micu"].update({"status": "unavailable", "lastError": str(exc)})
    return
  synced_at = int(time.time())
  establishes_baseline = False
  for user in managed:
    binding = user["micu"]
    token = tokens_by_id.get(int(binding.get("tokenId") or 0))
    if not token:
      binding.update({"status": "unavailable", "lastError": "MicuAPI token was not found"})
      continue
    balance = MICU_CLIENT.balance_from_token(token, binding.get("rechargeBaselineQuota"))
    establishes_baseline = establishes_baseline or "rechargeBaselineQuota" not in binding
    binding.update({"lastBalanceCny": balance["remainingCny"], "lastRemainingPercent": balance["remainingPercent"], "rechargeBaselineQuota": balance["referenceQuota"], "lastSyncedAt": synced_at, "status": balance["status"], "lastError": ""})
  if establishes_baseline:
    ACCOUNT_STORE.save()


def provision_micu(username: str, initial_balance_cny) -> dict:
  if not MICU_CLIENT.configured:
    raise ValueError("MicuAPI management credentials are not configured")
  try:
    return MICU_CLIENT.ensure_binding(username, initial_balance_cny)
  except MicuApiError as exc:
    raise ValueError(str(exc)) from exc


def reconcile_micu_accounts() -> None:
  if not MICU_CLIENT.configured:
    return
  changed = False
  for user in list(USERS.values()):
    binding = user.get("micu") or {}
    username = str(user.get("username") or "").strip()
    try:
      if not username:
        raise MicuApiError("invalid_username", "local account does not have a username")
      if binding.get("tokenId"):
        previous_name = str(binding.get("tokenName") or "")
        MICU_CLIENT.align_binding_name(binding, username)
        changed = changed or previous_name != username
      else:
        user["micu"] = MICU_CLIENT.ensure_binding(username, SETTINGS.micu_migration_balance_cny)
        changed = True
    except MicuApiError as exc:
      if binding.get("tokenId"):
        binding.update({"status": "unavailable", "lastError": str(exc)})
      else:
        user["micu"] = {"tokenName": username, "status": "error", "lastError": str(exc)}
      changed = True
  if changed:
    ACCOUNT_STORE.save()


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
  del AUDIT_LOGS[SETTINGS.audit_log_limit:]


UPLOAD_MANAGER = UploadManager(WORKSPACE_STORE, CHAT_RUNTIME.worker_registry, SETTINGS, add_audit)


def _prune_expired_sessions_unlocked(now: float) -> None:
  for token, session in list(SESSIONS.items()):
    if float(session.get("expiresAt") or 0) <= now:
      SESSIONS.pop(token, None)


def _active_sessions_for_unlocked(username: str) -> int:
  return sum(1 for session in SESSIONS.values() if session.get("username") == username)


def active_sessions_for(username: str) -> int:
  with SESSION_LOCK:
    _prune_expired_sessions_unlocked(time.time())
    return _active_sessions_for_unlocked(username)


def create_user_session(user: dict, audit_event: str, registration_digest: str = "") -> tuple[str | None, str | None]:
  username = str(user["username"])
  if not user.get("enabled", True):
    add_audit(username, "login blocked", "account disabled")
    return None, "account_disabled"

  now = time.time()
  with SESSION_LOCK:
    _prune_expired_sessions_unlocked(now)
    if registration_digest:
      for token, session in SESSIONS.items():
        if session.get("username") == username and hmac.compare_digest(
          str(session.get("registrationDigest") or ""), registration_digest
        ):
          session["expiresAt"] = now + SESSION_TTL_SECONDS
          add_audit(username, audit_event, "registration session reused")
          return token, None
    if _active_sessions_for_unlocked(username) >= int(user.get("maxSessions", 1)):
      add_audit(username, "login blocked", "concurrent session limit")
      return None, "session_limit"
    token = secrets.token_urlsafe(32)
    session = {"username": username, "createdAt": now, "expiresAt": now + SESSION_TTL_SECONDS}
    if registration_digest:
      session["registrationDigest"] = registration_digest
    SESSIONS[token] = session

  add_audit(username, audit_event)
  return token, None


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
    if len(password) < SETTINGS.registration_min_password_length:
      raise ValueError(f"password must be at least {SETTINGS.registration_min_password_length} characters")
    digest = invite_digest(invite_token)
    normalized_username = username.casefold()
    with REGISTRATION_LOCKS.hold(f"invite:{digest}", f"username:{normalized_username}"):
      with ACCOUNT_STORE.lock:
        pending = ACCOUNT_STORE.pending_by_token(invite_token)
        activated = next(
          (
            candidate
            for candidate in USERS.values()
            if hmac.compare_digest(str(candidate.get("activationInviteDigest") or ""), digest)
          ),
          None,
        )

      if not pending or pending.get("status") != "pending":
        if (
          not activated
          or str(activated.get("username") or "").casefold() != normalized_username
          or not verify_password(password, str(activated.get("passwordHash") or ""))
        ):
          raise ValueError("invalid invite token")
        user = activated
        audit_event = "registration retry login success"
      else:
        if ACCOUNT_STORE.username_exists(username):
          raise ValueError("username already exists")
        binding = provision_micu(username, pending.get("initialBudgetCny") or "0.00")
        try:
          user = ACCOUNT_STORE.activate(
            invite_token,
            username,
            hash_password(password),
            {},
            binding,
            "micu",
            digest,
          )
        except Exception as exc:
          add_audit("anonymous", "registration failed", str(exc))
          raise
        add_audit(username, "account activated", str(user.get("id") or ""))
        audit_event = "registration login success"

      token, session_error = create_user_session(user, audit_event, digest)
      response = {"user": public_user(user)}
      headers = None
      if token:
        response["sessionToken"] = token
        headers = {
          "Set-Cookie": f"{SESSION_COOKIE}={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}"
        }
      else:
        response["sessionError"] = session_error
      self.write_json(response, headers=headers)

  def start_session(self, user: dict, audit_event: str) -> None:
    token, session_error = create_user_session(user, audit_event)
    if not token:
      self.write_json({"error": session_error}, HTTPStatus.FORBIDDEN)
      return
    self.write_json(
      {"user": public_user(user), "sessionToken": token},
      headers={"Set-Cookie": f"{SESSION_COOKIE}={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}"},
    )

  def logout(self) -> None:
    token = self.session_token()
    with SESSION_LOCK:
      session = SESSIONS.pop(token, None) if token else None
    add_audit(session["username"] if session else "anonymous", "logout")
    self.write_json({"ok": True}, headers={"Set-Cookie": f"{SESSION_COOKIE}=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0"})

  def accounts(self) -> None:
    self.require_admin()
    refresh_micu_balances(USERS.values())
    user_usage, group_usage = WORKSPACE_STORE.usage_summaries()
    active = [
      {**admin_account(user), **user_usage.get(str(user.get("id") or ""), {"diskUsageBytes": 0, "workspaceCount": 0})}
      for user in USERS.values()
    ]
    pending = [
      {**admin_account(account), "diskUsageBytes": 0, "workspaceCount": 0}
      for account in ACCOUNT_STORE.pending_accounts.values()
    ]
    all_accounts = [*USERS.values(), *ACCOUNT_STORE.pending_accounts.values()]
    groups = []
    for group_id, group in ACCOUNT_STORE.groups.items():
      summary = group_usage.get(group_id, {"diskUsageBytes": 0, "workspaceCount": 0, "userCount": 0})
      groups.append(
        {
          **group,
          **summary,
          "userCount": sum(1 for account in all_accounts if str(account.get("groupId") or "") == group_id),
        }
      )
    self.write_json({"accounts": active + pending, "groups": groups})

  def create_account_batch(self) -> None:
    actor = self.require_admin()
    payload = self.read_json()
    group, accounts = ACCOUNT_STORE.create_batch(
      group_id=str(payload.get("groupId") or ""),
      new_group_name=str(payload.get("newGroupName") or ""),
      count=int(payload.get("count") or 0),
      budget_cny=payload.get("budgetCny", payload.get("budgetTokens", "0")),
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

  def reset_account_budget(self, raw_identifier: str) -> None:
    actor = self.require_admin()
    identifier = unquote(raw_identifier)
    payload = self.read_json()
    if "amountCny" not in payload and "budgetCny" not in payload:
      raise ValueError("amountCny is required")
    account = ACCOUNT_STORE.user_by_identifier(identifier)
    if not account:
      raise ValueError("user not found")
    binding = account.get("micu") or {}
    if not binding.get("tokenId"):
      raise ValueError("user does not have a provisioned MicuAPI token")
    amount = parse_cny(payload.get("amountCny", payload.get("budgetCny")))
    try:
      balance = MICU_CLIENT.add_balance(binding, amount)
    except MicuApiError as exc:
      raise StorageError("budget_provider_unavailable", str(exc)) from exc
    binding.update({"lastBalanceCny": balance["remainingCny"], "lastRemainingPercent": balance["remainingPercent"], "rechargeBaselineQuota": balance["rawQuota"], "lastSyncedAt": int(time.time()), "status": balance["status"], "lastError": ""})
    ACCOUNT_STORE.save()
    add_audit(actor["username"], "account MicuAPI balance added", f"{account['id']} amountCny={balance['addedCny']} balanceCny={balance['remainingCny']}")
    self.write_json({"account": admin_account(account)})

  def read_recharge_upload(self) -> tuple[bytes, dict[str, str]]:
    content_length = int(self.headers.get("Content-Length") or 0)
    if content_length <= 0:
      raise ValueError("XLSX file is required")
    if content_length > MAX_WORKBOOK_BYTES + 1024 * 1024:
      raise ValueError("XLSX upload exceeds the size limit")
    fields, files = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(content_length))
    if len(files) != 1:
      raise ValueError("upload exactly one XLSX file")
    uploaded = files[0]
    if Path(uploaded.path).suffix.lower() != ".xlsx":
      raise ValueError("only .xlsx files are supported")
    return uploaded.content, fields

  def classify_recharge_import(self, parsed: dict) -> dict:
    seen: set[str] = set()
    classified = []
    for source in parsed["records"]:
      record = dict(source)
      payment_number = str(record.pop("paymentNumber", "") or "")
      if record["status"] != "candidate":
        classified.append(record)
        continue
      key = payment_key(payment_number)
      if key in seen or ACCOUNT_STORE.recharge_payment(key):
        record.update({"status": "duplicate", "reason": "支付单号 has already been used", "paymentKey": key})
      else:
        seen.add(key)
        account = user_by_username(record["username"])
        if not account:
          record.update({"status": "unmatched", "reason": "用户名 does not match an active account", "paymentKey": key})
        elif not (account.get("micu") or {}).get("tokenId"):
          record.update({"status": "invalid", "reason": "account does not have a provisioned MicuAPI token", "paymentKey": key})
        else:
          record.update({"status": "eligible", "reason": "", "paymentKey": key, "userId": account["id"]})
      record["paymentNumber"] = payment_number
      classified.append(record)
    summary: dict[str, int] = {}
    for record in classified:
      summary[record["status"]] = summary.get(record["status"], 0) + 1
    return {**parsed, "records": classified, "summary": summary}

  @staticmethod
  def public_recharge_import(result: dict) -> dict:
    records = []
    for source in result["records"]:
      record = {key: value for key, value in source.items() if key not in {"paymentNumber", "paymentKey", "userId"}}
      records.append(record)
    return {**{key: value for key, value in result.items() if key != "records"}, "records": records}

  def preview_recharge_import(self) -> None:
    self.require_admin()
    content, _ = self.read_recharge_upload()
    result = self.classify_recharge_import(parse_recharge_workbook(content))
    self.write_json(self.public_recharge_import(result))

  def apply_recharge_import(self) -> None:
    actor = self.require_admin()
    content, fields = self.read_recharge_upload()
    parsed = parse_recharge_workbook(content)
    if not fields.get("previewDigest") or not secrets.compare_digest(fields["previewDigest"], parsed["digest"]):
      raise ValueError("uploaded workbook does not match the preview")
    result = self.classify_recharge_import(parsed)
    batch_id = f"rchb_{secrets.token_urlsafe(9)}"
    imported_at = time.strftime("%Y-%m-%d %H:%M:%S")
    for record in result["records"]:
      if record["status"] != "eligible":
        continue
      account = ACCOUNT_STORE.user_by_identifier(record["userId"])
      if not account:
        record.update({"status": "unmatched", "reason": "account no longer exists"})
        continue
      reservation = {
        "userId": account["id"],
        "paidAt": record["paidAt"],
        "amountCny": record["amountCny"],
        "creditCny": record["creditCny"],
        "importedAt": imported_at,
        "batchId": batch_id,
        "sheet": record["sheet"],
        "row": record["row"],
        "paymentRef": record["paymentRef"],
      }
      reserved, _ = ACCOUNT_STORE.reserve_recharge_payment(record["paymentKey"], reservation)
      if not reserved:
        record.update({"status": "duplicate", "reason": "支付单号 has already been used"})
        continue
      try:
        balance = MICU_CLIENT.add_balance(account["micu"], record["creditCny"])
        ACCOUNT_STORE.finish_recharge_payment(
          record["paymentKey"],
          status="applied",
          binding_updates={
            "lastBalanceCny": balance["remainingCny"],
            "lastRemainingPercent": balance["remainingPercent"],
            "rechargeBaselineQuota": balance["rawQuota"],
            "lastSyncedAt": int(time.time()),
            "status": balance["status"],
            "lastError": "",
          },
        )
        record.update({"status": "applied", "reason": ""})
      except Exception as exc:
        ACCOUNT_STORE.finish_recharge_payment(record["paymentKey"], status="review_required", reason=str(exc))
        record.update({"status": "review_required", "reason": str(exc)})
    summary: dict[str, int] = {}
    for record in result["records"]:
      summary[record["status"]] = summary.get(record["status"], 0) + 1
    result.update({"batchId": batch_id, "summary": summary})
    add_audit(
      actor["username"],
      "recharge workbook imported",
      f"{batch_id} applied={summary.get('applied', 0)} duplicate={summary.get('duplicate', 0)} review={summary.get('review_required', 0)} skipped={len(result['records']) - summary.get('applied', 0) - summary.get('duplicate', 0) - summary.get('review_required', 0)}",
    )
    self.write_json(self.public_recharge_import(result))

  def create_group(self) -> None:
    actor = self.require_admin()
    payload = self.read_json()
    raw_limit = payload.get("diskLimitBytes") if "diskLimitBytes" in payload else None
    parsed_limit = None if raw_limit is None else int(raw_limit)
    if parsed_limit is not None and parsed_limit < 0:
      raise ValueError("diskLimitBytes must be non-negative or null")
    raw_run_limit = payload.get("liveRunLimit") if "liveRunLimit" in payload else None
    parsed_run_limit = None if raw_run_limit is None else int(raw_run_limit)
    if parsed_run_limit is not None and parsed_run_limit < 0:
      raise ValueError("liveRunLimit must be non-negative")
    group = ACCOUNT_STORE.create_group(str(payload.get("name") or ""))
    if "diskLimitBytes" in payload:
      group = ACCOUNT_STORE.update_group_disk_limit(group["id"], parsed_limit)
    if parsed_run_limit is not None:
      group = ACCOUNT_STORE.update_group_live_run_limit(group["id"], parsed_run_limit)
    add_audit(actor["username"], "group created", str(group["id"]))
    self.write_json({"group": group}, HTTPStatus.CREATED)

  def update_group(self, raw_group_id: str) -> None:
    actor = self.require_admin()
    group_id = unquote(raw_group_id)
    payload = self.read_json()
    if not ({"diskLimitBytes", "liveRunLimit"} & payload.keys()):
      raise ValueError("diskLimitBytes or liveRunLimit is required")
    group = ACCOUNT_STORE.groups.get(group_id)
    if not group:
      raise ValueError("group not found")
    raw_disk_limit = payload.get("diskLimitBytes") if "diskLimitBytes" in payload else None
    disk_limit = None if raw_disk_limit is None else int(raw_disk_limit)
    if "diskLimitBytes" in payload and disk_limit is not None and disk_limit < 0:
      raise ValueError("diskLimitBytes must be non-negative or null")
    live_run_limit = int(payload["liveRunLimit"]) if "liveRunLimit" in payload else None
    if live_run_limit is not None and live_run_limit < 0:
      raise ValueError("liveRunLimit must be non-negative")
    if "diskLimitBytes" in payload:
      group = ACCOUNT_STORE.update_group_disk_limit(group_id, disk_limit)
      add_audit(actor["username"], "group disk limit updated", f"{group_id} limit={group['diskLimitBytes']}")
    if "liveRunLimit" in payload:
      group = ACCOUNT_STORE.update_group_live_run_limit(group_id, live_run_limit)
      add_audit(actor["username"], "group live run limit updated", f"{group_id} limit={group['liveRunLimit']}")
    self.write_json({"group": group})

  def delete_account(self, raw_identifier: str) -> None:
    actor = self.require_admin()
    identifier = unquote(raw_identifier)
    user = ACCOUNT_STORE.user_by_identifier(identifier)
    pending = ACCOUNT_STORE.pending_accounts.get(identifier)
    account = user or pending
    if not account:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
      return
    user_ids = {str(account["id"])}
    active_users = [user] if user else []
    self.validate_admin_deletion(actor, user_ids)
    deleted_resources = self.stop_and_delete_resources(actor, active_users)
    if user and (user.get("micu") or {}).get("tokenId"):
      try:
        MICU_CLIENT.delete_token(user["micu"])
      except MicuApiError as exc:
        user["enabled"] = False
        ACCOUNT_STORE.save()
        raise StorageError("budget_provider_unavailable", f"MicuAPI key cleanup failed: {exc}") from exc
    removed_users, removed_pending = ACCOUNT_STORE.remove_accounts(user_ids)
    self.invalidate_user_sessions({str(item.get("username")) for item in removed_users})
    add_audit(actor["username"], "account deleted", str(account["id"]))
    self.write_json({"deleted": {"accounts": len(removed_users) + len(removed_pending), **deleted_resources}})

  def delete_group(self, raw_group_id: str) -> None:
    actor = self.require_admin()
    group_id = unquote(raw_group_id)
    group = ACCOUNT_STORE.groups.get(group_id)
    if not group:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
      return
    active_users = [user for user in USERS.values() if str(user.get("groupId") or "") == group_id]
    pending = [account for account in ACCOUNT_STORE.pending_accounts.values() if str(account.get("groupId") or "") == group_id]
    user_ids = {str(account["id"]) for account in [*active_users, *pending]}
    self.validate_admin_deletion(actor, user_ids)
    deleted_resources = self.stop_and_delete_resources(actor, active_users)
    for user in active_users:
      if not (user.get("micu") or {}).get("tokenId"):
        continue
      try:
        MICU_CLIENT.delete_token(user["micu"])
      except MicuApiError as exc:
        for member in active_users:
          member["enabled"] = False
        ACCOUNT_STORE.save()
        raise StorageError("budget_provider_unavailable", f"MicuAPI key cleanup failed: {exc}") from exc
    removed_group, removed_users, removed_pending = ACCOUNT_STORE.remove_group_and_accounts(group_id, user_ids)
    self.invalidate_user_sessions({str(item.get("username")) for item in removed_users})
    add_audit(actor["username"], "group deleted", f"{group_id} accounts={len(removed_users) + len(removed_pending)}")
    self.write_json({"deleted": {"group": removed_group, "accounts": len(removed_users) + len(removed_pending), **deleted_resources}})

  def validate_admin_deletion(self, actor: dict, user_ids: set[str]) -> None:
    if str(actor.get("id") or "") in user_ids:
      raise StorageError("protected_admin", "the signed-in system administrator cannot be deleted")
    remaining_admins = [
      user
      for user in USERS.values()
      if user.get("role") == "system_admin" and str(user.get("id") or "") not in user_ids
    ]
    if not remaining_admins:
      raise StorageError("protected_admin", "at least one system administrator must remain")

  def stop_and_delete_resources(self, actor: dict, active_users: list[dict]) -> dict:
    usernames = {str(user.get("username") or "") for user in active_users if user.get("username")}
    if not usernames:
      return {"workspaces": [], "sessionIds": [], "runIds": []}
    remaining = CHAT_RUNTIME.stop_and_wait_for_users(usernames, actor)
    if remaining:
      raise StorageError("runs_not_stopped", f"runs did not stop: {', '.join(sorted(remaining))}")
    return CHAT_RUNTIME.delete_user_resources(usernames)

  def invalidate_user_sessions(self, usernames: set[str]) -> None:
    with SESSION_LOCK:
      for token, session in list(SESSIONS.items()):
        if session.get("username") in usernames:
          SESSIONS.pop(token, None)

  def create_account(self) -> None:
    actor = self.require_admin()
    payload = self.read_json()
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()
    if not username or not password:
      raise ValueError("username and password are required")
    with REGISTRATION_LOCKS.hold(f"username:{username.casefold()}"):
      if ACCOUNT_STORE.username_exists(username):
        self.write_json({"error": "account_exists"}, HTTPStatus.CONFLICT)
        return
      group_name = str(payload.get("group") or "").strip()
      group = ACCOUNT_STORE.group_by_name(group_name) if group_name else None
      if group_name and not group:
        group = ACCOUNT_STORE.create_group(group_name)
      user_id = f"usr_{secrets.token_urlsafe(12)}"
      binding = provision_micu(username, payload.get("budgetCny", "0.00"))
      USERS[username] = {
        "id": user_id,
        "username": username,
        "displayName": str(payload.get("displayName") or username),
        "role": str(payload.get("role") or "user"),
        "groupId": str(group.get("id") if group else ""),
        "group": group_name,
        "usedTokens": 0,
        "enabled": bool(payload.get("enabled", True)),
        "maxSessions": int(payload.get("maxSessions") or 1),
        "status": "active",
        "providerMode": "micu",
        "micu": binding,
        "customCodex": {},
        "passwordHash": hash_password(password),
      }
      try:
        ACCOUNT_STORE.save()
      except Exception:
        USERS.pop(username, None)
        raise
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
    was_enabled = bool(user.get("enabled", True))
    for key in ["displayName", "role", "group", "enabled", "maxSessions"]:
      if key in payload:
        user[key] = payload[key]
    if "codexBaseUrl" in payload or "codexApiKey" in payload or "clearCodexApiKey" in payload:
      user["customCodex"] = self.codex_payload_from_request(payload, user.get("customCodex") or {})
      user["providerMode"] = "custom"
    if "password" in payload and payload["password"]:
      user["passwordHash"] = hash_password(str(payload["password"]))
    if "enabled" in payload and bool(user.get("enabled")) != was_enabled and (user.get("micu") or {}).get("tokenId"):
      try:
        MICU_CLIENT.set_enabled(user["micu"], bool(user.get("enabled")))
      except MicuApiError as exc:
        user["enabled"] = was_enabled
        raise StorageError("budget_provider_unavailable", str(exc)) from exc
    ACCOUNT_STORE.save()
    add_audit(actor["username"], "account updated", username)
    self.write_json({"account": public_user(user)})

  def audit_logs(self) -> None:
    self.require_admin()
    self.write_json({"logs": AUDIT_LOGS})

  def workers(self) -> None:
    self.require_admin()
    self.write_json({"workers": CHAT_RUNTIME.worker_status()})

  def codex_settings(self) -> None:
    user = self.require_user()
    refresh_micu_balance(user)
    mode = str(user.get("providerMode") or "micu")
    custom = user.get("customCodex") or {}
    self.write_json(
      {
        "settings": {
          "mode": mode,
          "baseUrl": custom.get("baseUrl", ""),
          "apiKeyConfigured": bool(custom.get("apiKey")),
          "micu": {
            "baseUrl": SETTINGS.micu_inference_url,
            "group": SETTINGS.micu_token_group,
            "apiKeyConfigured": bool((user.get("micu") or {}).get("apiKey")),
            "budget": budget_summary({**user, "providerMode": "micu"}),
          },
        }
      }
    )

  def recharge_info(self) -> None:
    user = self.require_user()
    binding = user.get("micu") or {}
    api_key = str(binding.get("apiKey") or "").strip()
    if not api_key:
      raise StorageError("codex_auth_required", "MicuAPI account is not provisioned")
    add_audit(user["username"], "MicuAPI credentials viewed", str(user.get("id") or ""))
    self.write_json(
      {
        "username": user["username"],
        "baseUrl": SETTINGS.micu_inference_url,
        "apiKey": api_key,
        "products": [
          {"amountCny": "50.00", "qrCodeUrl": "/assets/payment-qr/50.png"},
          {"amountCny": "100.00", "qrCodeUrl": "/assets/payment-qr/100.png"},
          {"amountCny": "200.00", "qrCodeUrl": "/assets/payment-qr/200.png"},
        ],
        "adjustments": ACCOUNT_STORE.recharge_history(str(user.get("id") or "")),
      }
    )

  def update_codex_settings(self) -> None:
    user = self.require_user()
    payload = self.read_json()
    mode = str(payload.get("mode") or "custom")
    if mode not in {"micu", "custom"}:
      raise ValueError("mode must be micu or custom")
    if mode == "micu":
      if not (user.get("micu") or {}).get("apiKey"):
        raise ValueError("MicuAPI account is not provisioned")
    else:
      current_custom = user.get("customCodex") or {}
      custom_base_url = str(payload.get("baseUrl") or current_custom.get("baseUrl") or "").strip()
      if not custom_base_url:
        raise ValueError("custom API base URL is required")
      user["customCodex"] = self.codex_payload_from_request({**payload, "baseUrl": custom_base_url}, current_custom)
      if not user["customCodex"].get("apiKey"):
        raise ValueError("custom API key is required")
    user["providerMode"] = mode
    ACCOUNT_STORE.save()
    add_audit(user["username"], "codex settings updated", f"provider={mode}")
    custom = user.get("customCodex") or {}
    self.write_json(
      {
        "settings": {
          "mode": mode,
          "baseUrl": custom.get("baseUrl", ""),
          "apiKeyConfigured": bool(custom.get("apiKey")),
          "budget": budget_summary(user),
        }
      }
    )

  def codex_payload_from_request(self, payload: dict, current: dict) -> dict:
    base_url = str(payload.get("codexBaseUrl") or payload.get("baseUrl") or current.get("baseUrl") or SETTINGS.default_codex_base_url).strip()
    api_key = str(current.get("apiKey") or "")
    if payload.get("clearCodexApiKey") or payload.get("clearCustomApiKey"):
      api_key = ""
    if "codexApiKey" in payload or "apiKey" in payload:
      api_key = str(payload.get("codexApiKey") or payload.get("apiKey") or "").strip()
    if not base_url.startswith(("http://", "https://")):
      raise ValueError("codex base URL must start with http:// or https://")
    return {"baseUrl": base_url.rstrip("/"), "apiKey": api_key}

  def list_workspaces(self) -> None:
    user = self.require_user()
    self.write_json({"workspaces": WORKSPACE_STORE.list_workspaces(user)})

  def upload_api(self, method: str, path: str, query: str) -> None:
    user = self.require_user()
    parts = [unquote(part) for part in path.split("/") if part]
    if method == "POST" and len(parts) == 2:
      upload = UPLOAD_MANAGER.create(user, self.read_json())
      add_audit(user["username"], "workspace upload started", upload["id"])
      self.write_json({"upload": upload}, HTTPStatus.CREATED)
      return
    if len(parts) < 3:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
      return
    upload_id = parts[2]
    action = parts[3] if len(parts) > 3 else ""
    if method == "GET" and not action:
      self.write_json({"upload": UPLOAD_MANAGER.status(upload_id, user)})
    elif method == "POST" and action == "complete":
      result = UPLOAD_MANAGER.complete(upload_id, user)
      add_audit(user["username"], "workspace upload finalization requested", upload_id)
      self.write_json({"upload": result}, HTTPStatus.ACCEPTED)
    elif method == "PUT" and action == "files" and len(parts) == 5:
      length = int(self.headers.get("Content-Length") or -1)
      offset = int((parse_query(query).get("offset") or ["0"])[0])
      result = UPLOAD_MANAGER.receive_chunk(upload_id, user, int(parts[4]), offset, self.rfile, length)
      self.write_json({"upload": result})
    elif method == "DELETE" and not action:
      result = UPLOAD_MANAGER.cancel(upload_id, user)
      add_audit(user["username"], "workspace upload cancelled", upload_id)
      self.write_json({"upload": result})
    else:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

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
    subtail = parts[6] if len(parts) > 6 else ""
    if action == "chat":
      self.chat_api(method, workspace_id, subaction, tail, query, subtail)
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
      params = parse_query(query)
      parent = (params.get("path") or [""])[0]
      try:
        depth = int((params.get("depth") or ["3"])[0])
      except ValueError as exc:
        raise StorageError("bad_request", "file tree depth must be an integer") from exc
      self.write_json({"files": WORKSPACE_STORE.file_tree(workspace_id, parent=parent, depth=depth), "path": parent, "depth": depth})
    elif method == "POST" and action == "files" and not subaction:
      raise StorageError("upload_sessions_required", "use /api/uploads for workspace file uploads")
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

  def chat_api(self, method: str, workspace_id: str, action: str, tail: str, query: str, subtail: str = "") -> None:
    user = self.require_user()
    if method == "GET" and action == "sessions" and not tail:
      self.write_json({"sessions": CHAT_RUNTIME.list_sessions(workspace_id, user)})
    elif method == "POST" and action == "sessions" and not tail:
      payload = self.read_json()
      session = CHAT_RUNTIME.create_session(workspace_id, user, str(payload.get("title") or "") or None)
      add_audit(user["username"], "chat session created", f"{workspace_id} {session['id']}")
      self.write_json({"session": session}, HTTPStatus.CREATED)
    elif method == "PATCH" and action == "sessions" and tail:
      payload = self.read_json()
      session = CHAT_RUNTIME.update_session(workspace_id, tail, user, str(payload.get("title") or ""))
      add_audit(user["username"], "chat session renamed", f"{workspace_id} {session['id']}")
      self.write_json({"session": session})
    elif method == "DELETE" and action == "sessions" and tail and not subtail:
      deleted = CHAT_RUNTIME.delete_session(workspace_id, tail, user)
      add_audit(user["username"], "chat session deleted", f"{workspace_id} {tail} runs={len(deleted['runIds'])}")
      self.write_json({"deleted": deleted})
    elif method == "POST" and action == "sessions" and tail and subtail == "fork":
      payload = self.read_json()
      session = CHAT_RUNTIME.fork_session(workspace_id, tail, user, str(payload.get("title") or "") or None)
      add_audit(user["username"], "chat session forked", f"{workspace_id} {tail} -> {session['id']}")
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
      events = CHAT_RUNTIME.events(workspace_id, session_id, after, user)
      public_session = CHAT_RUNTIME.public_session(session_id)
      self.write_json({"events": events, "sessionStatus": public_session.get("status"), "session": public_session})
    elif method == "GET" and action == "stream" and not tail:
      params = parse_query(query)
      session_id = (params.get("sessionId") or [""])[0]
      after = int((params.get("after") or ["0"])[0] or 0)
      self.write_sse(workspace_id, session_id, after, user)
    else:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

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
        if CHAT_RUNTIME.public_session(session_id).get("status") in {"completed", "stopped", "failed"}:
          return
        idle_rounds += 1
        try:
          self.wfile.write(b": keepalive\n\n")
          self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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


def cors_headers(request: Request) -> dict[str, str]:
  origin = request.headers.get("origin")
  if not origin:
    return {}
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Credentials": "true",
  }


class AsgiHandler(Handler):
  """Compatibility adapter from the existing application methods to ASGI."""

  def __init__(self, request: Request, body=None):
    self.request = request
    self.path = str(request.url)
    self.headers = request.headers
    self.rfile = body if body is not None else io.BytesIO()
    self.response: Response | None = None

  def write_binary(
    self,
    body: bytes,
    content_type: str,
    status: HTTPStatus = HTTPStatus.OK,
    headers: dict | None = None,
    no_store: bool = False,
  ) -> None:
    response_headers = cors_headers(self.request)
    response_headers["Content-Type"] = content_type
    if no_store:
      response_headers["Cache-Control"] = "no-store"
    response_headers.update(headers or {})
    self.response = Response(content=body, status_code=int(status), headers=response_headers)

  def write_file(self, file_path: Path, content_type: str, filename: str, disposition: str) -> None:
    ascii_filename = ascii_download_filename(filename)
    headers = {
      **cors_headers(self.request),
      "Content-Disposition": f"{disposition}; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}",
      "X-Content-Type-Options": "nosniff",
      "Cache-Control": "no-store",
      "Connection": "close",
    }
    self.response = FileResponse(file_path, media_type=content_type, headers=headers)

  def serve_static(self, request_path: str) -> None:
    relative = unquote(request_path.lstrip("/")) or "index.html"
    candidate = (FRONTEND_DIR / relative).resolve()
    try:
      candidate.relative_to(FRONTEND_DIR.resolve())
    except ValueError:
      candidate = FRONTEND_DIR / "index.html"
    if not candidate.is_file():
      candidate = FRONTEND_DIR / "index.html"
    self.response = FileResponse(
      candidate,
      media_type=static_content_type(candidate),
      headers={"Cache-Control": "no-store"},
    )


class AsyncRequestReader:
  """Expose an ASGI request stream as the bounded blocking reader uploads expect."""

  _EOF = object()

  def __init__(self, loop: asyncio.AbstractEventLoop, max_chunks: int = 2):
    self.loop = loop
    self.queue: asyncio.Queue = asyncio.Queue(maxsize=max_chunks)
    self.remainder = b""

  async def feed(self, block: bytes) -> None:
    if block:
      await self.queue.put(block)

  async def finish(self, error: BaseException | None = None) -> None:
    await self.queue.put(error if error is not None else self._EOF)

  def read(self, size: int = -1) -> bytes:
    if self.remainder:
      block, self.remainder = self.remainder, b""
    else:
      item = asyncio.run_coroutine_threadsafe(self.queue.get(), self.loop).result()
      if item is self._EOF:
        return b""
      if isinstance(item, BaseException):
        raise item
      block = item
    if size >= 0 and len(block) > size:
      block, self.remainder = block[:size], block[size:]
    return block


def dispatch_request(request: Request, body) -> Response:
  handler = AsgiHandler(request, body)
  parsed = urlparse(handler.path)
  if request.method == "GET" and not parsed.path.startswith("/api/"):
    handler.serve_static(parsed.path)
  else:
    handler.handle_api(request.method, parsed.path, parsed.query)
  return handler.response or Response(status_code=HTTPStatus.NO_CONTENT)


async def dispatch_streaming_upload(request: Request) -> Response:
  loop = asyncio.get_running_loop()
  reader = AsyncRequestReader(loop)
  dispatch_task = asyncio.create_task(run_in_threadpool(dispatch_request, request, reader))

  async def pump() -> None:
    try:
      async for block in request.stream():
        if dispatch_task.done():
          return
        await reader.feed(block)
      await reader.finish()
    except BaseException as exc:
      await reader.finish(exc)

  pump_task = asyncio.create_task(pump())
  try:
    done, _ = await asyncio.wait({dispatch_task, pump_task}, return_when=asyncio.FIRST_COMPLETED)
    if pump_task in done:
      await pump_task
    return await dispatch_task
  finally:
    if not pump_task.done():
      pump_task.cancel()
    if not dispatch_task.done():
      dispatch_task.cancel()


def is_chunk_upload(method: str, path: str) -> bool:
  parts = [part for part in path.split("/") if part]
  return method == "PUT" and len(parts) == 5 and parts[:2] == ["api", "uploads"] and parts[3] == "files"


async def sse_response(request: Request) -> Response:
  handler = AsgiHandler(request)
  parsed = urlparse(handler.path)
  try:
    user = handler.require_user()
    params = parse_query(parsed.query)
    session_id = (params.get("sessionId") or [""])[0]
    after = int((params.get("after") or ["0"])[0] or 0)
    header_last_id = int(request.headers.get("Last-Event-ID") or 0)
    last_id = max(after, header_last_id)
    await run_in_threadpool(CHAT_RUNTIME.events, parsed.path.split("/")[3], session_id, last_id, user)
  except RequestStopped:
    return handler.response or Response(status_code=HTTPStatus.UNAUTHORIZED)
  except StorageError as exc:
    handler.write_json({"error": exc.code, "message": exc.message}, handler.status_for_storage_error(exc))
    return handler.response
  except ValueError as exc:
    handler.write_json({"error": "bad_request", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
    return handler.response

  async def stream():
    nonlocal last_id
    yield f"retry: {SETTINGS.sse_retry_ms}\n\n".encode("utf-8")
    idle_rounds = 0
    while idle_rounds < SETTINGS.sse_max_idle_rounds:
      signal = SSE_BROKER.prepare(session_id)
      events = await run_in_threadpool(CHAT_RUNTIME.events, parsed.path.split("/")[3], session_id, last_id, user)
      if not events:
        try:
          await asyncio.wait_for(signal.wait(), timeout=SETTINGS.sse_wait_timeout_seconds)
          continue
        except asyncio.TimeoutError:
          if (await run_in_threadpool(CHAT_RUNTIME.public_session, session_id)).get("status") in {"completed", "stopped", "failed"}:
            return
          idle_rounds += 1
          yield b": keepalive\n\n"
          continue
      idle_rounds = 0
      for event in events:
        last_id = int(event["id"])
        payload = json.dumps(event, ensure_ascii=False)
        yield f"id: {last_id}\nevent: {event['type']}\ndata: {payload}\n\n".encode("utf-8")
      if events[-1]["type"] in {"completed", "stopped", "failed"}:
        return

  headers = {
    **cors_headers(request),
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-store",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
  }
  return StreamingResponse(stream(), headers=headers)


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
  SSE_BROKER.bind(asyncio.get_running_loop())
  CHAT_RUNTIME.set_event_callback(SSE_BROKER.persisted)
  threading.Thread(target=reconcile_micu_accounts, name="micu-account-reconciler", daemon=True).start()
  try:
    yield
  finally:
    CHAT_RUNTIME.set_event_callback(None)
    SSE_BROKER.unbind()


app = FastAPI(
  title="AI Audit",
  docs_url=None,
  redoc_url=None,
  openapi_url=None,
  lifespan=app_lifespan,
)


@app.options("/{request_path:path}")
async def options_route(request: Request, request_path: str) -> Response:
  return Response(
    status_code=HTTPStatus.NO_CONTENT,
    headers={
      "Access-Control-Allow-Origin": request.headers.get("origin", "*"),
      "Access-Control-Allow-Credentials": "true",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
      "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
    },
  )


@app.api_route("/{request_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def application_route(request: Request, request_path: str) -> Response:
  path = "/" + request_path
  if request.method == "GET" and path.startswith("/api/workspaces/") and path.endswith("/chat/stream"):
    return await sse_response(request)
  if is_chunk_upload(request.method, path):
    try:
      return await dispatch_streaming_upload(request)
    except ClientDisconnect:
      return Response(status_code=499)
  body = await request.body()
  return await run_in_threadpool(dispatch_request, request, io.BytesIO(body))


def main() -> None:
  parser = argparse.ArgumentParser(description="AI Audit prototype backend")
  parser.add_argument("--host", default=SETTINGS.host)
  parser.add_argument("--port", type=int, default=SETTINGS.port)
  args = parser.parse_args()
  import uvicorn

  uvicorn.run(app, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
  main()
