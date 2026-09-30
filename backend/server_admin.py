from __future__ import annotations

import io
import json
import secrets
import time
from decimal import Decimal
from http import HTTPStatus
from urllib.parse import unquote, urlparse

from micu_api import MicuApiError, parse_cny
from recharge_import import MAX_WORKBOOK_BYTES, parse_recharge_workbook, payment_key
from server_recharge import apply_wechat_transaction, public_recharge_order, reconcile_wechat_order
from server_state import *
from server_utils import hash_password, invite_digest, utc_timestamp, verify_password
from wechat_pay import WechatPayError
from workspace_store import StorageError, parse_query, parse_urlencoded_paths

class AdminHandlerMixin:
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
    ACCOUNT_STORE.save_user(account)
    add_audit(actor["username"], "account MicuAPI balance added", f"{account['id']} amountCny={balance['addedCny']} balanceCny={balance['remainingCny']}")
    self.write_json({"account": admin_account(account)})

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
    if not ({"name", "diskLimitBytes", "liveRunLimit"} & payload.keys()):
      raise ValueError("name, diskLimitBytes, or liveRunLimit is required")
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
    if "name" in payload:
      group = ACCOUNT_STORE.update_group_name(group_id, str(payload.get("name") or ""))
      add_audit(actor["username"], "group name updated", f"{group_id} name={group['name']}")
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
        ACCOUNT_STORE.save_user(user)
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
        for member in active_users:
          ACCOUNT_STORE.save_user(member)
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
        ACCOUNT_STORE.save_user(USERS[username])
      except Exception:
        USERS.pop(username, None)
        raise
    add_audit(actor["username"], "account created", username)
    self.write_json({"account": public_user(USERS[username])}, HTTPStatus.CREATED)

  def update_account(self, raw_username: str) -> None:
    actor = self.require_admin()
    username = unquote(raw_username)
    user = user_by_identifier(username)
    is_pending = False
    if not user:
      user = ACCOUNT_STORE.pending_accounts.get(username)
      is_pending = bool(user)
    if not user:
      self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
      return
    payload = self.read_json()
    was_enabled = bool(user.get("enabled", True))
    if "groupId" in payload:
      group_id = str(payload.get("groupId") or "")
      group = ACCOUNT_STORE.groups.get(group_id) if group_id else None
      if group_id and not group:
        raise ValueError("group not found")
      user["groupId"] = group_id
      user["group"] = str(group.get("name") or "") if group else ""
    if "maxSessions" in payload:
      max_sessions = int(payload["maxSessions"])
      if max_sessions < 0 or max_sessions > SETTINGS.account_max_sessions_limit:
        raise ValueError("maxSessions is outside the allowed range")
      user["maxSessions"] = max_sessions
    for key in ["displayName", "role", "enabled"]:
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
    if is_pending:
      ACCOUNT_STORE.save_pending(user)
    else:
      ACCOUNT_STORE.save_user(user)
    add_audit(actor["username"], "account updated", username)
    self.write_json({"account": public_user(user)})

  def audit_logs(self) -> None:
    self.require_admin()
    self.write_json({"logs": AUDIT_LOGS})

  def workers(self) -> None:
    self.require_admin()
    self.write_json({"workers": CHAT_RUNTIME.worker_status()})
