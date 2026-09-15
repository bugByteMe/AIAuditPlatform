from __future__ import annotations

import sys
import tempfile
import unittest
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
      self.assertEqual(path.stat().st_mode & 0o777, 0o600)

  def test_seed_is_copied_before_mutation(self) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
      seed = {"user": {"username": "user", "codex": {"apiKey": ""}}}
      store = AccountStore(Path(tempdir) / "accounts.json", seed)
      store.users["user"]["codex"]["apiKey"] = "changed"
      self.assertEqual(seed["user"]["codex"]["apiKey"], "")


if __name__ == "__main__":
  unittest.main()
