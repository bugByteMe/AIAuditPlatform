from __future__ import annotations

import json
import os
import secrets
import threading
import time
from copy import deepcopy
from pathlib import Path

from config import SETTINGS


SCHEMA_VERSION = 6
DEFAULT_GROUP_LIVE_RUN_LIMIT = 1


def _new_id(prefix: str) -> str:
  return f"{prefix}_{secrets.token_urlsafe(12)}"


def _timestamp() -> str:
  return time.strftime("%Y-%m-%d %H:%M:%S")


class AccountStore:
  def __init__(self, path: Path, seed_users: dict[str, dict]):
    self.path = path
    self.seed_users = deepcopy(seed_users)
    self.lock = threading.RLock()
    self.users: dict[str, dict] = {}
    self.pending_accounts: dict[str, dict] = {}
    self.groups: dict[str, dict] = {}
    self.recharge_payments: dict[str, dict] = {}
    self.load_or_seed()

  def load_or_seed(self) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    existed = self.path.exists()
    raw = json.loads(self.path.read_text(encoding="utf-8")) if existed else deepcopy(self.seed_users)
    migrated = not (isinstance(raw, dict) and raw.get("schemaVersion") == SCHEMA_VERSION)
    if isinstance(raw, dict) and raw.get("schemaVersion") in {2, 3, 4, 5}:
      state = self.migrate_versioned(raw)
    else:
      state = self.migrate_legacy(raw) if migrated else raw
    self.users.update(deepcopy(state.get("users") or {}))
    self.pending_accounts.update(deepcopy(state.get("pendingAccounts") or {}))
    self.groups.update(deepcopy(state.get("groups") or {}))
    self.recharge_payments.update(deepcopy(state.get("rechargePayments") or {}))
    recovered = self._normalize_provider_state()
    if migrated or not existed or recovered:
      self.save()

  def migrate_legacy(self, users: dict[str, dict]) -> dict:
    migrated_users: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    group_ids_by_name: dict[str, str] = {}
    now = _timestamp()
    for storage_key, source in (users or {}).items():
      user = deepcopy(source)
      username = str(user.get("username") or storage_key)
      group_name = str(user.get("group") or "").strip()
      group_id = ""
      if group_name:
        normalized = group_name.casefold()
        group_id = group_ids_by_name.get(normalized, "")
        if not group_id:
          group_id = _new_id("grp")
          group_ids_by_name[normalized] = group_id
          groups[group_id] = {
            "id": group_id,
            "name": group_name,
            "createdAt": now,
            "diskLimitBytes": None,
            "liveRunLimit": DEFAULT_GROUP_LIVE_RUN_LIMIT,
          }
      user.update(
        {
          "id": str(user.get("id") or _new_id("usr")),
          "username": username,
          "groupId": str(user.get("groupId") or group_id),
          "group": group_name,
          "status": str(user.get("status") or "active"),
        }
      )
      migrated_users[username] = user
    return {
      "schemaVersion": SCHEMA_VERSION,
      "users": migrated_users,
      "pendingAccounts": {},
      "groups": groups,
      "rechargePayments": {},
    }

  def migrate_versioned(self, state: dict) -> dict:
    migrated = deepcopy(state)
    migrated["schemaVersion"] = SCHEMA_VERSION
    for group in (migrated.get("groups") or {}).values():
      group.setdefault("diskLimitBytes", None)
      group.setdefault("liveRunLimit", DEFAULT_GROUP_LIVE_RUN_LIMIT)
    migrated.setdefault("rechargePayments", {})
    return migrated

  def _normalize_provider_state(self) -> bool:
    changed = False
    for user in self.users.values():
      legacy = user.get("codex") or {}
      legacy_base = str(legacy.get("baseUrl") or "").rstrip("/")
      micu_base = str(SETTINGS.micu_inference_url or SETTINGS.default_codex_base_url).rstrip("/")
      mode = str(user.get("providerMode") or ("micu" if not legacy_base or legacy_base == micu_base else "custom"))
      user["providerMode"] = mode if mode in {"micu", "custom"} else "micu"
      user.setdefault("micu", {})
      if "customCodex" not in user:
        user["customCodex"] = {
          "baseUrl": legacy_base if legacy_base and legacy_base != micu_base else "",
          "apiKey": str(legacy.get("apiKey") or "") if legacy_base and legacy_base != micu_base else "",
        }
      user.pop("budgetTokens", None)
    for account in self.pending_accounts.values():
      if "initialBudgetCny" not in account:
        account["initialBudgetCny"] = "0.00"
      account.pop("budgetTokens", None)
    for payment in self.recharge_payments.values():
      if payment.get("status") == "processing":
        payment.update({"status": "review_required", "reason": "server restarted before provider result was persisted"})
        changed = True
    return changed

  def state(self) -> dict:
    return {
      "schemaVersion": SCHEMA_VERSION,
      "users": self.users,
      "pendingAccounts": self.pending_accounts,
      "groups": self.groups,
      "rechargePayments": self.recharge_payments,
    }

  def save(self, users: dict[str, dict] | None = None) -> None:
    with self.lock:
      self._save_unlocked(users)

  def _save_unlocked(self, users: dict[str, dict] | None = None) -> None:
    if users is not None and users is not self.users:
      self.users.clear()
      self.users.update(users)
    self.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = self.path.with_suffix(".tmp")
    tmp.write_text(json.dumps(self.state(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(self.path)
    os.chmod(self.path, 0o600)

  def group_by_name(self, name: str) -> dict | None:
    normalized = name.strip().casefold()
    return next((group for group in self.groups.values() if str(group.get("name") or "").casefold() == normalized), None)

  def username_exists(self, username: str) -> bool:
    normalized = username.strip().casefold()
    return any(existing.casefold() == normalized for existing in self.users)

  def create_group(self, name: str) -> dict:
    with self.lock:
      return self._create_group_unlocked(name)

  def _create_group_unlocked(self, name: str) -> dict:
    name = name.strip()
    if not name:
      raise ValueError("group name is required")
    if self.group_by_name(name):
      raise ValueError("group name already exists")
    group_id = _new_id("grp")
    group = {
      "id": group_id,
      "name": name,
      "createdAt": _timestamp(),
      "diskLimitBytes": None,
      "liveRunLimit": DEFAULT_GROUP_LIVE_RUN_LIMIT,
    }
    self.groups[group_id] = group
    try:
      self.save()
    except Exception:
      self.groups.pop(group_id, None)
      raise
    return deepcopy(group)

  def create_batch(self, **kwargs) -> tuple[dict, list[dict]]:
    with self.lock:
      return self._create_batch_unlocked(**kwargs)

  def _create_batch_unlocked(
    self,
    *,
    group_id: str = "",
    new_group_name: str = "",
    count: int,
    budget_cny: str | int | float | None = None,
    budget_tokens: int | None = None,
    max_sessions: int,
  ) -> tuple[dict, list[dict]]:
    group_id = group_id.strip()
    new_group_name = new_group_name.strip()
    if bool(group_id) == bool(new_group_name):
      raise ValueError("provide exactly one of groupId or newGroupName")
    if not 1 <= count <= SETTINGS.batch_invite_max_count:
      raise ValueError(f"count must be between 1 and {SETTINGS.batch_invite_max_count}")
    from micu_api import parse_cny
    initial_budget = parse_cny(budget_cny if budget_cny is not None else (budget_tokens or 0))
    if not 0 <= max_sessions <= SETTINGS.account_max_sessions_limit:
      raise ValueError(f"maxSessions must be between 0 and {SETTINGS.account_max_sessions_limit}")

    new_group = None
    if new_group_name:
      if self.group_by_name(new_group_name):
        raise ValueError("group name already exists")
      group_id = _new_id("grp")
      new_group = {
        "id": group_id,
        "name": new_group_name,
        "createdAt": _timestamp(),
        "diskLimitBytes": None,
        "liveRunLimit": DEFAULT_GROUP_LIVE_RUN_LIMIT,
      }
      group = new_group
    else:
      group = self.groups.get(group_id)
      if not group:
        raise ValueError("group not found")

    created = []
    now = _timestamp()
    used_ids = {str(user.get("id") or "") for user in self.users.values()} | set(self.pending_accounts)
    used_tokens = {str(account.get("inviteToken") or "") for account in self.pending_accounts.values()}
    for _ in range(count):
      user_id = _new_id("usr")
      while user_id in used_ids:
        user_id = _new_id("usr")
      used_ids.add(user_id)
      token = secrets.token_urlsafe(32)
      while token in used_tokens:
        token = secrets.token_urlsafe(32)
      used_tokens.add(token)
      created.append(
        {
          "id": user_id,
          "username": None,
          "displayName": "",
          "role": "user",
          "groupId": group_id,
          "group": group["name"],
          "initialBudgetCny": format(initial_budget, ".2f"),
          "usedTokens": 0,
          "enabled": True,
          "maxSessions": max_sessions,
          "status": "pending",
          "inviteToken": token,
          "createdAt": now,
        }
      )

    previous_groups = deepcopy(self.groups)
    previous_pending = deepcopy(self.pending_accounts)
    try:
      if new_group:
        self.groups[group_id] = new_group
      self.pending_accounts.update({account["id"]: account for account in created})
      self.save()
    except Exception:
      self.groups.clear()
      self.groups.update(previous_groups)
      self.pending_accounts.clear()
      self.pending_accounts.update(previous_pending)
      raise
    return deepcopy(group), deepcopy(created)

  def pending_by_token(self, token: str) -> dict | None:
    if not token:
      return None
    for account in self.pending_accounts.values():
      stored = str(account.get("inviteToken") or "")
      if stored and secrets.compare_digest(stored, token):
        return account
    return None

  def activate(
    self,
    token: str,
    username: str,
    password_hash: str,
    codex: dict | None = None,
    micu: dict | None = None,
    provider_mode: str = "micu",
  ) -> dict:
    with self.lock:
      return self._activate_unlocked(token, username, password_hash, codex, micu, provider_mode)

  def _activate_unlocked(
    self,
    token: str,
    username: str,
    password_hash: str,
    codex: dict | None = None,
    micu: dict | None = None,
    provider_mode: str = "micu",
  ) -> dict:
    account = self.pending_by_token(token)
    if not account or account.get("status") != "pending":
      raise ValueError("invalid invite token")
    if self.username_exists(username):
      raise ValueError("username already exists")

    user_id = account["id"]
    active = {key: deepcopy(value) for key, value in account.items() if key != "inviteToken"}
    active.update(
      {
        "username": username,
        "displayName": username,
        "passwordHash": password_hash,
        "status": "active",
        "activatedAt": _timestamp(),
        "providerMode": provider_mode,
        "micu": deepcopy(micu or {}),
        "customCodex": deepcopy(codex or {}),
      }
    )
    previous_pending = deepcopy(account)
    self.pending_accounts.pop(user_id)
    self.users[username] = active
    try:
      self.save()
    except Exception:
      self.users.pop(username, None)
      self.pending_accounts[user_id] = previous_pending
      raise
    return active

  def revoke_invite(self, user_id: str) -> dict | None:
    with self.lock:
      return self._revoke_invite_unlocked(user_id)

  def _revoke_invite_unlocked(self, user_id: str) -> dict | None:
    account = self.pending_accounts.get(user_id)
    if not account:
      return None
    if account.get("status") != "pending":
      raise ValueError("invite is not pending")
    account["status"] = "revoked"
    account["inviteToken"] = ""
    account["revokedAt"] = _timestamp()
    self.save()
    return account

  def update_group_disk_limit(self, group_id: str, disk_limit_bytes: int | None) -> dict:
    with self.lock:
      group = self.groups.get(group_id)
      if not group:
        raise ValueError("group not found")
      if disk_limit_bytes is not None and disk_limit_bytes < 0:
        raise ValueError("diskLimitBytes must be non-negative or null")
      previous = group.get("diskLimitBytes")
      group["diskLimitBytes"] = disk_limit_bytes
      try:
        self.save()
      except Exception:
        group["diskLimitBytes"] = previous
        raise
      return deepcopy(group)

  def update_group_live_run_limit(self, group_id: str, live_run_limit: int) -> dict:
    with self.lock:
      group = self.groups.get(group_id)
      if not group:
        raise ValueError("group not found")
      if live_run_limit < 1:
        raise ValueError("liveRunLimit must be at least 1")
      previous = group.get("liveRunLimit", DEFAULT_GROUP_LIVE_RUN_LIMIT)
      group["liveRunLimit"] = live_run_limit
      try:
        self.save()
      except Exception:
        group["liveRunLimit"] = previous
        raise
      return deepcopy(group)

  def user_by_identifier(self, identifier: str) -> dict | None:
    user = self.users.get(identifier)
    if user:
      return user
    return next((item for item in self.users.values() if item.get("id") == identifier), None)

  def recharge_payment(self, key: str) -> dict | None:
    with self.lock:
      payment = self.recharge_payments.get(key)
      return deepcopy(payment) if payment else None

  def reserve_recharge_payment(self, key: str, payment: dict) -> tuple[bool, dict]:
    with self.lock:
      existing = self.recharge_payments.get(key)
      if existing:
        return False, deepcopy(existing)
      reserved = {**deepcopy(payment), "id": f"rch_{key[:16]}", "status": "processing", "reason": ""}
      self.recharge_payments[key] = reserved
      try:
        self.save()
      except Exception:
        self.recharge_payments.pop(key, None)
        raise
      return True, deepcopy(reserved)

  def finish_recharge_payment(self, key: str, *, status: str, reason: str = "", binding_updates: dict | None = None) -> dict:
    with self.lock:
      payment = self.recharge_payments.get(key)
      if not payment:
        raise ValueError("recharge payment reservation was not found")
      previous_payment = deepcopy(payment)
      user = self.user_by_identifier(str(payment.get("userId") or ""))
      previous_binding = deepcopy((user or {}).get("micu") or {})
      payment.update({"status": status, "reason": reason, "completedAt": _timestamp()})
      if binding_updates:
        if not user:
          raise ValueError("recharge account was not found")
        user.setdefault("micu", {}).update(deepcopy(binding_updates))
      try:
        self.save()
      except Exception:
        self.recharge_payments[key] = previous_payment
        if user is not None:
          user["micu"] = previous_binding
        raise
      return deepcopy(payment)

  def recharge_history(self, user_id: str) -> list[dict]:
    with self.lock:
      rows = [
        {
          "id": payment["id"],
          "paidAt": payment["paidAt"],
          "amountCny": payment["amountCny"],
          "importedAt": payment["importedAt"],
        }
        for payment in self.recharge_payments.values()
        if payment.get("status") == "applied" and str(payment.get("userId") or "") == user_id
      ]
      return sorted(rows, key=lambda item: (item["paidAt"], item["importedAt"], item["id"]), reverse=True)

  def _tombstone_recharge_payments(self, user_ids: set[str]) -> None:
    for payment in self.recharge_payments.values():
      if str(payment.get("userId") or "") not in user_ids:
        continue
      payment.clear()
      payment.update({"id": f"rch_deleted_{secrets.token_hex(6)}", "status": "used", "deletedAt": _timestamp()})

  def remove_accounts(self, user_ids: set[str]) -> tuple[list[dict], list[dict]]:
    with self.lock:
      removed_users = [deepcopy(user) for user in self.users.values() if str(user.get("id")) in user_ids]
      removed_pending = [deepcopy(account) for account in self.pending_accounts.values() if str(account.get("id")) in user_ids]
      previous_users = deepcopy(self.users)
      previous_pending = deepcopy(self.pending_accounts)
      previous_payments = deepcopy(self.recharge_payments)
      try:
        for username, user in list(self.users.items()):
          if str(user.get("id")) in user_ids:
            self.users.pop(username, None)
        for user_id in user_ids:
          self.pending_accounts.pop(user_id, None)
        self._tombstone_recharge_payments(user_ids)
        self.save()
      except Exception:
        self.users.clear()
        self.users.update(previous_users)
        self.pending_accounts.clear()
        self.pending_accounts.update(previous_pending)
        self.recharge_payments.clear()
        self.recharge_payments.update(previous_payments)
        raise
      return removed_users, removed_pending

  def remove_group_and_accounts(self, group_id: str, user_ids: set[str]) -> tuple[dict, list[dict], list[dict]]:
    with self.lock:
      group = self.groups.get(group_id)
      if not group:
        raise ValueError("group not found")
      previous_groups = deepcopy(self.groups)
      previous_users = deepcopy(self.users)
      previous_pending = deepcopy(self.pending_accounts)
      previous_payments = deepcopy(self.recharge_payments)
      removed_users = [deepcopy(user) for user in self.users.values() if str(user.get("id")) in user_ids]
      removed_pending = [deepcopy(account) for account in self.pending_accounts.values() if str(account.get("id")) in user_ids]
      try:
        for username, user in list(self.users.items()):
          if str(user.get("id")) in user_ids:
            self.users.pop(username, None)
        for user_id in user_ids:
          self.pending_accounts.pop(user_id, None)
        self._tombstone_recharge_payments(user_ids)
        removed_group = deepcopy(self.groups.pop(group_id))
        self.save()
      except Exception:
        self.groups.clear()
        self.groups.update(previous_groups)
        self.users.clear()
        self.users.update(previous_users)
        self.pending_accounts.clear()
        self.pending_accounts.update(previous_pending)
        self.recharge_payments.clear()
        self.recharge_payments.update(previous_payments)
        raise
      return removed_group, removed_users, removed_pending
