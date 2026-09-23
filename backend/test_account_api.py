from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from account_store import AccountStore
from server import Handler, RequestStopped, admin_account, create_user_session, public_user, verify_password


class AccountApiTest(unittest.TestCase):
  def handler(self, payload: dict, actor: dict | None = None):
    handler = object.__new__(Handler)
    handler.read_json = lambda: payload
    handler.require_admin = lambda: actor
    handler.responses = []
    handler.write_json = lambda body, status=200, headers=None: handler.responses.append((body, int(status), headers or {}))
    return handler

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
    self.assertEqual([item["amountCny"] for item in body["products"]], ["50.00", "100.00", "200.00"])
    self.assertTrue(all(item["qrCodeUrl"].startswith("/assets/payment-qr/") for item in body["products"]))
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

  def test_registration_activates_and_starts_session(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      _, accounts = store.create_batch(new_group_name="Audit", count=1, budget_tokens=42, max_sessions=2)
      handler = self.handler({"inviteToken": accounts[0]["inviteToken"], "username": "new.user", "password": "password8"})
      audit = []
      sessions = {}
      with (
        patch("server.ACCOUNT_STORE", store),
        patch("server.USERS", store.users),
        patch("server.SESSIONS", sessions),
        patch("server.provision_micu", return_value={"tokenId": 7, "tokenName": "new.user", "apiKey": "test-key", "status": "ready", "lastBalanceCny": "42.00"}) as provision,
        patch("server.add_audit", side_effect=lambda actor, event, detail="": audit.append((actor, event, detail))),
      ):
        Handler.register(handler)

      body, status, headers = handler.responses[0]
      self.assertEqual(status, 200)
      self.assertEqual(body["user"]["id"], accounts[0]["id"])
      self.assertIn(body["sessionToken"], sessions)
      self.assertIn("Set-Cookie", headers)
      provision.assert_called_once_with("new.user", "42.00")
      self.assertTrue(verify_password("password8", store.users["new.user"]["passwordHash"]))
      self.assertFalse(any(accounts[0]["inviteToken"] in " ".join(item) for item in audit))

  def test_registration_retry_reuses_account_micu_binding_and_session(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      _, accounts = store.create_batch(new_group_name="Audit", count=1, budget_tokens=42, max_sessions=1)
      payload = {"inviteToken": accounts[0]["inviteToken"], "username": "new.user", "password": "password8"}
      first = self.handler(payload)
      retry = self.handler(payload)
      sessions = {}
      provider = MagicMock(return_value={
        "tokenId": 7, "tokenName": "new.user", "apiKey": "test-key", "status": "ready", "lastBalanceCny": "42.00"
      })
      with (
        patch("server.ACCOUNT_STORE", store),
        patch("server.USERS", store.users),
        patch("server.SESSIONS", sessions),
        patch("server.provision_micu", provider),
        patch("server.add_audit"),
      ):
        Handler.register(first)
        Handler.register(retry)

      self.assertEqual(len(store.users), 1)
      self.assertEqual(provider.call_count, 1)
      self.assertEqual(len(sessions), 1)
      self.assertEqual(first.responses[0][0]["sessionToken"], retry.responses[0][0]["sessionToken"])

  def test_concurrent_registration_requests_share_one_activation(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      _, accounts = store.create_batch(new_group_name="Audit", count=1, budget_tokens=42, max_sessions=1)
      payload = {"inviteToken": accounts[0]["inviteToken"], "username": "new.user", "password": "password8"}
      sessions = {}
      provider = MagicMock(return_value={
        "tokenId": 7, "tokenName": "new.user", "apiKey": "test-key", "status": "ready", "lastBalanceCny": "42.00"
      })
      barrier = threading.Barrier(2)

      def register_once():
        handler = self.handler(payload)
        barrier.wait()
        Handler.register(handler)
        return handler.responses[0][0]

      with (
        patch("server.ACCOUNT_STORE", store),
        patch("server.USERS", store.users),
        patch("server.SESSIONS", sessions),
        patch("server.provision_micu", provider),
        patch("server.add_audit"),
      ):
        with ThreadPoolExecutor(max_workers=2) as executor:
          responses = list(executor.map(lambda _: register_once(), range(2)))

      self.assertEqual(provider.call_count, 1)
      self.assertEqual(len(store.users), 1)
      self.assertEqual(len(sessions), 1)
      self.assertEqual(responses[0]["sessionToken"], responses[1]["sessionToken"])

  def test_losing_invitation_for_same_username_remains_pending(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      _, accounts = store.create_batch(new_group_name="Audit", count=2, budget_tokens=42, max_sessions=1)
      first = self.handler({"inviteToken": accounts[0]["inviteToken"], "username": "New.User", "password": "password8"})
      second = self.handler({"inviteToken": accounts[1]["inviteToken"], "username": "new.user", "password": "password8"})
      provider = MagicMock(return_value={"tokenId": 7, "tokenName": "New.User", "apiKey": "test-key"})
      with (
        patch("server.ACCOUNT_STORE", store),
        patch("server.USERS", store.users),
        patch("server.SESSIONS", {}),
        patch("server.provision_micu", provider),
        patch("server.add_audit"),
      ):
        Handler.register(first)
        with self.assertRaisesRegex(ValueError, "username already exists"):
          Handler.register(second)

      self.assertEqual(provider.call_count, 1)
      self.assertIsNotNone(store.pending_by_token(accounts[1]["inviteToken"]))

  def test_zero_session_invite_activates_without_login_token(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      _, accounts = store.create_batch(new_group_name="Paused", count=1, budget_tokens=0, max_sessions=0)
      handler = self.handler({"inviteToken": accounts[0]["inviteToken"], "username": "new.user", "password": "password8"})
      with (
        patch("server.ACCOUNT_STORE", store),
        patch("server.USERS", store.users),
        patch("server.SESSIONS", {}),
        patch("server.provision_micu", return_value={"tokenId": 7, "tokenName": "new.user", "apiKey": "test-key"}),
        patch("server.add_audit"),
      ):
        Handler.register(handler)

      body, status, _ = handler.responses[0]
      self.assertEqual(status, 200)
      self.assertEqual(body["sessionError"], "session_limit")
      self.assertNotIn("sessionToken", body)
      self.assertIn("new.user", store.users)

  def test_concurrent_login_session_creation_honors_limit_atomically(self) -> None:
    user = {"username": "alice", "enabled": True, "maxSessions": 1}
    sessions = {}
    barrier = threading.Barrier(2)

    def login_once():
      barrier.wait()
      return create_user_session(user, "login success")

    with patch("server.SESSIONS", sessions), patch("server.add_audit"):
      with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: login_once(), range(2)))

    self.assertEqual(len(sessions), 1)
    self.assertEqual(sum(1 for token, _ in results if token), 1)
    self.assertEqual(sum(1 for _, error in results if error == "session_limit"), 1)

  def test_registration_rejects_short_password_before_consuming_token(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      _, accounts = store.create_batch(new_group_name="Audit", count=1, budget_tokens=0, max_sessions=1)
      handler = self.handler({"inviteToken": accounts[0]["inviteToken"], "username": "new.user", "password": "short"})
      with patch("server.ACCOUNT_STORE", store):
        with self.assertRaisesRegex(ValueError, "at least 8"):
          Handler.register(handler)
      self.assertIn(accounts[0]["id"], store.pending_accounts)

  def test_admin_can_create_group_set_limit_and_reset_budget(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      actor = {"id": "usr_admin", "username": "admin", "role": "system_admin"}
      create_handler = self.handler({"name": "Audit"}, actor)
      with patch("server.ACCOUNT_STORE", store), patch("server.add_audit"):
        Handler.create_group(create_handler)
      group = create_handler.responses[0][0]["group"]
      update_handler = self.handler({"diskLimitBytes": 2048}, actor)
      with patch("server.ACCOUNT_STORE", store), patch("server.add_audit"):
        Handler.update_group(update_handler, group["id"])
      self.assertEqual(store.groups[group["id"]]["diskLimitBytes"], 2048)
      run_limit_handler = self.handler({"liveRunLimit": 4}, actor)
      with patch("server.ACCOUNT_STORE", store), patch("server.add_audit") as audit:
        Handler.update_group(run_limit_handler, group["id"])
      self.assertEqual(store.groups[group["id"]]["liveRunLimit"], 4)
      audit.assert_called_once_with("admin", "group live run limit updated", f"{group['id']} limit=4")

      zero_handler = self.handler({"liveRunLimit": 0}, actor)
      with patch("server.ACCOUNT_STORE", store), patch("server.add_audit"):
        Handler.update_group(zero_handler, group["id"])
      self.assertEqual(store.groups[group["id"]]["liveRunLimit"], 0)

      invalid_handler = self.handler({"diskLimitBytes": 4096, "liveRunLimit": -1}, actor)
      with patch("server.ACCOUNT_STORE", store), patch("server.add_audit"):
        with self.assertRaisesRegex(ValueError, "non-negative"):
          Handler.update_group(invalid_handler, group["id"])
      self.assertEqual(store.groups[group["id"]]["diskLimitBytes"], 2048)
      self.assertEqual(store.groups[group["id"]]["liveRunLimit"], 0)

      _, invitations = store.create_batch(group_id=group["id"], count=1, budget_tokens=100, max_sessions=1)
      user = store.activate(invitations[0]["inviteToken"], "alice", "hash")
      store.users["alice"]["usedTokens"] = 90
      user["micu"] = {"tokenId": 7, "apiKey": "test-key", "status": "ready", "lastBalanceCny": "1.00"}
      provider = MagicMock()
      provider.add_balance.return_value = {"addedCny": "5.00", "rawQuota": 3_000_000, "remainingCny": "6.00", "remainingPercent": 100.0, "status": "ready"}
      reset_handler = self.handler({"amountCny": "5.00"}, actor)
      with patch("server.ACCOUNT_STORE", store), patch("server.MICU_CLIENT", provider), patch("server.add_audit"):
        Handler.reset_account_budget(reset_handler, user["id"])
      self.assertEqual(store.users["alice"]["micu"]["lastBalanceCny"], "6.00")
      self.assertEqual(store.users["alice"]["micu"]["lastRemainingPercent"], 100.0)
      self.assertEqual(store.users["alice"]["micu"]["rechargeBaselineQuota"], 3_000_000)
      self.assertEqual(store.users["alice"]["usedTokens"], 90)

  def test_self_deletion_is_protected(self) -> None:
    actor = {"id": "usr_admin", "username": "admin", "role": "system_admin"}
    handler = self.handler({}, actor)
    with self.assertRaisesRegex(Exception, "signed-in"):
      Handler.validate_admin_deletion(handler, actor, {"usr_admin"})

  def test_account_delete_cascades_resources_and_invalidates_sessions(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      seed = {
        "admin": {"id": "usr_admin", "username": "admin", "role": "system_admin", "group": "", "passwordHash": "hash"},
        "alice": {"id": "usr_alice", "username": "alice", "role": "user", "group": "", "passwordHash": "hash"},
      }
      store = AccountStore(Path(tempdir) / "accounts.json", seed)
      actor = store.users["admin"]
      handler = self.handler({}, actor)
      runtime = MagicMock()
      runtime.stop_and_wait_for_users.return_value = set()
      runtime.delete_user_resources.return_value = {"workspaces": [{"id": "ws_1"}], "sessionIds": ["chat_1"], "runIds": ["run_1"]}
      sessions = {"token-a": {"username": "alice"}, "token-admin": {"username": "admin"}}
      with (
        patch("server.ACCOUNT_STORE", store),
        patch("server.USERS", store.users),
        patch("server.CHAT_RUNTIME", runtime),
        patch("server.SESSIONS", sessions),
        patch("server.add_audit"),
      ):
        Handler.delete_account(handler, "usr_alice")
      self.assertNotIn("alice", store.users)
      self.assertNotIn("token-a", sessions)
      self.assertIn("token-admin", sessions)
      runtime.delete_user_resources.assert_called_once_with({"alice"})

  def test_account_delete_keeps_account_when_run_does_not_stop(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      seed = {
        "admin": {"id": "usr_admin", "username": "admin", "role": "system_admin", "group": "", "passwordHash": "hash"},
        "alice": {"id": "usr_alice", "username": "alice", "role": "user", "group": "", "passwordHash": "hash"},
      }
      store = AccountStore(Path(tempdir) / "accounts.json", seed)
      handler = self.handler({}, store.users["admin"])
      runtime = MagicMock()
      runtime.stop_and_wait_for_users.return_value = {"run_busy"}
      with patch("server.ACCOUNT_STORE", store), patch("server.USERS", store.users), patch("server.CHAT_RUNTIME", runtime):
        with self.assertRaisesRegex(Exception, "did not stop"):
          Handler.delete_account(handler, "usr_alice")
      self.assertIn("alice", store.users)
      runtime.delete_user_resources.assert_not_called()

  def test_group_delete_removes_active_and_pending_members(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      seed = {
        "admin": {"id": "usr_admin", "username": "admin", "role": "system_admin", "group": "Admins", "passwordHash": "hash"},
        "alice": {"id": "usr_alice", "username": "alice", "role": "user", "group": "Audit", "passwordHash": "hash"},
      }
      store = AccountStore(Path(tempdir) / "accounts.json", seed)
      audit_group = store.group_by_name("Audit")
      _, pending = store.create_batch(group_id=audit_group["id"], count=1, budget_tokens=0, max_sessions=1)
      handler = self.handler({}, store.users["admin"])
      runtime = MagicMock()
      runtime.stop_and_wait_for_users.return_value = set()
      runtime.delete_user_resources.return_value = {"workspaces": [], "sessionIds": [], "runIds": []}
      with (
        patch("server.ACCOUNT_STORE", store),
        patch("server.USERS", store.users),
        patch("server.CHAT_RUNTIME", runtime),
        patch("server.add_audit"),
      ):
        Handler.delete_group(handler, audit_group["id"])
      self.assertIsNone(store.group_by_name("Audit"))
      self.assertNotIn("alice", store.users)
      self.assertNotIn(pending[0]["id"], store.pending_accounts)


if __name__ == "__main__":
  unittest.main()
