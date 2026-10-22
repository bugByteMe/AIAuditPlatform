import base64
import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from account_store import AccountStore
from identity_database import IdentityDatabase
from identity_service import IdentityAuthority
from migrate_oidc_accounts import imported_credential, preflight, migrate
from oidc_client import IdentityError, IdentityNameTaken, KeycloakAdmin


ISSUER = "https://identity.example.test/realms/audit"


class SharedIdentityTest(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.addCleanup(self.directory.cleanup)
    self.store = AccountStore(Path(self.directory.name) / "accounts.json", {})
    self.db = IdentityDatabase(self.store.database.engine)
    self.store.identity_db = self.db
    self.group = self.store.create_group("Default")
    self.other = self.store.create_group("Invited")
    self.identity = {"sub": "subject-1", "username": "alice", "customerId": "business",
      "micu": {"tokenId": 17, "apiKey": "synthetic-key"}}

  def admit(self, identity=None, invite=""):
    return self.store.admit_identity(ISSUER, identity or self.identity, self.group["id"], invite)

  def invite(self, customer="business", count=1):
    return self.store.create_batch(group_id=self.other["id"], count=count,
      budget_cny="10.00", max_sessions=2, customer_id=customer)[1]

  def test_first_login_creates_ordinary_profile_in_default_group(self):
    user = self.admit()
    self.assertEqual(user["groupId"], self.group["id"])
    self.assertEqual(user["role"], "user")
    self.assertEqual(user["micu"], self.identity["micu"])
    self.assertNotIn("passwordHash", user)

  def test_existing_profile_preserves_group_permissions_and_usage(self):
    user = self.admit()
    user.update(role="system_admin", groupId=self.other["id"], usedTokens=123)
    self.assertIs(self.admit(), user)
    self.assertEqual(user["role"], "system_admin")
    self.assertEqual(user["groupId"], self.other["id"])
    self.assertEqual(user["usedTokens"], 123)

  def test_disabled_and_deleted_local_accounts_are_not_recreated(self):
    user = self.admit()
    user["enabled"] = False
    with self.assertRaisesRegex(IdentityError, "disabled"):
      self.admit()
    self.store.users.clear()
    with self.assertRaisesRegex(IdentityError, "removed"):
      self.admit()

  def test_username_does_not_implicitly_link_legacy_account(self):
    self.store.users["alice"] = {"id": "legacy", "username": "alice"}
    with self.assertRaisesRegex(IdentityError, "migration"):
      self.admit()

  def test_changed_username_or_customer_fails_closed(self):
    self.admit()
    for change in ({"customerId": "other"}, {"username": "renamed"}):
      with self.assertRaises(IdentityError):
        self.admit({**self.identity, **change})

  def test_invitation_group_and_limits_are_preserved(self):
    pending = self.invite()[0]
    user = self.admit(invite=pending["inviteToken"])
    self.assertEqual(user["id"], pending["id"])
    self.assertEqual(user["groupId"], self.other["id"])
    self.assertEqual(user["maxSessions"], 2)
    self.assertFalse(self.store.pending_accounts)

  def test_customer_mismatch_does_not_consume_invitation(self):
    pending = self.invite("another")[0]
    with self.assertRaisesRegex(IdentityError, "mismatch"):
      self.admit(invite=pending["inviteToken"])
    self.assertIn(pending["id"], self.store.pending_accounts)

  def test_existing_profile_cannot_use_invitation_to_move_groups(self):
    user = self.admit()
    pending = self.invite()[0]
    self.admit(invite=pending["inviteToken"])
    self.assertEqual(user["groupId"], self.group["id"])
    self.assertIn(pending["id"], self.store.pending_accounts)

  def test_reservation_is_durable_and_bound_to_username(self):
    pending = self.invite()[0]
    self.store.reserve_identity_invite(pending["inviteToken"], "alice")
    reopened = AccountStore(self.store.path, {})
    reopened.identity_db = IdentityDatabase(reopened.database.engine)
    self.assertEqual(reopened.pending_accounts[pending["id"]]["status"], "provisioning")
    with self.assertRaisesRegex(IdentityError, "another username"):
      reopened.reserve_identity_invite(pending["inviteToken"], "bob")
    with self.assertRaisesRegex(ValueError, "not pending"):
      reopened.revoke_invite(pending["id"])

  def test_login_finishes_interrupted_registration_in_invitation_group(self):
    pending = self.invite()[0]
    self.store.reserve_identity_invite(pending["inviteToken"], "alice")
    self.assertEqual(self.admit()["groupId"], self.other["id"])

  def test_concurrent_admission_cannot_create_two_profiles(self):
    barrier = threading.Barrier(2)
    def admit():
      barrier.wait()
      return self.admit()["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
      results = list(pool.map(lambda _: admit(), range(2)))
    self.assertEqual(len(set(results)), 1)
    self.assertEqual(len(self.store.users), 1)

  def authority(self):
    self.db.put(self.db.customers, "business", {"id": "business", "enabled": True})
    settings = SimpleNamespace(issuer=ISSUER, clients={"seasonal": {
      "token": "synthetic-service-token", "customerId": "business"}})
    provider = MagicMock(return_value=self.identity["micu"])
    authority = IdentityAuthority(settings, self.db, provider)
    authority.admin = MagicMock()
    authority.admin.create.return_value = {"id": "subject-1", "username": "alice", "enabled": True,
      "attributes": {"customerId": ["business"]}}
    authority.admin.user.return_value = authority.admin.create.return_value
    return authority, provider

  def test_provisioning_retry_does_not_repeat_credit_or_store_password(self):
    authority, provider = self.authority()
    first = authority.provision("operation", "alice", "synthetic-password", "business", "10.00")
    self.assertEqual(first, authority.provision("operation", "alice", "another-password", "business", "10.00"))
    provider.assert_called_once_with("alice", "10.00")
    serialized = json.dumps(self.db.get(self.db.operations, "operation"))
    self.assertNotIn("synthetic-password", serialized)
    self.assertNotIn("another-password", serialized)

  def test_provider_failure_leaves_resumable_subject(self):
    authority, provider = self.authority()
    provider.side_effect = [ValueError("synthetic failure"), self.identity["micu"]]
    with self.assertRaises(ValueError):
      authority.provision("operation", "alice", "password", "business", "10.00")
    self.assertEqual(self.db.get(self.db.operations, "operation")["subject"], "subject-1")
    self.assertEqual(authority.provision("operation", "alice", "password", "business", "10.00")["sub"], "subject-1")

  def test_retry_cannot_change_customer_or_budget(self):
    authority, _ = self.authority()
    authority.provision("operation", "alice", "password", "business", "10.00")
    with self.assertRaisesRegex(IdentityError, "original request"):
      authority.provision("operation", "alice", "password", "business", "99.00")

  def test_remote_binding_rechecks_enabled_and_customer(self):
    authority, _ = self.authority()
    authority.provision("operation", "alice", "password", "business", "10.00")
    with self.assertRaises(IdentityError):
      authority.binding("subject-1", "another")
    authority.admin.user.return_value["enabled"] = False
    with self.assertRaises(IdentityError):
      authority.binding("subject-1", "business")

  def test_service_authentication_requires_bearer_and_scoped_token(self):
    authority, _ = self.authority()
    self.assertEqual(authority.authenticate_client("Bearer synthetic-service-token")[0], "seasonal")
    for bad in ("synthetic-service-token", "Bearer invalid", ""):
      with self.assertRaises(IdentityError):
        authority.authenticate_client(bad)

  def test_keycloak_retry_cannot_claim_preexisting_username(self):
    admin = KeycloakAdmin(SimpleNamespace(issuer=ISSUER))
    admin.find = MagicMock(return_value={"id": "existing", "attributes": {}})
    with self.assertRaisesRegex(IdentityError, "Username already exists"):
      admin.create("alice", "password", "business", "operation")

  def test_confirmed_username_conflict_does_not_strand_operation(self):
    authority, provider = self.authority()
    authority.admin.create.side_effect = IdentityNameTaken("Username already exists")
    with self.assertRaises(IdentityNameTaken):
      authority.provision("operation", "alice", "password", "business", "10.00")
    self.assertIsNone(self.db.get(self.db.operations, "operation"))
    provider.assert_not_called()

  def test_import_preserves_pbkdf2_bytes_including_unicode_password(self):
    for password in ("synthetic-password", "测试口令-é", "x" * 200):
      salt = bytes.fromhex("00112233445566778899aabbccddeeff")
      expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 2000)
      credential = imported_credential(f"pbkdf2_sha256$2000${salt.hex()}${expected.hex()}")
      secret = json.loads(credential["secretData"])
      parameters = json.loads(credential["credentialData"])
      actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(secret["salt"]), parameters["hashIterations"])
      self.assertEqual(actual, base64.b64decode(secret["value"]))
      self.assertEqual(parameters["algorithm"], "pbkdf2-sha256")
      self.assertFalse(credential["temporary"])

  def test_import_rejects_invalid_hash(self):
    for encoded in ("", "argon2$1$00$00", "pbkdf2_sha256$0$00$00"):
      with self.assertRaises(IdentityError):
        imported_credential(encoded)

  def test_offline_migration_preserves_ownership_permissions_and_legacy_case(self):
    salt = bytes.fromhex("00112233445566778899aabbccddeeff")
    digest = hashlib.pbkdf2_hmac("sha256", b"synthetic-password", salt, 2000)
    user = {"id": "legacy-id", "username": "Legacy.User", "role": "system_admin",
      "displayName": "Legacy", "groupId": self.group["id"], "enabled": True,
      "usedTokens": 123, "status": "active", "micu": self.identity["micu"],
      "passwordHash": f"pbkdf2_sha256$2000${salt.hex()}${digest.hex()}"}
    self.store.users[user["username"]] = user
    self.store.save_user(user)
    mapping = {"users": [{"localUserId": "legacy-id", "customerId": "business"}],
      "customers": [{"id": "business", "name": "Synthetic customer"}]}
    settings = SimpleNamespace(issuer=ISSUER, registration_paused=True, customer_id="")
    central = {"id": "subject-legacy", "username": "legacy.user", "enabled": True,
      "attributes": {"customerId": ["business"], "platformUsername": ["Legacy.User"],
        "platformProvisioningId": ["migration:legacy-id"]}}
    admin = MagicMock()
    admin.find.side_effect = [None, central]
    with patch("migrate_oidc_accounts.IDENTITY", settings), patch("migrate_oidc_accounts.KeycloakAdmin", return_value=admin):
      self.assertFalse(migrate(self.store, mapping)["applied"])
      admin.call.assert_not_called()
      self.assertTrue(migrate(self.store, mapping, apply=True)["applied"])
    self.assertEqual(user["id"], "legacy-id")
    self.assertEqual(user["username"], "Legacy.User")
    self.assertEqual(user["groupId"], self.group["id"])
    self.assertEqual(user["role"], "system_admin")
    self.assertEqual(user["usedTokens"], 123)
    self.assertEqual(user["micu"]["tokenId"], 17)
    self.assertNotIn("passwordHash", user)
    self.assertEqual(self.db.linked_user_id(ISSUER, "subject-legacy"), "legacy-id")
    credential = admin.call.call_args.kwargs["json"]["credentials"][0]
    self.assertNotIn("value", credential)
    self.assertIn("secretData", credential)


if __name__ == "__main__":
  unittest.main()
