from __future__ import annotations

import json
import os
import secrets
import threading
import time
from copy import deepcopy
from pathlib import Path

from config import SETTINGS


SCHEMA_VERSION = 2


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
    self.load_or_seed()

  def load_or_seed(self) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    existed = self.path.exists()
    raw = json.loads(self.path.read_text(encoding="utf-8")) if existed else deepcopy(self.seed_users)
    migrated = not (isinstance(raw, dict) and raw.get("schemaVersion") == SCHEMA_VERSION)
    state = self.migrate_legacy(raw) if migrated else raw
    self.users.update(deepcopy(state.get("users") or {}))
    self.pending_accounts.update(deepcopy(state.get("pendingAccounts") or {}))
    self.groups.update(deepcopy(state.get("groups") or {}))
    if migrated or not existed:
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
          groups[group_id] = {"id": group_id, "name": group_name, "createdAt": now}
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
    }

  def state(self) -> dict:
    return {
      "schemaVersion": SCHEMA_VERSION,
      "users": self.users,
      "pendingAccounts": self.pending_accounts,
      "groups": self.groups,
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
    group = {"id": group_id, "name": name, "createdAt": _timestamp()}
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
    budget_tokens: int,
    max_sessions: int,
  ) -> tuple[dict, list[dict]]:
    group_id = group_id.strip()
    new_group_name = new_group_name.strip()
    if bool(group_id) == bool(new_group_name):
      raise ValueError("provide exactly one of groupId or newGroupName")
    if not 1 <= count <= SETTINGS.batch_invite_max_count:
      raise ValueError(f"count must be between 1 and {SETTINGS.batch_invite_max_count}")
    if budget_tokens < 0:
      raise ValueError("budgetTokens must be non-negative")
    if not 1 <= max_sessions <= SETTINGS.account_max_sessions_limit:
      raise ValueError(f"maxSessions must be between 1 and {SETTINGS.account_max_sessions_limit}")

    new_group = None
    if new_group_name:
      if self.group_by_name(new_group_name):
        raise ValueError("group name already exists")
      group_id = _new_id("grp")
      new_group = {"id": group_id, "name": new_group_name, "createdAt": _timestamp()}
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
          "budgetTokens": budget_tokens,
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

  def activate(self, token: str, username: str, password_hash: str, codex: dict | None = None) -> dict:
    with self.lock:
      return self._activate_unlocked(token, username, password_hash, codex)

  def _activate_unlocked(self, token: str, username: str, password_hash: str, codex: dict | None = None) -> dict:
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
        "codex": deepcopy(codex or {}),
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
