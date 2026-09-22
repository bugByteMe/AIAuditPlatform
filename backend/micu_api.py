from __future__ import annotations

import json
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class MicuApiError(RuntimeError):
  def __init__(self, code: str, message: str):
    super().__init__(message)
    self.code = code


def parse_cny(value) -> Decimal:
  try:
    raw = Decimal(str(value))
    amount = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
  except (InvalidOperation, ValueError) as exc:
    raise ValueError("budgetCny must be a valid CNY amount") from exc
  if raw != amount:
    raise ValueError("budgetCny must have at most two decimal places")
  if amount < 0:
    raise ValueError("budgetCny must be non-negative")
  return amount


class MicuApiClient:
  def __init__(
    self,
    management_url: str,
    inference_url: str,
    access_token: str,
    user_id: str,
    group: str,
    quota_per_cny: int,
    timeout: float,
  ):
    self.management_url = management_url.rstrip("/")
    self.inference_url = inference_url.rstrip("/")
    self.access_token = access_token.strip()
    self.user_id = str(user_id).strip()
    self.group = group.strip()
    self.quota_per_cny = int(quota_per_cny)
    self.timeout = max(1.0, float(timeout))
    self.lock = threading.RLock()
    if self.quota_per_cny <= 0:
      raise ValueError("MicuAPI quota_per_cny must be positive")

  @property
  def configured(self) -> bool:
    return bool(self.access_token and self.user_id)

  def _request(self, method: str, path: str, payload: dict | None = None, *, api_key: str = "") -> dict:
    headers = {
      "Accept": "application/json",
      "User-Agent": "AI-AuditPlatform/1.0",
    }
    if api_key:
      headers["Authorization"] = f"Bearer {api_key}"
    else:
      if not self.configured:
        raise MicuApiError("not_configured", "MicuAPI management credentials are not configured")
      headers["Authorization"] = f"Bearer {self.access_token}"
      headers["New-Api-User"] = self.user_id
    body = None
    if payload is not None:
      body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
      headers["Content-Type"] = "application/json"
    request = Request(f"{self.management_url}{path}", data=body, headers=headers, method=method)
    try:
      with urlopen(request, timeout=self.timeout) as response:
        result = json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as exc:
      message = "MicuAPI rejected the request"
      raw_content_type = str(exc.headers.get("Content-Type") or "") if exc.headers else ""
      response_type = raw_content_type.split(";", 1)[0].strip().lower() or "unknown"
      try:
        error = json.loads(exc.read().decode("utf-8") or "{}")
        provider_message = error.get("message") if isinstance(error, dict) else None
        if isinstance(provider_message, str) and provider_message.strip():
          message = " ".join(provider_message.split())[:300]
      except Exception:
        pass
      detail = f"{message} (HTTP {exc.code}, {response_type})"
      raise MicuApiError("not_found" if exc.code == 404 else "http_error", detail) from exc
    except (URLError, TimeoutError, OSError) as exc:
      raise MicuApiError("unavailable", "MicuAPI is unavailable") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
      raise MicuApiError("invalid_response", "MicuAPI returned an invalid response") from exc
    if result.get("success") is False:
      raise MicuApiError("api_error", str(result.get("message") or "MicuAPI request failed"))
    return result

  def quota_from_cny(self, amount) -> int:
    cny = parse_cny(amount)
    return int((cny * self.quota_per_cny).to_integral_value(rounding=ROUND_HALF_UP))

  def cny_from_quota(self, quota: int) -> str:
    amount = (Decimal(max(0, int(quota))) / Decimal(self.quota_per_cny)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(amount, ".2f")

  @staticmethod
  def remaining_percent(remaining_quota: int, used_quota: int, unlimited: bool = False) -> float:
    if unlimited:
      return 100.0
    remaining = max(0, int(remaining_quota))
    used = max(0, int(used_quota))
    total = remaining + used
    return round((remaining * 100.0 / total) if total else 0.0, 1)

  def all_tokens(self) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
      result = self._request("GET", f"/api/token/?{urlencode({'p': page, 'page_size': 100})}")
      data = result.get("data") or {}
      batch = list(data.get("items") or [])
      items.extend(batch)
      total = int(data.get("total") or len(items))
      if not batch or len(items) >= total:
        return items
      page += 1

  def find_token(self, name: str) -> dict | None:
    query = urlencode({"keyword": name, "p": 1, "page_size": 100})
    result = self._request("GET", f"/api/token/search?{query}")
    data = result.get("data") or {}
    exact = [item for item in data.get("items") or [] if str(item.get("name") or "") == name]
    if len(exact) > 1:
      raise MicuApiError("duplicate_token", f"multiple MicuAPI tokens are named {name}")
    return exact[0] if exact else None

  def token(self, token_id: int) -> dict:
    result = self._request("GET", f"/api/token/{int(token_id)}")
    token = result.get("data") or {}
    if not token:
      raise MicuApiError("missing_token", "MicuAPI token was not found")
    return token

  def full_key(self, token_id: int) -> str:
    result = self._request("POST", f"/api/token/{int(token_id)}/key", {})
    data = result.get("data")
    if isinstance(data, dict):
      key = str(data.get("key") or "")
    else:
      key = str(data or "")
    if not key:
      raise MicuApiError("missing_key", "MicuAPI did not return the token key")
    return key if key.startswith("sk-") else f"sk-{key}"

  def create_token(self, name: str, balance_cny) -> dict:
    self._request(
      "POST",
      "/api/token/",
      {
        "name": name,
        "remain_quota": self.quota_from_cny(balance_cny),
        "expired_time": -1,
        "unlimited_quota": False,
        "model_limits_enabled": False,
        "model_limits": "",
        "allow_ips": "",
        "group": self.group,
        "cross_group_retry": False,
      },
    )
    token = self.find_token(name)
    if not token:
      raise MicuApiError("create_unconfirmed", "created MicuAPI token could not be found")
    return token

  def ensure_binding(self, username: str, initial_balance_cny) -> dict:
    with self.lock:
      token = self.find_token(username)
      created = False
      if not token:
        token = self.create_token(username, initial_balance_cny)
        created = True
      key = self.full_key(int(token["id"]))
      return {
        "tokenId": int(token["id"]),
        "tokenName": username,
        "apiKey": key,
        "group": str(token.get("group") or self.group),
        "status": "ready",
        "lastBalanceCny": self.cny_from_quota(int(token.get("remain_quota") or 0)),
        "lastRemainingPercent": self.remaining_percent(
          int(token.get("remain_quota") or 0),
          int(token.get("used_quota") or 0),
          bool(token.get("unlimited_quota")),
        ),
        "lastSyncedAt": int(time.time()),
        "lastError": "",
        "createdByReconcile": created,
      }

  def align_binding_name(self, binding: dict, username: str) -> str:
    token_id = int(binding.get("tokenId") or 0)
    if not token_id:
      raise MicuApiError("missing_token", "MicuAPI binding does not contain a token ID")
    token = self.token(token_id)
    current_name = str(token.get("name") or "")
    if current_name != username:
      conflict = self.find_token(username)
      if conflict and int(conflict.get("id") or 0) != token_id:
        raise MicuApiError("duplicate_token", f"another MicuAPI token is already named {username}")
      payload = {key: value for key, value in token.items() if key not in {"key", "DeletedAt"}}
      payload.update({"id": token_id, "name": username})
      self._request("PUT", "/api/token/", payload)
    binding["tokenName"] = username
    return username

  def balance(self, binding: dict) -> dict:
    token = self.token(int(binding.get("tokenId") or 0))
    return self.balance_from_token(token)

  def balance_from_token(self, token: dict) -> dict:
    remaining_quota = int(token.get("remain_quota") or 0)
    used_quota = int(token.get("used_quota") or 0)
    token_status = int(token.get("status") or 0)
    if not token.get("unlimited_quota") and remaining_quota <= 0:
      status = "exhausted"
    elif token_status != 1:
      status = "disabled"
    else:
      status = "ready"
    return {
      "rawQuota": remaining_quota,
      "remainingCny": self.cny_from_quota(remaining_quota),
      "remainingPercent": self.remaining_percent(remaining_quota, used_quota, bool(token.get("unlimited_quota"))),
      "status": status,
      "tokenStatus": token_status,
    }

  def add_balance(self, binding: dict, amount_cny) -> dict:
    amount = parse_cny(amount_cny)
    with self.lock:
      token = self.token(int(binding.get("tokenId") or 0))
      current = max(0, int(token.get("remain_quota") or 0))
      new_quota = current + self.quota_from_cny(amount)
      payload = {key: value for key, value in token.items() if key not in {"key", "DeletedAt"}}
      payload.update({"id": int(token["id"]), "remain_quota": new_quota, "unlimited_quota": False})
      self._request("PUT", "/api/token/", payload)
      if new_quota > 0 and int(token.get("status") or 0) != 1:
        self._request("PUT", "/api/token/?status_only=true", {"id": int(token["id"]), "status": 1})
    return {
      "addedCny": format(amount, ".2f"),
      "rawQuota": new_quota,
      "remainingCny": self.cny_from_quota(new_quota),
      "remainingPercent": self.remaining_percent(new_quota, int(token.get("used_quota") or 0)),
      "status": "ready" if new_quota > 0 else "exhausted",
    }

  def set_enabled(self, binding: dict, enabled: bool) -> None:
    token_id = int(binding.get("tokenId") or 0)
    if not token_id:
      return
    self._request("PUT", "/api/token/?status_only=true", {"id": token_id, "status": 1 if enabled else 2})

  def delete_token(self, binding: dict) -> None:
    token_id = int(binding.get("tokenId") or 0)
    if token_id:
      try:
        self._request("DELETE", f"/api/token/{token_id}")
      except MicuApiError as exc:
        if exc.code != "not_found":
          raise
