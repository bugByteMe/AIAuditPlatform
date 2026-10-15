from __future__ import annotations

import hmac
import os
import re
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from wechat_callback_inbox import CallbackInbox
from wechat_callback_verifier import WechatCallbackError, WechatCallbackVerifier


MAX_CALLBACK_BYTES = 64 * 1024
EVENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def create_app(verifier: WechatCallbackVerifier, inbox: CallbackInbox, app_id: str, merchant_id: str, token: str) -> FastAPI:
  if not app_id or not merchant_id or len(token) < 32:
    raise ValueError("callback AppID, merchant ID, and 32-character internal token are required")
  app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

  def require_internal(request: Request) -> None:
    authorization = request.headers.get("Authorization", "")
    if not hmac.compare_digest(authorization, f"Bearer {token}"):
      raise HTTPException(status_code=401, detail="unauthorized")

  @app.post("/api/payments/wechat/notify")
  async def notify(request: Request):
    try:
      content_length = int(request.headers.get("Content-Length", "0"))
    except ValueError:
      content_length = 0
    if content_length > MAX_CALLBACK_BYTES:
      raise HTTPException(status_code=413, detail="callback exceeds size limit")
    body = bytearray()
    async for chunk in request.stream():
      body.extend(chunk)
      if len(body) > MAX_CALLBACK_BYTES:
        raise HTTPException(status_code=413, detail="callback exceeds size limit")
    if not body:
      raise HTTPException(status_code=400, detail="callback is empty")
    try:
      decoded = verifier.decode(request.headers, bytes(body))
    except WechatCallbackError as exc:
      raise HTTPException(status_code=400, detail=exc.code) from exc
    transaction = decoded["transaction"]
    notification_id = decoded["id"]
    out_trade_no = transaction.get("out_trade_no")
    if (
      not EVENT_ID.fullmatch(notification_id)
      or decoded["eventType"] != "TRANSACTION.SUCCESS"
      or transaction.get("trade_state") != "SUCCESS"
      or transaction.get("appid") != app_id
      or transaction.get("mchid") != merchant_id
      or not isinstance(out_trade_no, str) or not out_trade_no.strip() or len(out_trade_no) > 255
      or not isinstance(transaction.get("transaction_id"), str) or not transaction["transaction_id"].strip()
    ):
      raise HTTPException(status_code=400, detail="unexpected transaction")
    try:
      inbox.record(notification_id, out_trade_no)
    except sqlite3.Error as exc:
      raise HTTPException(status_code=503, detail="callback inbox unavailable") from exc
    return Response(status_code=204)

  @app.get("/internal/receipts")
  def pending(request: Request, limit: int = 100):
    require_internal(request)
    if not 1 <= limit <= 100:
      raise HTTPException(status_code=400, detail="invalid limit")
    try:
      return {"receipts": inbox.pending(limit)}
    except sqlite3.Error as exc:
      raise HTTPException(status_code=503, detail="callback inbox unavailable") from exc

  @app.post("/internal/receipts/{notification_id}/ack")
  def acknowledge(notification_id: str, request: Request):
    require_internal(request)
    if not EVENT_ID.fullmatch(notification_id):
      raise HTTPException(status_code=400, detail="invalid notification ID")
    try:
      inbox.acknowledge(notification_id)
    except sqlite3.Error as exc:
      raise HTTPException(status_code=503, detail="callback inbox unavailable") from exc
    return Response(status_code=204)

  @app.post("/internal/receipts/{notification_id}/defer")
  def defer(notification_id: str, request: Request):
    require_internal(request)
    if not EVENT_ID.fullmatch(notification_id):
      raise HTTPException(status_code=400, detail="invalid notification ID")
    try:
      inbox.defer(notification_id, "payment not yet confirmed")
    except sqlite3.Error as exc:
      raise HTTPException(status_code=503, detail="callback inbox unavailable") from exc
    return Response(status_code=204)

  @app.get("/health")
  def health():
    try:
      return JSONResponse(inbox.health_stats())
    except sqlite3.Error:
      return JSONResponse({"status": "unavailable"}, status_code=503)

  return app


def app_from_environment() -> FastAPI:
  key = os.environ.get("AI_AUDIT_WECHAT_API_V3_KEY", "").encode("utf-8")
  key_id = os.environ.get("AI_AUDIT_WECHAT_PUBLIC_KEY_ID", "").strip()
  public_file = Path(os.environ.get("AI_AUDIT_WECHAT_PUBLIC_KEY_FILE", ""))
  if len(key) != 32 or not key_id.startswith("PUB_KEY_ID_") or not public_file.is_file():
    raise ValueError("callback API v3 key, public-key ID and public-key file are required")
  public_key = serialization.load_pem_public_key(public_file.read_bytes())
  if getattr(public_key, "key_size", 0) != 2048:
    raise ValueError("callback public key must be RSA-2048")
  inbox_path = os.environ.get("AI_AUDIT_WECHAT_CALLBACK_DB_PATH", "").strip()
  if not inbox_path:
    raise ValueError("callback inbox path is required")
  return create_app(
    WechatCallbackVerifier(public_key, key_id, key),
    CallbackInbox(Path(inbox_path)),
    os.environ.get("AI_AUDIT_WECHAT_APP_ID", "").strip(),
    os.environ.get("AI_AUDIT_WECHAT_MERCHANT_ID", "").strip(),
    os.environ.get("AI_AUDIT_WECHAT_RECEIPT_TOKEN", "").strip(),
  )
