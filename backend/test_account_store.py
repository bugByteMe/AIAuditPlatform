from __future__ import annotations

import sys
import tempfile
import threading
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from account_store import AccountStore


class AccountStoreTest(unittest.TestCase):
  def test_account_settings_persist_across_reload(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      path = Path(tempdir) / "accounts.json"
      seed = {
        "li.review": {
          "username": "li.review",
          "codex": {"baseUrl": "https://api.openai.com/v1", "apiKey": ""},
        }
      }
      store = AccountStore(path, seed)
      store.users["li.review"]["codex"] = {"baseUrl": "https://codex.example/v1", "apiKey": "sk-test"}
      store.save()

      reloaded = AccountStore(path, {})
      self.assertEqual(reloaded.users["li.review"]["codex"]["baseUrl"], "https://codex.example/v1")
      self.assertEqual(reloaded.users["li.review"]["codex"]["apiKey"], "sk-test")
      if sys.platform != "win32":
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

  def test_seed_is_copied_before_mutation(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      seed = {"user": {"username": "user", "codex": {"apiKey": ""}}}
      store = AccountStore(Path(tempdir) / "accounts.json", seed)
      store.users["user"]["codex"]["apiKey"] = "changed"
      self.assertEqual(seed["user"]["codex"]["apiKey"], "")

  def test_legacy_accounts_migrate_to_stable_ids_and_groups(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      path = Path(tempdir) / "accounts.json"
      seed = {
        "alice": {"username": "alice", "group": "Audit", "passwordHash": "hash"},
        "bob": {"username": "bob", "group": "Audit", "passwordHash": "hash"},
      }
      store = AccountStore(path, seed)
      alice_id = store.users["alice"]["id"]
      group_id = store.users["alice"]["groupId"]

      reloaded = AccountStore(path, {})
      self.assertEqual(reloaded.users["alice"]["id"], alice_id)
      self.assertEqual(reloaded.users["bob"]["groupId"], group_id)
      self.assertEqual(reloaded.groups[group_id]["name"], "Audit")
      self.assertIsNone(reloaded.groups[group_id]["diskLimitBytes"])
      self.assertEqual(reloaded.groups[group_id]["liveRunLimit"], 1)

  def test_v2_groups_migrate_to_unlimited_disk(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      path = Path(tempdir) / "accounts.json"
      path.write_text(
        json.dumps(
          {
            "schemaVersion": 2,
            "users": {"alice": {"id": "usr_1", "username": "alice", "groupId": "grp_1"}},
            "pendingAccounts": {},
            "groups": {"grp_1": {"id": "grp_1", "name": "Audit"}},
          }
        ),
        encoding="utf-8",
      )
      store = AccountStore(path, {})
      self.assertEqual(store.state()["schemaVersion"], 6)
      self.assertIsNone(store.groups["grp_1"]["diskLimitBytes"])
      self.assertEqual(store.groups["grp_1"]["liveRunLimit"], 1)

  def test_v3_groups_migrate_to_default_live_run_limit(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      path = Path(tempdir) / "accounts.json"
      path.write_text(
        json.dumps(
          {
            "schemaVersion": 3,
            "users": {"alice": {"id": "usr_1", "username": "alice", "groupId": "grp_1"}},
            "pendingAccounts": {},
            "groups": {"grp_1": {"id": "grp_1", "name": "Audit", "diskLimitBytes": None}},
          }
        ),
        encoding="utf-8",
      )
      store = AccountStore(path, {})
      self.assertEqual(store.state()["schemaVersion"], 6)
      self.assertEqual(store.groups["grp_1"]["liveRunLimit"], 1)

  def test_batch_invites_have_unique_ids_tokens_and_no_credentials(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      group, accounts = store.create_batch(new_group_name="New team", count=3, budget_tokens=5000, max_sessions=2)

      self.assertEqual(group["name"], "New team")
      self.assertEqual(len({account["id"] for account in accounts}), 3)
      self.assertEqual(len({account["inviteToken"] for account in accounts}), 3)
      self.assertTrue(all(account["username"] is None for account in accounts))
      self.assertTrue(all("passwordHash" not in account for account in accounts))
      self.assertTrue(all(account["groupId"] == group["id"] for account in accounts))
      self.assertTrue(all(account["initialBudgetCny"] == "5000.00" and account["maxSessions"] == 2 for account in accounts))

  def test_batch_invites_allow_zero_concurrent_sessions(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      _, accounts = store.create_batch(new_group_name="Paused team", count=1, budget_tokens=0, max_sessions=0)

      self.assertEqual(accounts[0]["maxSessions"], 0)
      with self.assertRaisesRegex(ValueError, "between 0 and"):
        store.create_batch(new_group_name="Invalid team", count=1, budget_tokens=0, max_sessions=-1)

  def test_batch_creation_rolls_back_when_save_fails(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      with patch.object(store, "save", side_effect=OSError("disk full")):
        with self.assertRaises(OSError):
          store.create_batch(new_group_name="Rollback", count=2, budget_tokens=0, max_sessions=1)
      self.assertEqual(store.groups, {})
      self.assertEqual(store.pending_accounts, {})

  def test_activation_preserves_identity_and_consumes_invite(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      group, accounts = store.create_batch(new_group_name="Audit", count=1, budget_tokens=900, max_sessions=3)
      invite = accounts[0]
      user = store.activate(invite["inviteToken"], "New.User", "password-hash")

      self.assertEqual(user["id"], invite["id"])
      self.assertEqual(user["groupId"], group["id"])
      self.assertEqual(user["initialBudgetCny"], "900.00")
      self.assertEqual(user["maxSessions"], 3)
      self.assertNotIn(invite["id"], store.pending_accounts)
      self.assertIsNone(store.pending_by_token(invite["inviteToken"]))
      self.assertTrue(store.username_exists("new.user"))

  def test_duplicate_username_is_case_insensitive_and_invite_remains_pending(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      seed = {"Alice": {"username": "Alice", "group": "Audit", "passwordHash": "hash"}}
      store = AccountStore(Path(tempdir) / "accounts.json", seed)
      group_id = next(iter(store.groups))
      _, accounts = store.create_batch(group_id=group_id, count=1, budget_tokens=0, max_sessions=1)
      with self.assertRaisesRegex(ValueError, "username already exists"):
        store.activate(accounts[0]["inviteToken"], "alice", "hash")
      self.assertEqual(store.pending_accounts[accounts[0]["id"]]["status"], "pending")

  def test_revoked_invite_cannot_activate(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      _, accounts = store.create_batch(new_group_name="Audit", count=1, budget_tokens=0, max_sessions=1)
      account = store.revoke_invite(accounts[0]["id"])
      self.assertEqual(account["status"], "revoked")
      self.assertEqual(account["inviteToken"], "")
      with self.assertRaisesRegex(ValueError, "invalid invite token"):
        store.activate(accounts[0]["inviteToken"], "new.user", "hash")

  def test_invite_is_consumed_once_under_concurrent_activation(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      _, accounts = store.create_batch(new_group_name="Audit", count=1, budget_tokens=0, max_sessions=1)
      token = accounts[0]["inviteToken"]
      barrier = threading.Barrier(2)

      def activate(username: str) -> str:
        barrier.wait()
        try:
          store.activate(token, username, "hash")
          return "activated"
        except ValueError:
          return "rejected"

      with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(activate, ["first.user", "second.user"]))
      self.assertEqual(sorted(results), ["activated", "rejected"])
      self.assertEqual(len(store.users), 1)

  def test_group_limits_and_reporting_usage_persist(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      path = Path(tempdir) / "accounts.json"
      store = AccountStore(path, {})
      group, accounts = store.create_batch(new_group_name="Audit", count=1, budget_tokens=100, max_sessions=1)
      user = store.activate(accounts[0]["inviteToken"], "alice", "hash")
      store.users["alice"]["usedTokens"] = 75
      store.save()
      store.update_group_disk_limit(group["id"], 1024)
      store.update_group_live_run_limit(group["id"], 3)
      reloaded = AccountStore(path, {})
      self.assertEqual(reloaded.groups[group["id"]]["diskLimitBytes"], 1024)
      self.assertEqual(reloaded.groups[group["id"]]["liveRunLimit"], 3)
      self.assertEqual(reloaded.users["alice"]["usedTokens"], 75)

  def test_group_live_run_limit_must_be_positive(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {})
      group = store.create_group("Audit")
      with self.assertRaisesRegex(ValueError, "at least 1"):
        store.update_group_live_run_limit(group["id"], 0)
      self.assertEqual(store.groups[group["id"]]["liveRunLimit"], 1)

  def test_recharge_payment_is_unique_persistent_and_visible_to_its_user(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      path = Path(tempdir) / "accounts.json"
      store = AccountStore(path, {"alice": {"id": "usr_alice", "username": "alice", "micu": {"tokenId": 7}}})
      reserved, _ = store.reserve_recharge_payment(
        "payment-hash",
        {
          "userId": "usr_alice",
          "paidAt": "2026-09-20 14:39:07",
          "amountCny": "50.00",
          "creditCny": "40.00",
          "importedAt": "2026-09-20 15:00:00",
          "paymentRef": "***6241",
        },
      )
      self.assertTrue(reserved)
      self.assertFalse(store.reserve_recharge_payment("payment-hash", {})[0])
      store.finish_recharge_payment("payment-hash", status="applied", binding_updates={"lastBalanceCny": "40.00"})
      reloaded = AccountStore(path, {})
      self.assertEqual(reloaded.recharge_history("usr_alice")[0]["amountCny"], "50.00")
      self.assertEqual(reloaded.users["alice"]["micu"]["lastBalanceCny"], "40.00")

  def test_user_deletion_keeps_payment_hash_as_anonymous_tombstone(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      store = AccountStore(Path(tempdir) / "accounts.json", {"alice": {"id": "usr_alice", "username": "alice"}})
      store.reserve_recharge_payment(
        "payment-hash",
        {"userId": "usr_alice", "paidAt": "2026-09-20 14:39:07", "amountCny": "50.00", "importedAt": "2026-09-20 15:00:00"},
      )
      store.remove_accounts({"usr_alice"})
      tombstone = store.recharge_payment("payment-hash")
      self.assertEqual(tombstone["status"], "used")
      self.assertNotIn("userId", tombstone)


if __name__ == "__main__":
  unittest.main()
