from __future__ import annotations

import time
from decimal import Decimal
from urllib.parse import quote

from micu_api import MicuApiError, parse_cny
from recharge_import import payment_key
from server_state import ACCOUNT_STORE, MICU_CLIENT, WECHAT_PAY, add_audit, user_by_identifier
from server_utils import utc_timestamp
from wechat_pay import WechatPayError
from workspace_store import StorageError

def public_recharge_order(order: dict) -> dict:
  return {
    "id": order.get("id"),
    "amountCny": order.get("amountCny"),
    "creditCny": order.get("creditCny"),
    "status": order.get("status"),
    "createdAt": order.get("createdAt"),
    "expiresAt": order.get("expiresAt"),
    "reason": "manual_review_required" if order.get("status") == "review_required" else "",
    "qrCodeUrl": f"/api/recharge/orders/{quote(str(order.get('id') or ''))}/qr" if order.get("codeUrl") else "",
  }

def validate_paid_transaction(order: dict, transaction: dict) -> None:
  amount = transaction.get("amount") or {}
  try:
    paid_fen = int(amount.get("total"))
  except (TypeError, ValueError) as exc:
    raise WechatPayError("invalid_wechat_transaction", "transaction amount is invalid") from exc
  checks = (
    (transaction.get("trade_state") == "SUCCESS", "transaction is not successful"),
    (str(transaction.get("appid") or "") == WECHAT_PAY.app_id, "AppID does not match"),
    (str(transaction.get("mchid") or "") == WECHAT_PAY.merchant_id, "merchant ID does not match"),
    (str(transaction.get("out_trade_no") or "") == str(order.get("outTradeNo") or ""), "merchant order number does not match"),
    (str(amount.get("currency") or "") == "CNY", "transaction currency does not match"),
    (paid_fen == int(order.get("amountFen") or 0), "transaction amount does not match"),
    (bool(str(transaction.get("transaction_id") or "").strip()), "transaction ID is missing"),
  )
  for valid, message in checks:
    if not valid:
      raise WechatPayError("invalid_wechat_transaction", message)

def apply_wechat_transaction(order: dict, transaction: dict) -> dict:
  validate_paid_transaction(order, transaction)
  current = ACCOUNT_STORE.recharge_order(str(order["id"]))
  if not current:
    raise WechatPayError("unknown_wechat_order", "recharge order was not found")
  if current.get("status") == "applied":
    return current
  transaction_id = str(transaction["transaction_id"])
  key = payment_key(transaction_id)
  existing = ACCOUNT_STORE.recharge_payment(key)
  if existing:
    if str(existing.get("userId") or "") != str(current.get("userId") or "") or str(existing.get("amountCny") or "") != str(current.get("amountCny") or ""):
      return ACCOUNT_STORE.update_recharge_order(current["id"], status="review_required", reason="transaction was already associated with different recharge data")
    mapped_status = {"applied": "applied", "processing": "crediting"}.get(str(existing.get("status")), "review_required")
    if mapped_status == "crediting":
      return ACCOUNT_STORE.transition_recharge_order(current["id"], {"creating", "pending", "paid", "crediting"}, status="crediting")
    return ACCOUNT_STORE.update_recharge_order(current["id"], status=mapped_status, reason=str(existing.get("reason") or ""))

  paid_at = str(transaction.get("success_time") or utc_timestamp(time.time()))
  ACCOUNT_STORE.transition_recharge_order(
    current["id"], {"creating", "pending", "failed", "expired", "closed"}, status="paid", paidAt=paid_at, reason=""
  )
  reservation = {
    "userId": current["userId"],
    "paidAt": paid_at,
    "amountCny": current["amountCny"],
    "creditCny": current["creditCny"],
    "recordedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    "source": "wechat_native",
    "orderId": current["id"],
    "paymentRef": f"***{transaction_id[-4:]}",
  }
  reserved, existing = ACCOUNT_STORE.reserve_recharge_payment(key, reservation)
  if not reserved:
    mapped_status = {"applied": "applied", "processing": "crediting"}.get(str(existing.get("status")), "review_required")
    if mapped_status == "crediting":
      return ACCOUNT_STORE.transition_recharge_order(current["id"], {"creating", "pending", "paid", "crediting"}, status="crediting")
    return ACCOUNT_STORE.update_recharge_order(current["id"], status=mapped_status, reason=str(existing.get("reason") or ""))
  ACCOUNT_STORE.transition_recharge_order(current["id"], {"paid"}, status="crediting", paymentKey=key)
  account = ACCOUNT_STORE.user_by_identifier(str(current["userId"]))
  if not account or not (account.get("micu") or {}).get("tokenId"):
    reason = "recharge account or MicuAPI binding is unavailable"
    ACCOUNT_STORE.finish_recharge_payment(key, status="review_required", reason=reason)
    add_audit("wechat-pay", "recharge requires review", f"order={current['id']} reason=account_unavailable")
    return ACCOUNT_STORE.update_recharge_order(current["id"], status="review_required", reason=reason)
  try:
    balance = MICU_CLIENT.add_balance(account["micu"], current["creditCny"])
    ACCOUNT_STORE.finish_recharge_payment(
      key,
      status="applied",
      binding_updates={
        "lastBalanceCny": balance["remainingCny"],
        "lastRemainingPercent": balance["remainingPercent"],
        "rechargeBaselineQuota": balance["rawQuota"],
        "lastSyncedAt": int(time.time()),
        "status": balance["status"],
        "lastError": "",
      },
    )
    applied = ACCOUNT_STORE.update_recharge_order(current["id"], status="applied", reason="", appliedAt=utc_timestamp(time.time()))
    add_audit(str(account.get("username") or "wechat-pay"), "WeChat recharge applied", f"order={current['id']} amountCny={current['amountCny']} creditCny={current['creditCny']}")
    return applied
  except Exception as exc:
    reason = str(exc)
    ACCOUNT_STORE.finish_recharge_payment(key, status="review_required", reason=reason)
    reviewed = ACCOUNT_STORE.update_recharge_order(current["id"], status="review_required", reason=reason)
    add_audit("wechat-pay", "recharge requires review", f"order={current['id']} provider_result_uncertain")
    return reviewed

def reconcile_wechat_order(order: dict) -> dict:
  current = ACCOUNT_STORE.recharge_order(str(order.get("id") or ""))
  if not current or current.get("status") not in {"creating", "pending", "paid"}:
    return current or order
  now = time.time()
  try:
    transaction = WECHAT_PAY.query_order(str(current["outTradeNo"]))
    ACCOUNT_STORE.update_recharge_order(current["id"], lastCheckedAtEpoch=now)
    trade_state = str(transaction.get("trade_state") or "")
    if trade_state == "SUCCESS":
      return apply_wechat_transaction(current, transaction)
    if trade_state in {"CLOSED", "REVOKED", "PAYERROR"}:
      return ACCOUNT_STORE.update_recharge_order(current["id"], status="closed", reason="")
    if now >= float(current.get("expiresAtEpoch") or 0):
      try:
        WECHAT_PAY.close_order(str(current["outTradeNo"]))
      except WechatPayError:
        pass
      return ACCOUNT_STORE.update_recharge_order(current["id"], status="expired", reason="")
  except WechatPayError:
    ACCOUNT_STORE.update_recharge_order(current["id"], lastCheckedAtEpoch=now)
  return ACCOUNT_STORE.recharge_order(str(current["id"])) or current

def reconcile_wechat_orders() -> None:
  if not WECHAT_PAY.configured:
    return
  while not WECHAT_RECONCILE_STOP.is_set():
    for order in ACCOUNT_STORE.pending_recharge_orders():
      reconcile_wechat_order(order)
    WECHAT_RECONCILE_STOP.wait(max(5.0, SETTINGS.wechat_reconcile_interval_seconds))
