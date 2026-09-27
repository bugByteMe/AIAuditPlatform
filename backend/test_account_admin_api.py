from account_api_test_support import *


class AccountAdminApiTest(AccountApiTestBase):
  def test_batch_endpoint_returns_admin_visible_tokens(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      actor = {"username": "admin", "role": "system_admin"}
      handler = self.handler({"newGroupName": "Audit", "count": 2, "budgetTokens": 1000, "maxSessions": 2}, actor)
      with patch("server.ACCOUNT_STORE", store), patch("server.add_audit"):
        Handler.create_account_batch(handler)

      body, status, _ = handler.responses[0]
      self.assertEqual(status, 201)
      self.assertEqual(len(body["accounts"]), 2)
      self.assertTrue(all(account["inviteToken"] for account in body["accounts"]))

  def test_batch_endpoint_preserves_zero_concurrent_sessions(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      actor = {"username": "admin", "role": "system_admin"}
      handler = self.handler({"newGroupName": "Paused", "count": 1, "budgetTokens": 0, "maxSessions": 0}, actor)
      with patch("server.ACCOUNT_STORE", store), patch("server.add_audit"):
        Handler.create_account_batch(handler)

      body, status, _ = handler.responses[0]
      self.assertEqual(status, 201)
      self.assertEqual(body["accounts"][0]["maxSessions"], 0)

  def test_batch_endpoint_stops_when_admin_check_fails(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      handler = self.handler({"newGroupName": "Audit", "count": 1, "budgetTokens": 0, "maxSessions": 1})
      handler.require_admin = lambda: (_ for _ in ()).throw(RequestStopped())
      with patch("server.ACCOUNT_STORE", store):
        with self.assertRaises(RequestStopped):
          Handler.create_account_batch(handler)
      self.assertEqual(store.pending_accounts, {})

  def test_public_user_never_exposes_invite_token(self) -> None:
    pending = {
      "id": "usr_1",
      "status": "pending",
      "inviteToken": "invite-placeholder",
      "micu": {"apiKey": "managed-placeholder"},
      "customCodex": {"apiKey": "custom-placeholder"},
    }
    self.assertNotIn("inviteToken", public_user(pending))
    self.assertNotIn("micu", public_user(pending))
    self.assertNotIn("customCodex", public_user(pending))
    self.assertEqual(admin_account(pending)["inviteToken"], "invite-placeholder")

    active = {
      **pending,
      "status": "active",
      "activationInviteDigest": "private-digest",
      "providerMode": "micu",
      "micu": {"apiKey": "managed-placeholder", "lastBalanceCny": "12.34", "lastRemainingPercent": 67.8, "status": "ready"},
    }
    self.assertNotIn("activationInviteDigest", public_user(active))
    self.assertNotIn("activationInviteDigest", admin_account(active))
    user_budget = public_user(active)["budget"]
    self.assertEqual(user_budget["remainingPercent"], 67.8)
    self.assertNotIn("remaining", user_budget)
    self.assertNotIn("currency", user_budget)
    self.assertEqual(admin_account(active)["budget"]["remaining"], "12.34")

  def test_admin_accounts_refreshes_micu_balances_from_one_token_list(self) -> None:
    users = {
      "alice": {
        "id": "usr_alice", "username": "alice", "status": "active", "enabled": True,
        "providerMode": "micu", "micu": {"tokenId": 7, "lastBalanceCny": "0.00"},
      },
      "bob": {
        "id": "usr_bob", "username": "bob", "status": "active", "enabled": True,
        "providerMode": "micu", "micu": {"tokenId": 9, "lastBalanceCny": "0.00"},
      },
      "custom": {
        "id": "usr_custom", "username": "custom", "status": "active", "enabled": True,
        "providerMode": "custom", "customCodex": {},
      },
    }
    account_store = MagicMock()
    account_store.pending_accounts = {}
    account_store.groups = {}
    workspace_store = MagicMock()
    workspace_store.usage_summaries.return_value = ({}, {})
    provider = MagicMock()
    provider.all_tokens.return_value = [{"id": 9}, {"id": 7}]
    provider.balance_from_token.side_effect = lambda token, reference: {
      "remainingCny": f"{token['id']}.00", "remainingPercent": float(token["id"]),
      "referenceQuota": token["id"] if reference is None else reference, "status": "ready"
    }
    handler = self.handler({}, {"username": "admin", "role": "system_admin"})

    with (
      patch("server.USERS", users),
      patch("server.ACCOUNT_STORE", account_store),
      patch("server.WORKSPACE_STORE", workspace_store),
      patch("server.MICU_CLIENT", provider),
    ):
      Handler.accounts(handler)

    provider.all_tokens.assert_called_once_with()
    provider.balance.assert_not_called()
    self.assertEqual(users["alice"]["micu"]["lastBalanceCny"], "7.00")
    self.assertEqual(users["bob"]["micu"]["lastBalanceCny"], "9.00")
    self.assertEqual(users["alice"]["micu"]["rechargeBaselineQuota"], 7)
    self.assertEqual(users["bob"]["micu"]["rechargeBaselineQuota"], 9)
    account_store.save.assert_called_once_with()
    self.assertNotIn("micu", users["custom"])

  def test_worker_status_requires_admin_and_returns_runtime_summary(self) -> None:
    actor = {"username": "admin", "role": "system_admin"}
    handler = self.handler({}, actor)
    handler.require_admin = MagicMock(return_value=actor)
    runtime = MagicMock()
    runtime.worker_status.return_value = [{"id": "worker-1", "healthy": True}]
    with patch("server.CHAT_RUNTIME", runtime):
      Handler.workers(handler)
    handler.require_admin.assert_called_once_with()
    self.assertEqual(handler.responses[0][0], {"workers": [{"id": "worker-1", "healthy": True}]})

  def test_recharge_info_returns_only_current_users_managed_credentials(self) -> None:
    user = {
      "id": "usr_alice",
      "username": "alice",
      "micu": {"apiKey": "managed-placeholder"},
    }
    handler = self.handler({})
    handler.require_user = MagicMock(return_value=user)
    with patch("server.add_audit") as audit:
      Handler.recharge_info(handler)

    body, status, _ = handler.responses[0]
    self.assertEqual(status, 200)
    self.assertEqual(body["username"], "alice")
    self.assertEqual(body["apiKey"], "managed-placeholder")
    self.assertEqual([item["amountCny"] for item in body["products"]], ["1.00", "50.00", "100.00", "200.00"])
    self.assertEqual(body["products"][0]["creditCny"], "0.80")
    self.assertTrue(body["products"][0]["debug"])
    handler.require_user.assert_called_once_with()
    audit.assert_called_once_with("alice", "MicuAPI credentials viewed", "usr_alice")

  def test_recharge_import_previews_then_applies_eighty_percent_once(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(
        Path(tempdir) / "accounts.json",
        {"Alice": {"id": "usr_alice", "username": "Alice", "role": "user", "micu": {"tokenId": 7, "apiKey": "key"}}},
      )
      actor = {"id": "usr_admin", "username": "admin", "role": "system_admin"}
      parsed = {
        "digest": "workbook-digest",
        "sheetCount": 2,
        "targetSheetCount": 1,
        "ignoredSheetCount": 1,
        "records": [
          {
            "sheet": "Target",
            "row": 10,
            "status": "candidate",
            "reason": "",
            "username": "alice",
            "paidAt": "2026-09-20 14:39:07",
            "amountCny": "50.00",
            "creditCny": "40.00",
            "paymentNumber": "payment-number-1",
            "paymentRef": "***0001",
          }
        ],
      }
      provider = MagicMock()
      provider.add_balance.return_value = {"rawQuota": 20_500_000, "remainingCny": "41.00", "remainingPercent": 100.0, "status": "ready"}

      preview = self.handler({}, actor)
      preview.read_recharge_upload = lambda: (b"xlsx", {})
      with patch("server.ACCOUNT_STORE", store), patch("server.USERS", store.users), patch("server.parse_recharge_workbook", return_value=parsed):
        Handler.preview_recharge_import(preview)
      self.assertEqual(preview.responses[0][0]["summary"]["eligible"], 1)
      provider.add_balance.assert_not_called()

      apply = self.handler({}, actor)
      apply.read_recharge_upload = lambda: (b"xlsx", {"previewDigest": "workbook-digest"})
      with (
        patch("server.ACCOUNT_STORE", store),
        patch("server.USERS", store.users),
        patch("server.MICU_CLIENT", provider),
        patch("server.parse_recharge_workbook", return_value=parsed),
        patch("server.add_audit"),
      ):
        Handler.apply_recharge_import(apply)
      self.assertEqual(apply.responses[0][0]["summary"]["applied"], 1)
      provider.add_balance.assert_called_once_with(store.users["Alice"]["micu"], "40.00")
      self.assertEqual(store.users["Alice"]["micu"]["rechargeBaselineQuota"], 20_500_000)
      self.assertEqual(store.users["Alice"]["micu"]["lastRemainingPercent"], 100.0)
      self.assertEqual(store.recharge_history("usr_alice")[0]["amountCny"], "50.00")

      repeated = self.handler({}, actor)
      repeated.read_recharge_upload = lambda: (b"xlsx", {"previewDigest": "workbook-digest"})
      with (
        patch("server.ACCOUNT_STORE", store),
        patch("server.USERS", store.users),
        patch("server.MICU_CLIENT", provider),
        patch("server.parse_recharge_workbook", return_value=parsed),
        patch("server.add_audit"),
      ):
        Handler.apply_recharge_import(repeated)
      self.assertEqual(repeated.responses[0][0]["summary"]["duplicate"], 1)
      self.assertEqual(provider.add_balance.call_count, 1)

  def test_recharge_import_requires_system_admin(self) -> None:
    handler = self.handler({})
    handler.require_admin = lambda: (_ for _ in ()).throw(RequestStopped())
    with self.assertRaises(RequestStopped):
      Handler.preview_recharge_import(handler)

  def test_wechat_debug_recharge_credits_eighty_cents_exactly_once(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(
        Path(tempdir) / "accounts.json",
        {"alice": {"id": "usr_alice", "username": "alice", "micu": {"tokenId": 7}}},
      )
      order = store.create_recharge_order(
        {
          "id": "wpo_debug",
          "outTradeNo": "AIADEBUG1",
          "userId": "usr_alice",
          "amountCny": "1.00",
          "amountFen": 100,
          "creditCny": "0.80",
          "status": "pending",
        }
      )
      transaction = {
        "appid": "wx-test",
        "mchid": "1900000001",
        "out_trade_no": "AIADEBUG1",
        "transaction_id": "4200000000001",
        "trade_state": "SUCCESS",
        "success_time": "2026-09-25T12:00:00+08:00",
        "amount": {"total": 100, "currency": "CNY"},
      }
      provider = MagicMock()
      provider.add_balance.return_value = {
        "rawQuota": 900_000, "remainingCny": "1.80", "remainingPercent": 100.0, "status": "ready"
      }
      wechat = MagicMock(app_id="wx-test", merchant_id="1900000001")
      with patch("server.ACCOUNT_STORE", store), patch("server.MICU_CLIENT", provider), patch("server.WECHAT_PAY", wechat), patch("server.add_audit"):
        with ThreadPoolExecutor(max_workers=4) as executor:
          results = list(executor.map(lambda _: apply_wechat_transaction(order, transaction), range(8)))
      self.assertEqual(store.recharge_order("wpo_debug")["status"], "applied")
      self.assertTrue(all(result["status"] in {"crediting", "applied"} for result in results))
      provider.add_balance.assert_called_once_with(store.users["alice"]["micu"], "0.80")

  def test_create_debug_recharge_order_is_bound_to_current_user(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      user = {"id": "usr_alice", "username": "alice", "micu": {"tokenId": 7}}
      store = AccountStore(Path(tempdir) / "accounts.json", {"alice": user})
      handler = self.handler({"amountCny": "1.00"})
      handler.require_user = MagicMock(return_value=store.users["alice"])
      wechat = MagicMock()
      wechat.create_native_order.return_value = {"code_url": "weixin://wxpay/bizpayurl?pr=test"}
      with patch("server.ACCOUNT_STORE", store), patch("server.WECHAT_PAY", wechat), patch("server.add_audit"):
        Handler.create_recharge_order(handler)
      body, status, _ = handler.responses[0]
      self.assertEqual(status, 201)
      self.assertEqual(body["order"]["amountCny"], "1.00")
      persisted = store.recharge_order(body["order"]["id"])
      self.assertEqual(persisted["userId"], "usr_alice")
      self.assertEqual(persisted["creditCny"], "0.80")
      wechat.require_configured.assert_called_once_with()
      wechat.create_native_order.assert_called_once()

  def test_wechat_callback_decodes_and_acknowledges_after_processing(self) -> None:
    handler = self.handler({})
    handler.headers = {"Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")
    binary_responses = []
    handler.write_binary = lambda body, content_type, status=200, headers=None, no_store=False: binary_responses.append((body, int(status)))
    transaction = {"out_trade_no": "AIA1"}
    store = MagicMock()
    store.recharge_order.return_value = {"id": "wpo_1"}
    wechat = MagicMock()
    wechat.decode_callback.return_value = {"eventType": "TRANSACTION.SUCCESS", "transaction": transaction}
    with patch("server.ACCOUNT_STORE", store), patch("server.WECHAT_PAY", wechat), patch("server.apply_wechat_transaction") as apply:
      Handler.wechat_payment_notify(handler)
    wechat.decode_callback.assert_called_once_with(handler.headers, b"{}")
    apply.assert_called_once_with(store.recharge_order.return_value, transaction)
    self.assertEqual(binary_responses, [(b"", 204)])

