from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from account_store import AccountStore
from server import Handler, RequestStopped, admin_account, public_user, verify_password


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
    pending = {"id": "usr_1", "status": "pending", "inviteToken": "secret-token"}
    self.assertNotIn("inviteToken", public_user(pending))
    self.assertEqual(admin_account(pending)["inviteToken"], "secret-token")

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

  def test_registration_activates_and_starts_session(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      _, accounts = store.create_batch(new_group_name="Audit", count=1, budget_tokens=42, max_sessions=2)
      handler = self.handler({"inviteToken": accounts[0]["inviteToken"], "username": "new.user", "password": "password8"})
      started = []
      handler.start_session = lambda user, event: started.append((user, event))
      audit = []
      with (
        patch("server.ACCOUNT_STORE", store),
        patch("server.USERS", store.users),
        patch("server.add_audit", side_effect=lambda actor, event, detail="": audit.append((actor, event, detail))),
      ):
        Handler.register(handler)

      user, event = started[0]
      self.assertEqual(event, "registration login success")
      self.assertEqual(user["id"], accounts[0]["id"])
      self.assertTrue(verify_password("password8", user["passwordHash"]))
      self.assertFalse(any(accounts[0]["inviteToken"] in " ".join(item) for item in audit))

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

      _, invitations = store.create_batch(group_id=group["id"], count=1, budget_tokens=100, max_sessions=1)
      user = store.activate(invitations[0]["inviteToken"], "alice", "hash")
      store.users["alice"]["usedTokens"] = 90
      reset_handler = self.handler({"budgetTokens": 500}, actor)
      with patch("server.ACCOUNT_STORE", store), patch("server.add_audit"):
        Handler.reset_account_budget(reset_handler, user["id"])
      self.assertEqual(store.users["alice"]["budgetTokens"], 500)
      self.assertEqual(store.users["alice"]["usedTokens"], 0)

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
