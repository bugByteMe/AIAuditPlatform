from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class WechatPayError(RuntimeError):
  def __init__(self, code: str, message: str):
    super().__init__(message)
    self.code = code
    self.message = message


class WechatPayClient:
  API_BASE = "https://api.mch.weixin.qq.com"

  def __init__(
    self,
    *,
    enabled: bool,
    app_id: str,
    merchant_id: str,
    notify_url: str,
    api_v3_key: str,
    merchant_cert_file: Path | None,
    merchant_key_file: Path | None,
    public_key_id: str,
    public_key_file: Path | None,
    timeout_seconds: float = 10,
    timestamp_tolerance_seconds: int = 300,
  ):
    self.enabled = enabled
    self.app_id = app_id.strip()
    self.merchant_id = merchant_id.strip()
    self.notify_url = notify_url.strip()
    self.api_v3_key = api_v3_key.encode("utf-8")
    self.merchant_cert_file = merchant_cert_file
    self.merchant_key_file = merchant_key_file
    self.public_key_id = public_key_id.strip()
    self.public_key_file = public_key_file
    self.timeout_seconds = timeout_seconds
    self.timestamp_tolerance_seconds = timestamp_tolerance_seconds
    self._merchant_private_key = None
    self._wechat_public_key = None
    self._merchant_serial = ""

  @property
  def configured(self) -> bool:
    return self.enabled and not self.configuration_errors()

  def configuration_errors(self) -> list[str]:
    if not self.enabled:
      return ["WeChat Pay is disabled"]
    errors = []
    if not self.app_id:
      errors.append("AppID is missing")
    if not self.merchant_id:
      errors.append("merchant ID is missing")
    if not self.notify_url.startswith("https://"):
      errors.append("notification URL must use HTTPS")
    if len(self.api_v3_key) != 32:
      errors.append("API v3 key must contain 32 bytes")
    if not self.public_key_id.startswith("PUB_KEY_ID_"):
      errors.append("WeChat Pay public-key ID is missing")
    for label, path in (
      ("merchant certificate", self.merchant_cert_file),
      ("merchant private key", self.merchant_key_file),
      ("WeChat Pay public key", self.public_key_file),
    ):
      if path is None or not path.is_file():
        errors.append(f"{label} file is missing")
    if errors:
      return errors
    try:
      self._load_keys()
    except Exception as exc:
      return [f"invalid WeChat Pay key material: {exc}"]
    return []

  def require_configured(self) -> None:
    errors = self.configuration_errors()
    if errors:
      raise WechatPayError("wechat_pay_unavailable", "; ".join(errors))

  def _load_keys(self) -> None:
    if self._merchant_private_key and self._wechat_public_key and self._merchant_serial:
      return
    cert = x509.load_pem_x509_certificate(self.merchant_cert_file.read_bytes())
    now = time.time()
    if cert.not_valid_after_utc.timestamp() <= now:
      raise ValueError("merchant certificate is expired")
    merchant_private_key = serialization.load_pem_private_key(
      self.merchant_key_file.read_bytes(), password=None
    )
    wechat_public_key = serialization.load_pem_public_key(self.public_key_file.read_bytes())
    cert_key = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    merchant_key = merchant_private_key.public_key().public_bytes(
      serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if cert_key != merchant_key:
      raise ValueError("merchant certificate and private key do not match")
    if getattr(wechat_public_key, "key_size", 0) != 2048:
      raise ValueError("WeChat Pay public key must be RSA-2048")
    self._merchant_serial = format(cert.serial_number, "X")
    self._merchant_private_key = merchant_private_key
    self._wechat_public_key = wechat_public_key

  def _authorization(self, method: str, canonical_url: str, body: str) -> str:
    self.require_configured()
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    message = f"{method}\n{canonical_url}\n{timestamp}\n{nonce}\n{body}\n".encode("utf-8")
    signature = self._merchant_private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    encoded = base64.b64encode(signature).decode("ascii")
    return (
      'WECHATPAY2-SHA256-RSA2048 '
      f'mchid="{self.merchant_id}",nonce_str="{nonce}",timestamp="{timestamp}",'
      f'serial_no="{self._merchant_serial}",signature="{encoded}"'
    )

  def _verify_signed_message(self, headers, body: bytes, *, check_timestamp: bool) -> None:
    self.require_configured()
    timestamp = str(headers.get("Wechatpay-Timestamp") or "")
    nonce = str(headers.get("Wechatpay-Nonce") or "")
    serial = str(headers.get("Wechatpay-Serial") or "")
    signature = str(headers.get("Wechatpay-Signature") or "")
    if not timestamp or not nonce or not serial or not signature:
      raise WechatPayError("invalid_wechat_signature", "required WeChat Pay signature headers are missing")
    if serial != self.public_key_id:
      raise WechatPayError("invalid_wechat_signature", "unexpected WeChat Pay public-key ID")
    try:
      timestamp_value = int(timestamp)
    except ValueError as exc:
      raise WechatPayError("invalid_wechat_signature", "invalid WeChat Pay timestamp") from exc
    if check_timestamp and abs(int(time.time()) - timestamp_value) > self.timestamp_tolerance_seconds:
      raise WechatPayError("stale_wechat_callback", "WeChat Pay callback timestamp is outside the allowed window")
    message = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n"
    try:
      self._wechat_public_key.verify(
        base64.b64decode(signature, validate=True), message, padding.PKCS1v15(), hashes.SHA256()
      )
    except (InvalidSignature, ValueError) as exc:
      raise WechatPayError("invalid_wechat_signature", "WeChat Pay signature verification failed") from exc

  def _request(self, method: str, canonical_url: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload is not None else ""
    headers = {
      "Accept": "application/json",
      "Content-Type": "application/json",
      "Authorization": self._authorization(method, canonical_url, body),
      "User-Agent": "AI-Audit-Platform/1.0",
    }
    try:
      response = httpx.request(
        method,
        f"{self.API_BASE}{canonical_url}",
        content=body.encode("utf-8") if body else None,
        headers=headers,
        timeout=self.timeout_seconds,
      )
    except httpx.HTTPError as exc:
      raise WechatPayError("wechat_pay_unavailable", str(exc)) from exc
    if response.status_code < 200 or response.status_code >= 300:
      try:
        error = response.json()
        message = str(error.get("message") or error.get("code") or "WeChat Pay request failed")
      except ValueError:
        message = "WeChat Pay request failed"
      raise WechatPayError("wechat_pay_request_failed", message)
    self._verify_signed_message(response.headers, response.content, check_timestamp=False)
    return response.json() if response.content else {}

  def create_native_order(self, out_trade_no: str, amount_fen: int, description: str, expires_at: str) -> dict:
    return self._request(
      "POST",
      "/v3/pay/transactions/native",
      {
        "appid": self.app_id,
        "mchid": self.merchant_id,
        "description": description[:127],
        "out_trade_no": out_trade_no,
        "time_expire": expires_at,
        "notify_url": self.notify_url,
        "amount": {"total": amount_fen, "currency": "CNY"},
      },
    )

  def query_order(self, out_trade_no: str) -> dict:
    canonical = f"/v3/pay/transactions/out-trade-no/{quote(out_trade_no, safe='')}?mchid={quote(self.merchant_id)}"
    return self._request("GET", canonical)

  def close_order(self, out_trade_no: str) -> None:
    canonical = f"/v3/pay/transactions/out-trade-no/{quote(out_trade_no, safe='')}/close"
    self._request("POST", canonical, {"mchid": self.merchant_id})

  def decode_callback(self, headers, body: bytes) -> dict:
    self._verify_signed_message(headers, body, check_timestamp=True)
    try:
      envelope = json.loads(body.decode("utf-8"))
      resource = envelope["resource"]
      if resource.get("algorithm") != "AEAD_AES_256_GCM" or resource.get("original_type") != "transaction":
        raise ValueError("unexpected encrypted resource type")
      plaintext = AESGCM(self.api_v3_key).decrypt(
        str(resource["nonce"]).encode("utf-8"),
        base64.b64decode(resource["ciphertext"]),
        str(resource.get("associated_data") or "").encode("utf-8"),
      )
      transaction = json.loads(plaintext.decode("utf-8"))
    except (KeyError, ValueError, UnicodeError, binascii.Error, InvalidTag) as exc:
      raise WechatPayError("invalid_wechat_callback", "WeChat Pay callback could not be decrypted") from exc
    return {"eventType": str(envelope.get("event_type") or ""), "transaction": transaction}
