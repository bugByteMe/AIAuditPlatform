from __future__ import annotations

import threading
import time
import secrets
from decimal import Decimal
from pathlib import Path

from account_store import AccountStore
from chat_common import KeyedLockPool
from chat_runtime import ChatRuntime
from config import SETTINGS
from micu_api import MicuApiClient, MicuApiError, parse_cny
from server_utils import hash_password, invite_digest
from upload_store import UploadManager
from wechat_pay import WechatPayClient
from workspace_store import StorageError, WorkspaceStore

FRONTEND_DIR = SETTINGS.frontend_dir

WORKSPACE_STORAGE_DIR = SETTINGS.workspace_storage_dir

SESSION_COOKIE = SETTINGS.session_cookie

SESSION_TTL_SECONDS = SETTINGS.session_ttl_seconds

PBKDF2_ITERATIONS = SETTINGS.pbkdf2_iterations

WORKSPACE_STORE = WorkspaceStore(WORKSPACE_STORAGE_DIR, SETTINGS.database_url)

REGISTRATION_LOCKS = KeyedLockPool()

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

WECHAT_PAY = WechatPayClient(
  enabled=SETTINGS.wechat_pay_enabled,
  app_id=SETTINGS.wechat_app_id,
  merchant_id=SETTINGS.wechat_merchant_id,
  notify_url=SETTINGS.wechat_notify_url,
  api_v3_key=SETTINGS.wechat_api_v3_key,
  merchant_cert_file=SETTINGS.wechat_merchant_cert_file,
  merchant_key_file=SETTINGS.wechat_merchant_key_file,
  public_key_id=SETTINGS.wechat_public_key_id,
  public_key_file=SETTINGS.wechat_public_key_file,
  timeout_seconds=SETTINGS.wechat_request_timeout_seconds,
)

RECHARGE_PRODUCTS = {
  "1.00": {"amountFen": 100, "creditCny": "0.80", "debug": True},
  "50.00": {"amountFen": 5_000, "creditCny": "40.00", "debug": False},
  "100.00": {"amountFen": 10_000, "creditCny": "80.00", "debug": False},
  "200.00": {"amountFen": 20_000, "creditCny": "160.00", "debug": False},
}

WECHAT_CALLBACK_MAX_BYTES = 64 * 1024

WECHAT_RECONCILE_STOP = threading.Event()

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
