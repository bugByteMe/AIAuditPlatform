from __future__ import annotations

import base64
import binascii
import json
import time

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class WechatCallbackError(RuntimeError):
  def __init__(self, code: str, message: str):
    super().__init__(message)
    self.code = code


class WechatCallbackVerifier:
  def __init__(self, public_key, public_key_id: str, api_v3_key: bytes, timestamp_tolerance_seconds: int = 300):
    self.public_key = public_key
    self.public_key_id = public_key_id
    self.api_v3_key = api_v3_key
    self.timestamp_tolerance_seconds = timestamp_tolerance_seconds

  def verify(self, headers, body: bytes, *, check_timestamp: bool = True) -> None:
    timestamp = str(headers.get("Wechatpay-Timestamp") or "")
    nonce = str(headers.get("Wechatpay-Nonce") or "")
    serial = str(headers.get("Wechatpay-Serial") or "")
    signature = str(headers.get("Wechatpay-Signature") or "")
    if not timestamp or not nonce or not serial or not signature:
      raise WechatCallbackError("invalid_wechat_signature", "required WeChat Pay signature headers are missing")
    if serial != self.public_key_id:
      raise WechatCallbackError("invalid_wechat_signature", "unexpected WeChat Pay public-key ID")
    try:
      timestamp_value = int(timestamp)
    except ValueError as exc:
      raise WechatCallbackError("invalid_wechat_signature", "invalid WeChat Pay timestamp") from exc
    if check_timestamp and abs(int(time.time()) - timestamp_value) > self.timestamp_tolerance_seconds:
      raise WechatCallbackError("stale_wechat_callback", "WeChat Pay callback timestamp is outside the allowed window")
    message = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n"
    try:
      self.public_key.verify(base64.b64decode(signature, validate=True), message, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError, binascii.Error) as exc:
      raise WechatCallbackError("invalid_wechat_signature", "WeChat Pay signature verification failed") from exc

  def decode(self, headers, body: bytes) -> dict:
    self.verify(headers, body)
    try:
      envelope = json.loads(body.decode("utf-8"))
      if not isinstance(envelope, dict) or not isinstance(envelope.get("id"), str):
        raise ValueError("invalid notification envelope")
      resource = envelope["resource"]
      if not isinstance(resource, dict):
        raise ValueError("invalid encrypted resource")
      if resource.get("algorithm") != "AEAD_AES_256_GCM" or resource.get("original_type") != "transaction":
        raise ValueError("unexpected encrypted resource type")
      plaintext = AESGCM(self.api_v3_key).decrypt(
        str(resource["nonce"]).encode("utf-8"),
        base64.b64decode(resource["ciphertext"], validate=True),
        str(resource.get("associated_data") or "").encode("utf-8"),
      )
      transaction = json.loads(plaintext.decode("utf-8"))
      if not isinstance(transaction, dict):
        raise ValueError("invalid transaction")
      return {"id": envelope["id"], "eventType": str(envelope["event_type"]), "transaction": transaction}
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error, InvalidTag) as exc:
      raise WechatCallbackError("invalid_wechat_callback", "WeChat Pay callback could not be decrypted") from exc
