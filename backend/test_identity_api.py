from account_api_test_support import *


class IdentityApiTest(AccountApiTestBase):
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

