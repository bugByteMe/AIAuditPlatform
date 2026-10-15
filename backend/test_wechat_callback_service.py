from __future__ import annotations

import base64
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wechat_callback_inbox import CallbackInbox
from wechat_callback_service import create_app
from wechat_callback_verifier import WechatCallbackError, WechatCallbackVerifier
from server_recharge import consume_wechat_receipts


TOKEN = "test-internal-token-only-32-characters-long"


class CallbackServiceTest(unittest.TestCase):
  def setUp(self) -> None:
    self.tempdir = tempfile.TemporaryDirectory()
    self.path = Path(self.tempdir.name) / "receipts.sqlite3"
    self.inbox = CallbackInbox(self.path)
    self.verifier = MagicMock()
    self.verifier.decode.return_value = {
      "id": "notification-1", "eventType": "TRANSACTION.SUCCESS",
      "transaction": {
        "appid": "wx-test", "mchid": "merchant-test", "trade_state": "SUCCESS",
        "out_trade_no": "order-1", "transaction_id": "full-payment-id-should-not-be-stored",
      },
    }
    self.client = TestClient(create_app(self.verifier, self.inbox, "wx-test", "merchant-test", TOKEN))

  def tearDown(self) -> None:
    self.client.close()
    self.tempdir.cleanup()

  def test_valid_duplicate_and_restart_keep_only_minimal_receipt(self) -> None:
    for _ in range(2):
      self.assertEqual(self.client.post("/api/payments/wechat/notify", content=b"signed body").status_code, 204)
    self.assertEqual(self.client.get("/internal/receipts").status_code, 401)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    self.assertEqual(len(self.client.get("/internal/receipts", headers=headers).json()["receipts"]), 1)
    self.assertEqual(self.client.get("/health").json()["pendingCount"], 1)
    self.assertNotIn(b"full-payment-id", self.path.read_bytes())
    self.assertEqual(CallbackInbox(self.path).pending(100)[0]["outTradeNo"], "order-1")
    self.assertEqual(self.client.post("/internal/receipts/notification-1/ack", headers=headers).status_code, 204)
    self.assertEqual(CallbackInbox(self.path).pending(100), [])
    self.assertEqual(self.client.get("/health").json()["pendingCount"], 0)

  def test_bad_signature_and_unavailable_inbox_are_not_acknowledged(self) -> None:
    self.verifier.decode.side_effect = WechatCallbackError("invalid_wechat_signature", "signature failed")
    self.assertEqual(self.client.post("/api/payments/wechat/notify", content=b"bad").status_code, 400)
    self.verifier.decode.side_effect = WechatCallbackError("stale_wechat_callback", "expired")
    self.assertEqual(self.client.post("/api/payments/wechat/notify", content=b"old").status_code, 400)
    self.verifier.decode.side_effect = None
    with patch.object(self.inbox, "record", side_effect=sqlite3.OperationalError("disk full")):
      self.assertEqual(self.client.post("/api/payments/wechat/notify", content=b"valid").status_code, 503)
    self.assertEqual(self.inbox.pending(100), [])

  def test_invalid_merchant_or_event_is_rejected_and_large_body_is_bounded(self) -> None:
    transaction = self.verifier.decode.return_value["transaction"]
    transaction["mchid"] = "other-merchant"
    self.assertEqual(self.client.post("/api/payments/wechat/notify", content=b"signed").status_code, 400)
    transaction["mchid"] = "merchant-test"
    self.verifier.decode.return_value["eventType"] = "TRANSACTION.CLOSED"
    self.assertEqual(self.client.post("/api/payments/wechat/notify", content=b"signed").status_code, 400)
    self.assertEqual(self.client.post("/api/payments/wechat/notify", content=b"x" * (64 * 1024 + 1)).status_code, 413)
    self.assertEqual(self.inbox.pending(100), [])

  def test_deferred_receipt_returns_after_retry_window(self) -> None:
    self.inbox.record("notification-1", "order-1")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    self.assertEqual(self.client.post("/internal/receipts/notification-1/defer", headers=headers).status_code, 204)
    self.assertEqual(self.inbox.pending(100), [])

  def test_real_signed_notification_reaches_inbox_without_merchant_private_key(self) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    api_key = b"0123456789abcdef0123456789abcdef"
    verifier = WechatCallbackVerifier(private_key.public_key(), "PUB_KEY_ID_TEST", api_key)
    transaction = {
      "appid": "wx-test", "mchid": "merchant-test", "out_trade_no": "order-verified",
      "trade_state": "SUCCESS", "transaction_id": "payment-not-saved",
    }
    nonce = b"0123456789ab"
    body = json.dumps({
      "id": "verified-1", "event_type": "TRANSACTION.SUCCESS",
      "resource": {
        "algorithm": "AEAD_AES_256_GCM", "original_type": "transaction",
        "nonce": nonce.decode(), "associated_data": "transaction",
        "ciphertext": base64.b64encode(AESGCM(api_key).encrypt(nonce, json.dumps(transaction).encode(), b"transaction")).decode(),
      },
    }, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signed = timestamp.encode() + b"\nnonce\n" + body + b"\n"
    headers = {
      "Wechatpay-Timestamp": timestamp, "Wechatpay-Nonce": "nonce", "Wechatpay-Serial": "PUB_KEY_ID_TEST",
      "Wechatpay-Signature": base64.b64encode(private_key.sign(signed, padding.PKCS1v15(), hashes.SHA256())).decode(),
    }
    with TestClient(create_app(verifier, self.inbox, "wx-test", "merchant-test", TOKEN)) as client:
      self.assertEqual(client.post("/api/payments/wechat/notify", content=body, headers=headers).status_code, 204)
      self.assertEqual(client.post("/api/payments/wechat/notify", content=body + b" ", headers=headers).status_code, 400)
    self.assertEqual(CallbackInbox(self.path).pending(100)[0]["outTradeNo"], "order-verified")


class ReceiptConsumerTest(unittest.TestCase):
  def test_queries_wechat_before_crediting_and_then_acknowledges(self) -> None:
    settings = MagicMock(wechat_callback_internal_url="http://receiver:8001", wechat_receipt_token=TOKEN)
    order = {"id": "internal-order", "outTradeNo": "merchant-order", "status": "pending"}
    store = MagicMock()
    store.recharge_order.return_value = order
    wechat = MagicMock()
    wechat.query_order.return_value = {"trade_state": "SUCCESS"}
    client = MagicMock()
    client.get.return_value.json.return_value = {
      "receipts": [{"id": "notice-1", "outTradeNo": "merchant-order", "receivedAt": time.time(), "attempts": 0}]
    }
    client.__enter__.return_value = client
    with patch("server_recharge.SETTINGS", settings), patch("server_recharge.ACCOUNT_STORE", store), \
         patch("server_recharge.WECHAT_PAY", wechat), patch("server_recharge.httpx.Client", return_value=client), \
         patch("server_recharge.apply_wechat_transaction", return_value={"status": "applied"}) as apply:
      consume_wechat_receipts()
    wechat.query_order.assert_called_once_with("merchant-order")
    apply.assert_called_once_with(order, wechat.query_order.return_value)
    self.assertIn("/ack", client.post.call_args.args[0])

  def test_unavailable_order_is_deferred_not_credited(self) -> None:
    settings = MagicMock(wechat_callback_internal_url="http://receiver:8001", wechat_receipt_token=TOKEN)
    store = MagicMock()
    store.recharge_order.return_value = None
    client = MagicMock()
    client.get.return_value.json.return_value = {
      "receipts": [{"id": "notice-1", "outTradeNo": "unknown", "receivedAt": time.time(), "attempts": 0}]
    }
    client.__enter__.return_value = client
    with patch("server_recharge.SETTINGS", settings), patch("server_recharge.ACCOUNT_STORE", store), \
         patch("server_recharge.httpx.Client", return_value=client), \
         patch("server_recharge.apply_wechat_transaction") as apply:
      consume_wechat_receipts()
    apply.assert_not_called()
    self.assertIn("/defer", client.post.call_args.args[0])


if __name__ == "__main__":
  unittest.main()
