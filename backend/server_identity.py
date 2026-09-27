from __future__ import annotations

import io
import hmac
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

class IdentityHandlerMixin:
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
