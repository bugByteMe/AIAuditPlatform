from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wechat_pay import WechatPayClient, WechatPayError


class WechatPayClientTest(unittest.TestCase):
  def setUp(self) -> None:
    self.tempdir = tempfile.TemporaryDirectory()
    root = Path(self.tempdir.name)
    merchant_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wechat_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test merchant")])
    cert = (
      x509.CertificateBuilder()
      .subject_name(subject)
      .issuer_name(subject)
      .public_key(merchant_key.public_key())
      .serial_number(123456)
      .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
      .not_valid_after(datetime.now(timezone.utc) + timedelta(days=30))
      .sign(merchant_key, hashes.SHA256())
    )
    cert_file = root / "merchant.pem"
    key_file = root / "merchant-key.pem"
    public_file = root / "wechat-public.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
      merchant_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
      )
    )
    public_file.write_bytes(
      wechat_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
      )
    )
    self.wechat_key = wechat_key
    self.api_key = b"0123456789abcdef0123456789abcdef"
    self.client = WechatPayClient(
      enabled=True,
      app_id="wx-test",
      merchant_id="1900000001",
      notify_url="https://audit.example/api/payments/wechat/notify",
      api_v3_key=self.api_key.decode(),
      merchant_cert_file=cert_file,
      merchant_key_file=key_file,
      public_key_id="PUB_KEY_ID_TEST",
      public_key_file=public_file,
    )

  def tearDown(self) -> None:
    self.tempdir.cleanup()

  def callback(self, *, timestamp: int | None = None, tamper: bool = False):
    transaction = {
      "appid": "wx-test",
      "mchid": "1900000001",
      "out_trade_no": "AIA1",
      "transaction_id": "4200000001",
      "trade_state": "SUCCESS",
      "success_time": "2026-09-25T12:00:00+08:00",
      "amount": {"total": 100, "currency": "CNY"},
    }
    nonce_resource = b"0123456789ab"
    associated = b"transaction"
    ciphertext = AESGCM(self.api_key).encrypt(nonce_resource, json.dumps(transaction).encode(), associated)
    body = json.dumps(
      {
        "event_type": "TRANSACTION.SUCCESS",
        "resource": {
          "algorithm": "AEAD_AES_256_GCM",
          "original_type": "transaction",
          "nonce": nonce_resource.decode(),
          "associated_data": associated.decode(),
          "ciphertext": base64.b64encode(ciphertext).decode(),
        },
      },
      separators=(",", ":"),
    ).encode()
    signed_body = body + (b" " if tamper else b"")
    stamp = str(timestamp or int(time.time()))
    nonce = "callback-nonce"
    message = stamp.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n"
    signature = self.wechat_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    headers = {
      "Wechatpay-Timestamp": stamp,
      "Wechatpay-Nonce": nonce,
      "Wechatpay-Serial": "PUB_KEY_ID_TEST",
      "Wechatpay-Signature": base64.b64encode(signature).decode(),
    }
    return headers, signed_body

  def test_valid_callback_is_verified_and_decrypted(self) -> None:
    headers, body = self.callback()
    decoded = self.client.decode_callback(headers, body)
    self.assertEqual(decoded["eventType"], "TRANSACTION.SUCCESS")
    self.assertEqual(decoded["transaction"]["transaction_id"], "4200000001")

  def test_tampered_and_stale_callbacks_are_rejected(self) -> None:
    headers, body = self.callback(tamper=True)
    with self.assertRaisesRegex(WechatPayError, "signature verification"):
      self.client.decode_callback(headers, body)
    headers, body = self.callback(timestamp=int(time.time()) - 301)
    with self.assertRaisesRegex(WechatPayError, "outside the allowed window"):
      self.client.decode_callback(headers, body)

  def test_configuration_requires_https_and_32_byte_api_key(self) -> None:
    self.client.notify_url = "http://audit.example/notify"
    self.client.api_v3_key = b"short"
    errors = self.client.configuration_errors()
    self.assertIn("notification URL must use HTTPS", errors)
    self.assertIn("API v3 key must contain 32 bytes", errors)


if __name__ == "__main__":
  unittest.main()
