import json
import stat
import tempfile
import unittest
from pathlib import Path

from account_database import AccountDatabase
from generate_oidc_mapping import build_mapping, read_accounts, write_mapping


class GenerateOidcMappingTest(unittest.TestCase):
  def test_all_accounts_including_disabled_and_admin_are_public_without_secrets(self):
    mapping = build_mapping([
      {"id": "user-b", "enabled": False, "passwordHash": "synthetic-hash", "micu": {"apiKey": "synthetic-key"}},
      {"id": "user-a", "role": "system_admin"},
    ], [{"id": "invite-a", "status": "pending", "inviteToken": "synthetic-token"},
        {"id": "invite-b", "status": "revoked"}])
    self.assertEqual(mapping, {"users": [
      {"localUserId": "user-a", "customerId": "public"},
      {"localUserId": "user-b", "customerId": "public"},
    ], "invitations": [{"invitationId": "invite-a", "customerId": "public"}]})

  def test_preserves_explicit_existing_public_subject(self):
    mapping = build_mapping([{"id": "user-a", "customerId": "public", "oidcSubject": "subject-a"}], [])
    self.assertEqual(mapping["users"][0]["subject"], "subject-a")

  def test_refuses_existing_business_customer_or_empty_database(self):
    for users, invitations in [
      ([], []),
      ([{"id": "user-a", "customerId": "business"}], []),
      ([{"id": "user-a"}], [{"id": "invite-a", "status": "pending", "customerId": "business"}]),
    ]:
      with self.subTest(users=users, invitations=invitations), self.assertRaises(ValueError):
        build_mapping(users, invitations)

  def test_private_output_and_no_overwrite(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "mapping.json"
      mapping = build_mapping([{"id": "user-a"}], [])
      write_mapping(path, mapping)
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
      self.assertEqual(json.loads(path.read_text()), mapping)
      with self.assertRaises(FileExistsError):
        write_mapping(path, {})
      self.assertEqual(json.loads(path.read_text()), mapping)
      link = Path(directory) / "link.json"
      link.symlink_to(path)
      with self.assertRaises(FileExistsError):
        write_mapping(link, {})

  def test_missing_sqlite_is_not_created(self):
    with tempfile.TemporaryDirectory() as directory:
      with self.assertRaises(ValueError):
        read_accounts("", directory)
      self.assertEqual(list(Path(directory).iterdir()), [])

  def test_existing_sqlite_reads_without_changing_accounts(self):
    with tempfile.TemporaryDirectory() as directory:
      db = AccountDatabase(Path(directory))
      db.save_user({"id": "user-a", "username": "synthetic-user", "passwordHash": "synthetic-hash"})
      db.save_pending({"id": "invite-a", "status": "pending"})
      before = db.load()
      users, invitations = read_accounts("", directory)
      self.assertEqual(build_mapping(users, invitations)["users"], [
        {"localUserId": "user-a", "customerId": "public"}])
      self.assertEqual(db.load(), before)
      db.engine.dispose()


if __name__ == "__main__":
  unittest.main()
