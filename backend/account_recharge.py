from __future__ import annotations

import secrets
from copy import deepcopy

from account_common import timestamp


class AccountRechargeMixin:
  def recharge_payment(self, key: str) -> dict | None:
    with self.lock:
      payment = self.recharge_payments.get(key)
      return deepcopy(payment) if payment else None

  def create_recharge_order(self, order: dict) -> dict:
    with self.lock:
      order_id = str(order.get("id") or "")
      out_trade_no = str(order.get("outTradeNo") or "")
      if not order_id or not out_trade_no:
        raise ValueError("recharge order id and merchant order number are required")
      if order_id in self.recharge_orders or any(item.get("outTradeNo") == out_trade_no for item in self.recharge_orders.values()):
        raise ValueError("recharge order already exists")
      self.recharge_orders[order_id] = deepcopy(order)
      try:
        self.database.save_order(self.recharge_orders[order_id])
      except Exception:
        self.recharge_orders.pop(order_id, None)
        raise
      return deepcopy(self.recharge_orders[order_id])

  def recharge_order(self, identifier: str) -> dict | None:
    with self.lock:
      order = self.recharge_orders.get(identifier)
      if not order:
        order = next((item for item in self.recharge_orders.values() if item.get("outTradeNo") == identifier), None)
      return deepcopy(order) if order else None

  def update_recharge_order(self, order_id: str, **updates) -> dict:
    with self.lock:
      order = self.recharge_orders.get(order_id)
      if not order:
        raise ValueError("recharge order was not found")
      previous = deepcopy(order)
      order.update(deepcopy(updates))
      try:
        self.database.save_order(order)
      except Exception:
        self.recharge_orders[order_id] = previous
        raise
      return deepcopy(order)

  def transition_recharge_order(self, order_id: str, allowed_statuses: set[str], **updates) -> dict:
    with self.lock:
      order = self.recharge_orders.get(order_id)
      if not order:
        raise ValueError("recharge order was not found")
      if str(order.get("status") or "") not in allowed_statuses:
        return deepcopy(order)
      previous = deepcopy(order)
      order.update(deepcopy(updates))
      try:
        self.database.save_order(order)
      except Exception:
        self.recharge_orders[order_id] = previous
        raise
      return deepcopy(order)

  def pending_recharge_orders(self) -> list[dict]:
    with self.lock:
      return [
        deepcopy(order)
        for order in self.recharge_orders.values()
        if order.get("status") in {"creating", "pending", "paid"}
      ]

  def reserve_recharge_payment(self, key: str, payment: dict) -> tuple[bool, dict]:
    with self.lock:
      existing = self.recharge_payments.get(key)
      if existing:
        return False, deepcopy(existing)
      reserved = {**deepcopy(payment), "id": f"rch_{key[:16]}", "status": "processing", "reason": ""}
      self.recharge_payments[key] = reserved
      try:
        self.database.save_payment(key, reserved)
      except Exception:
        self.recharge_payments.pop(key, None)
        raise
      return True, deepcopy(reserved)

  def finish_recharge_payment(self, key: str, *, status: str, reason: str = "", binding_updates: dict | None = None) -> dict:
    with self.lock:
      payment = self.recharge_payments.get(key)
      if not payment:
        raise ValueError("recharge payment reservation was not found")
      previous_payment = deepcopy(payment)
      user = self.user_by_identifier(str(payment.get("userId") or ""))
      previous_binding = deepcopy((user or {}).get("micu") or {})
      payment.update({"status": status, "reason": reason, "completedAt": timestamp()})
      if binding_updates:
        if not user:
          raise ValueError("recharge account was not found")
        user.setdefault("micu", {}).update(deepcopy(binding_updates))
      try:
        self.database.save_payment_and_user(key, payment, user if binding_updates else None)
      except Exception:
        self.recharge_payments[key] = previous_payment
        if user is not None:
          user["micu"] = previous_binding
        raise
      return deepcopy(payment)

  def recharge_history(self, user_id: str) -> list[dict]:
    with self.lock:
      rows = [
        {
          "id": payment["id"],
          "paidAt": payment["paidAt"],
          "amountCny": payment["amountCny"],
          "importedAt": payment.get("importedAt") or payment.get("recordedAt") or payment.get("completedAt") or "",
        }
        for payment in self.recharge_payments.values()
        if payment.get("status") == "applied" and str(payment.get("userId") or "") == user_id
      ]
      return sorted(rows, key=lambda item: (item["paidAt"], item["importedAt"], item["id"]), reverse=True)

  def _tombstone_recharge_payments(self, user_ids: set[str]) -> None:
    for payment in self.recharge_payments.values():
      if str(payment.get("userId") or "") not in user_ids:
        continue
      payment.clear()
      payment.update({"id": f"rch_deleted_{secrets.token_hex(6)}", "status": "used", "deletedAt": timestamp()})

  def _tombstone_recharge_orders(self, user_ids: set[str]) -> None:
    for order in self.recharge_orders.values():
      if str(order.get("userId") or "") not in user_ids:
        continue
      retained = {
        "id": order.get("id"),
        "outTradeNo": order.get("outTradeNo"),
        "status": "deleted",
        "deletedAt": timestamp(),
      }
      order.clear()
      order.update(retained)
