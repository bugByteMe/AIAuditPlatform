from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
  unittest.main()
