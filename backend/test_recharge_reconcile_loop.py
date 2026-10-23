import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server_recharge
import server_state


class RechargeReconcileLoopTest(unittest.TestCase):
  def test_loop_uses_shared_shutdown_event(self):
    self.assertIs(server_recharge.WECHAT_RECONCILE_STOP, server_state.WECHAT_RECONCILE_STOP)
    with patch.object(server_recharge, "WECHAT_PAY", SimpleNamespace(configured=True)), \
         patch.object(server_recharge, "ACCOUNT_STORE") as store:
      stop = server_recharge.WECHAT_RECONCILE_STOP
      previous = stop.is_set()
      stop.set()
      try:
        server_recharge.reconcile_wechat_orders()
        store.pending_recharge_orders.assert_not_called()
      finally:
        if not previous:
          stop.clear()

  def test_enabled_loop_reconciles_orders_and_waits_until_stopped(self):
    for interval, expected in [(30, 30), (1, 5)]:
      with self.subTest(interval=interval):
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        order = {"id": "example-order"}
        with patch.object(server_recharge, "WECHAT_PAY", SimpleNamespace(configured=True)), \
             patch.object(server_recharge, "WECHAT_RECONCILE_STOP", stop), \
             patch.object(server_recharge, "SETTINGS", SimpleNamespace(wechat_reconcile_interval_seconds=interval)), \
             patch.object(server_recharge, "ACCOUNT_STORE") as store, \
             patch.object(server_recharge, "reconcile_wechat_order") as reconcile:
          store.pending_recharge_orders.return_value = [order]
          server_recharge.reconcile_wechat_orders()
          reconcile.assert_called_once_with(order)
          stop.wait.assert_called_once_with(expected)

  def test_unconfigured_payment_skips_reconciliation(self):
    with patch.object(server_recharge, "WECHAT_PAY", SimpleNamespace(configured=False)), \
         patch.object(server_recharge, "ACCOUNT_STORE") as store, \
         patch.object(server_recharge, "WECHAT_RECONCILE_STOP") as stop:
      server_recharge.reconcile_wechat_orders()
      store.pending_recharge_orders.assert_not_called()
      stop.is_set.assert_not_called()
