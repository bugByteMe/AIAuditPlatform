import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from account_store import AccountStore
from identity_database import IdentityDatabase
from identity_admin import change_customer, full_delete, require_central_admin
from identity_service import IdentityAuthority
from oidc_client import IdentityNameTaken, IdentityError
from workspace_store import StorageError


class IdentityAdministrationTest(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.addCleanup(self.directory.cleanup)
    self.store = AccountStore(Path(self.directory.name) / "accounts.json", {})
    self.addCleanup(self.store.database.engine.dispose)
    self.db = IdentityDatabase(self.store.database.engine)
    self.store.identity_db = self.db
    self.group = self.store.create_group("Synthetic")
    self.identity = {"sub": "subject-a", "username": "synthetic-user", "customerId": "public", "micu": {"tokenId": 17}}
    self.user = self.store.admit_identity("https://identity.example.test/realms/audit", self.identity, self.group["id"])

  def test_local_deletion_allows_fresh_ordinary_readmission(self):
    original = self.user["id"]
    self.store.remove_accounts({original})
    self.assertIsNone(self.db.linked_user_id(self.user["oidcIssuer"], "subject-a"))
    profile = self.store.admit_identity(self.user["oidcIssuer"], self.identity, self.group["id"])
    self.assertNotEqual(profile["id"], original)
    self.assertEqual(profile["role"], "user")
    self.assertEqual(profile["micu"], self.identity["micu"])

  def test_group_deletion_removes_links(self):
    self.store.remove_group_and_accounts(self.group["id"], {self.user["id"]})
    self.assertIsNone(self.db.linked_user_id(self.user["oidcIssuer"], "subject-a"))

  def test_failed_deletion_preserves_link(self):
    with patch.object(self.store, "_tombstone_recharge_orders", side_effect=RuntimeError("synthetic failure")):
      with self.assertRaises(RuntimeError):
        self.store.remove_accounts({self.user["id"]})
    self.assertEqual(self.db.linked_user_id(self.user["oidcIssuer"], "subject-a"), self.user["id"])

  def test_database_failure_rolls_back_link_removal(self):
    with patch.object(self.store.database, "_upsert", side_effect=RuntimeError("synthetic failure")):
      with self.assertRaises(RuntimeError):
        self.store.remove_accounts({self.user["id"]})
    self.assertEqual(self.db.linked_user_id(self.user["oidcIssuer"], "subject-a"), self.user["id"])

  def test_retained_binding_prevents_old_key_being_given_to_new_identity(self):
    self.db.put(self.db.customers, "public", {"id": "public"})
    self.db.put(self.db.bindings, "subject-a", self.identity, username_key=self.user["username"])
    provision = MagicMock()
    authority = IdentityAuthority(SimpleNamespace(issuer=self.user["oidcIssuer"]), self.db, provision)
    authority.admin = MagicMock()
    with self.assertRaises(IdentityNameTaken):
      authority.provision("new-operation", self.user["username"], "synthetic-password", "public", "0")
    authority.admin.create.assert_not_called()
    provision.assert_not_called()

  def test_dedicated_and_local_modes_cannot_administer_central_identity(self):
    for enabled, customer in [(False, ""), (True, "business")]:
      with patch("identity_admin.IDENTITY", SimpleNamespace(enabled=enabled, customer_id=customer)):
        with self.assertRaises(StorageError):
          require_central_admin(MagicMock())

  def context(self):
    import sys
    authority = MagicMock()
    authority.admin.user.return_value = {"username": self.user["username"], "attributes": {"unrelated": ["preserved"]}}
    self.db.put(self.db.bindings, "subject-a", self.identity, username_key=self.user["username"])
    self.db.put(self.db.customers, "business", {"id": "business", "enabled": True})
    handler = MagicMock()
    handler.require_management.return_value = {"username": "synthetic-admin"}
    modules = {"server_state": SimpleNamespace(ACCOUNT_STORE=self.store, add_audit=MagicMock()),
      "server_oidc": SimpleNamespace(services=lambda: (self.store, self.db, authority, None, None))}
    return handler, authority, patch.dict(sys.modules, modules), patch("identity_admin.IDENTITY",
      SimpleNamespace(enabled=True, customer_id="", issuer=self.user["oidcIssuer"]))

  def test_customer_change_preserves_attributes_and_local_group(self):
    handler, authority, modules, settings = self.context()
    handler.read_json.return_value = {"customerId": "business"}
    with modules, settings:
      change_customer(handler, self.user["id"])
    self.assertEqual(self.user["customerId"], "business")
    self.assertEqual(self.user["groupId"], self.group["id"])
    self.assertEqual(self.db.get(self.db.bindings, "subject-a")["customerId"], "business")
    self.assertEqual(authority.admin.call.call_args.kwargs["json"]["attributes"],
      {"unrelated": ["preserved"], "customerId": ["business"]})
    handler.invalidate_user_sessions.assert_called_once_with({self.user["username"]})

  def test_full_delete_disables_before_remote_call_and_preserves_micu_binding(self):
    handler, authority, modules, settings = self.context()
    handler.read_json.return_value = {"confirmSubject": "subject-a"}
    with modules, settings:
      full_delete(handler, self.user["id"])
    self.assertFalse(self.user["enabled"])
    authority.admin.call.assert_called_once_with("DELETE", "/users/subject-a", accepted_statuses=(404,))
    handler.delete_account.assert_called_once_with(self.user["id"])
    self.assertIsNotNone(self.db.get(self.db.bindings, "subject-a"))

  def test_remote_failure_is_retryable_with_disabled_local_profile(self):
    handler, authority, modules, settings = self.context()
    handler.read_json.return_value = {"confirmSubject": "subject-a"}
    authority.admin.call.side_effect = RuntimeError("synthetic unavailable")
    with modules, settings, self.assertRaises(RuntimeError):
      full_delete(handler, self.user["id"])
    self.assertFalse(self.user["enabled"])
    handler.delete_account.assert_not_called()

  def test_full_delete_rejects_wrong_confirmation_before_mutation(self):
    handler, authority, modules, settings = self.context()
    handler.read_json.return_value = {"confirmSubject": "wrong-subject"}
    with modules, settings, self.assertRaises(IdentityError):
      full_delete(handler, self.user["id"])
    self.assertTrue(self.user["enabled"])
    authority.admin.call.assert_not_called()

  def test_full_delete_respects_admin_protection(self):
    handler, authority, modules, settings = self.context()
    handler.validate_admin_deletion.side_effect = StorageError("protected_admin", "synthetic protection")
    with modules, settings, self.assertRaises(StorageError):
      full_delete(handler, self.user["id"])
    authority.admin.call.assert_not_called()


if __name__ == "__main__":
  unittest.main()
