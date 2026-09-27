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

class RechargeHandlerMixin:
  def read_recharge_upload(self) -> tuple[bytes, dict[str, str]]:
    content_length = int(self.headers.get("Content-Length") or 0)
    if content_length <= 0:
      raise ValueError("XLSX file is required")
    if content_length > MAX_WORKBOOK_BYTES + 1024 * 1024:
      raise ValueError("XLSX upload exceeds the size limit")
    fields, files = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(content_length))
    if len(files) != 1:
      raise ValueError("upload exactly one XLSX file")
    uploaded = files[0]
    if Path(uploaded.path).suffix.lower() != ".xlsx":
      raise ValueError("only .xlsx files are supported")
    return uploaded.content, fields

  def classify_recharge_import(self, parsed: dict) -> dict:
    seen: set[str] = set()
    classified = []
    for source in parsed["records"]:
      record = dict(source)
      payment_number = str(record.pop("paymentNumber", "") or "")
      if record["status"] != "candidate":
        classified.append(record)
        continue
      key = payment_key(payment_number)
      if key in seen or ACCOUNT_STORE.recharge_payment(key):
        record.update({"status": "duplicate", "reason": "支付单号 has already been used", "paymentKey": key})
      else:
        seen.add(key)
        account = user_by_username(record["username"])
        if not account:
          record.update({"status": "unmatched", "reason": "用户名 does not match an active account", "paymentKey": key})
        elif not (account.get("micu") or {}).get("tokenId"):
          record.update({"status": "invalid", "reason": "account does not have a provisioned MicuAPI token", "paymentKey": key})
        else:
          record.update({"status": "eligible", "reason": "", "paymentKey": key, "userId": account["id"]})
      record["paymentNumber"] = payment_number
      classified.append(record)
    summary: dict[str, int] = {}
    for record in classified:
      summary[record["status"]] = summary.get(record["status"], 0) + 1
    return {**parsed, "records": classified, "summary": summary}

  def public_recharge_import(result: dict) -> dict:
    records = []
    for source in result["records"]:
      record = {key: value for key, value in source.items() if key not in {"paymentNumber", "paymentKey", "userId"}}
      records.append(record)
    return {**{key: value for key, value in result.items() if key != "records"}, "records": records}

  def preview_recharge_import(self) -> None:
    self.require_admin()
    content, _ = self.read_recharge_upload()
    result = self.classify_recharge_import(parse_recharge_workbook(content))
    self.write_json(self.public_recharge_import(result))

  def apply_recharge_import(self) -> None:
    actor = self.require_admin()
    content, fields = self.read_recharge_upload()
    parsed = parse_recharge_workbook(content)
    if not fields.get("previewDigest") or not secrets.compare_digest(fields["previewDigest"], parsed["digest"]):
      raise ValueError("uploaded workbook does not match the preview")
    result = self.classify_recharge_import(parsed)
    batch_id = f"rchb_{secrets.token_urlsafe(9)}"
    imported_at = time.strftime("%Y-%m-%d %H:%M:%S")
    for record in result["records"]:
      if record["status"] != "eligible":
        continue
      account = ACCOUNT_STORE.user_by_identifier(record["userId"])
      if not account:
        record.update({"status": "unmatched", "reason": "account no longer exists"})
        continue
      reservation = {
        "userId": account["id"],
        "paidAt": record["paidAt"],
        "amountCny": record["amountCny"],
        "creditCny": record["creditCny"],
        "importedAt": imported_at,
        "batchId": batch_id,
        "sheet": record["sheet"],
        "row": record["row"],
        "paymentRef": record["paymentRef"],
      }
      reserved, _ = ACCOUNT_STORE.reserve_recharge_payment(record["paymentKey"], reservation)
      if not reserved:
        record.update({"status": "duplicate", "reason": "支付单号 has already been used"})
        continue
      try:
        balance = MICU_CLIENT.add_balance(account["micu"], record["creditCny"])
        ACCOUNT_STORE.finish_recharge_payment(
          record["paymentKey"],
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
        record.update({"status": "applied", "reason": ""})
      except Exception as exc:
        ACCOUNT_STORE.finish_recharge_payment(record["paymentKey"], status="review_required", reason=str(exc))
        record.update({"status": "review_required", "reason": str(exc)})
    summary: dict[str, int] = {}
    for record in result["records"]:
      summary[record["status"]] = summary.get(record["status"], 0) + 1
    result.update({"batchId": batch_id, "summary": summary})
    add_audit(
      actor["username"],
      "recharge workbook imported",
      f"{batch_id} applied={summary.get('applied', 0)} duplicate={summary.get('duplicate', 0)} review={summary.get('review_required', 0)} skipped={len(result['records']) - summary.get('applied', 0) - summary.get('duplicate', 0) - summary.get('review_required', 0)}",
    )
    self.write_json(self.public_recharge_import(result))

  def recharge_info(self) -> None:
    user = self.require_user()
    binding = user.get("micu") or {}
    api_key = str(binding.get("apiKey") or "").strip()
    if not api_key:
      raise StorageError("codex_auth_required", "MicuAPI account is not provisioned")
    add_audit(user["username"], "MicuAPI credentials viewed", str(user.get("id") or ""))
    self.write_json(
      {
        "username": user["username"],
        "baseUrl": SETTINGS.micu_inference_url,
        "apiKey": api_key,
        "paymentReady": WECHAT_PAY.configured,
        "paymentError": "" if WECHAT_PAY.configured or not WECHAT_PAY.enabled else "; ".join(WECHAT_PAY.configuration_errors()),
        "products": [
          {"amountCny": amount, "creditCny": product["creditCny"], "debug": product["debug"]}
          for amount, product in RECHARGE_PRODUCTS.items()
        ],
        "adjustments": ACCOUNT_STORE.recharge_history(str(user.get("id") or "")),
      }
    )

  def create_recharge_order(self) -> None:
    user = self.require_user()
    WECHAT_PAY.require_configured()
    if not (user.get("micu") or {}).get("tokenId"):
      raise WechatPayError("wechat_pay_unavailable", "MicuAPI account is not provisioned")
    payload = self.read_json()
    try:
      amount = Decimal(str(payload.get("amountCny") or "")).quantize(Decimal("0.01"))
    except Exception as exc:
      raise ValueError("a supported recharge amount is required") from exc
    amount_cny = f"{amount:.2f}"
    product = RECHARGE_PRODUCTS.get(amount_cny)
    if not product:
      raise ValueError("unsupported recharge amount")
    now = time.time()
    expires = now + SETTINGS.wechat_order_expiry_seconds
    order_id = f"wpo_{secrets.token_urlsafe(12)}"
    out_trade_no = f"AIA{int(now)}{secrets.token_hex(8)}"
    order = ACCOUNT_STORE.create_recharge_order(
      {
        "id": order_id,
        "outTradeNo": out_trade_no,
        "userId": user["id"],
        "amountCny": amount_cny,
        "amountFen": product["amountFen"],
        "creditCny": product["creditCny"],
        "debug": product["debug"],
        "status": "creating",
        "createdAt": utc_timestamp(now),
        "createdAtEpoch": now,
        "expiresAt": utc_timestamp(expires),
        "expiresAtEpoch": expires,
        "reason": "",
      }
    )
    try:
      result = WECHAT_PAY.create_native_order(
        out_trade_no,
        int(product["amountFen"]),
        f"AI Audit recharge CNY {amount_cny}",
        order["expiresAt"],
      )
      code_url = str(result.get("code_url") or "").strip()
      if not code_url.startswith("weixin://"):
        raise WechatPayError("wechat_pay_request_failed", "WeChat Pay did not return a valid Native Pay code URL")
      order = ACCOUNT_STORE.update_recharge_order(order_id, status="pending", codeUrl=code_url)
    except Exception as exc:
      ACCOUNT_STORE.update_recharge_order(order_id, status="failed", reason=str(exc))
      raise
    add_audit(user["username"], "WeChat recharge order created", f"order={order_id} amountCny={amount_cny}")
    self.write_json({"order": public_recharge_order(order)}, HTTPStatus.CREATED)

  def recharge_order_api(self, method: str, path: str) -> None:
    user = self.require_user()
    suffix = path.removeprefix("/api/recharge/orders/")
    wants_qr = suffix.endswith("/qr")
    order_id = unquote(suffix[:-3] if wants_qr else suffix).strip("/")
    order = ACCOUNT_STORE.recharge_order(order_id)
    if not order:
      raise StorageError("not_found", "recharge order was not found")
    if str(order.get("userId") or "") != str(user.get("id") or ""):
      raise StorageError("forbidden", "recharge order does not belong to this account")
    if method != "GET":
      raise StorageError("not_found", "recharge endpoint was not found")
    if wants_qr:
      code_url = str(order.get("codeUrl") or "")
      if not code_url:
        raise StorageError("not_found", "recharge QR code is unavailable")
      import qrcode
      image = qrcode.make(code_url)
      output = io.BytesIO()
      image.save(output, format="PNG")
      self.write_binary(output.getvalue(), "image/png", headers={"X-Content-Type-Options": "nosniff"}, no_store=True)
      return
    if order.get("status") in {"creating", "pending", "paid"}:
      last_checked = float(order.get("lastCheckedAtEpoch") or 0)
      if time.time() - last_checked >= 5:
        order = reconcile_wechat_order(order)
    self.write_json({"order": public_recharge_order(order)})

  def wechat_payment_notify(self) -> None:
    content_length = int(self.headers.get("Content-Length") or 0)
    if content_length <= 0 or content_length > WECHAT_CALLBACK_MAX_BYTES:
      raise WechatPayError("invalid_wechat_callback", "invalid WeChat Pay callback size")
    body = self.rfile.read(content_length)
    decoded = WECHAT_PAY.decode_callback(self.headers, body)
    if decoded["eventType"] != "TRANSACTION.SUCCESS":
      raise WechatPayError("invalid_wechat_callback", "unexpected WeChat Pay callback event")
    transaction = decoded["transaction"]
    order = ACCOUNT_STORE.recharge_order(str(transaction.get("out_trade_no") or ""))
    if not order:
      raise WechatPayError("unknown_wechat_order", "recharge order was not found")
    apply_wechat_transaction(order, transaction)
    self.write_binary(b"", "application/json; charset=utf-8", HTTPStatus.NO_CONTENT, no_store=True)

