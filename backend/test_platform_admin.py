from account_api_test_support import *
from workspace_store import StorageError


class PlatformAdminTest(AccountApiTestBase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.store = AccountStore(Path(self.temp.name) / "accounts.json", {
      "root": {"id": "root", "username": "root", "role": "system_admin", "enabled": True},
      "member": {"id": "member", "username": "member", "role": "user", "enabled": True},
    })
    self.actor = {"id": "platform", "username": "platform", "role": "platform_admin"}
    for name, value in [("ACCOUNT_STORE", self.store), ("USERS", self.store.users)]:
      patcher = patch(f"server.{name}", value)
      patcher.start()
      self.addCleanup(patcher.stop)
    patcher = patch("server.add_audit")
    patcher.start()
    self.addCleanup(patcher.stop)

  def request(self, payload, role="platform_admin"):
    handler = self.handler(payload, {**self.actor, "role": role})
    handler.current_user = lambda: {**self.actor, "role": role}
    del handler.require_admin
    del handler.require_management
    return handler

  def test_management_permissions_fail_closed(self):
    for role in ["user", "group_admin", "unknown", "", None]:
      for method, args in [(Handler.accounts, ()), (Handler.create_group, ()),
                           (Handler.update_group, ("missing",)), (Handler.delete_group, ("missing",)),
                           (Handler.update_account, ("member",))]:
        with self.subTest(role=role, method=method.__name__):
          handler = self.request({}, role)
          with self.assertRaises(RequestStopped):
            method(handler, *args)
          self.assertEqual(handler.responses[0][1], 403)

  def test_platform_account_fields_are_checked_before_mutation(self):
    group = self.store.create_group("Example team")
    for field, value in {"role": "system_admin", "enabled": False, "displayName": "Other",
                         "maxSessions": 3, "password": "placeholder", "codexApiKey": "placeholder",
                         "unexpected": True}.items():
      with self.subTest(field=field), self.assertRaises(StorageError) as caught:
        Handler.update_account(self.request({"groupId": group["id"], field: value}), "member")
      self.assertEqual(caught.exception.code, "forbidden")
      self.assertFalse(self.store.users["member"].get("groupId"))
    Handler.update_account(self.request({"groupId": group["id"]}), "member")
    self.assertEqual(self.store.users["member"]["groupId"], group["id"])
    Handler.update_account(self.request({"groupId": ""}), "member")
    self.assertEqual(self.store.users["member"]["groupId"], "")

  def test_platform_group_fields_and_empty_group_deletion(self):
    handler = self.request({"name": "Example team", "liveRunLimit": 2})
    Handler.create_group(handler)
    group_id = handler.responses[0][0]["group"]["id"]
    for field in ["diskLimitBytes", "enabled", "unexpected"]:
      with self.subTest(field=field), self.assertRaises(StorageError):
        Handler.update_group(self.request({"name": "Changed", field: 0}), group_id)
      self.assertEqual(self.store.groups[group_id]["name"], "Example team")
      with self.assertRaises(StorageError):
        Handler.create_group(self.request({"name": "Rejected", field: 0}))
    Handler.update_group(self.request({"name": "Renamed", "liveRunLimit": 0}), group_id)
    self.assertEqual(self.store.groups[group_id]["liveRunLimit"], 0)
    with patch("server.SESSIONS", {}):
      Handler.delete_group(self.request({}), group_id)
    self.assertNotIn(group_id, self.store.groups)

  def test_system_admin_role_validation_and_persistence(self):
    Handler.update_account(self.request({"role": "platform_admin"}, "system_admin"), "member")
    self.assertEqual(public_user(self.store.users["member"])["role"], "platform_admin")
    reloaded = AccountStore(Path(self.temp.name) / "accounts.json", {})
    self.assertEqual(reloaded.users["member"]["role"], "platform_admin")
    with self.assertRaises(ValueError):
      Handler.update_account(self.request({"role": "unknown"}, "system_admin"), "member")
    with self.assertRaises(StorageError):
      Handler.update_account(self.request({"role": "user"}, "system_admin"), "root")

  def test_platform_cannot_use_system_only_endpoints(self):
    for method, args in [(Handler.create_account, ()), (Handler.create_account_batch, ()),
                         (Handler.delete_account, ("member",)), (Handler.revoke_invite, ("missing",)),
                         (Handler.reset_account_budget, ("member",)), (Handler.audit_logs, ()), (Handler.workers, ())]:
      with self.subTest(method=method.__name__), self.assertRaises(RequestStopped):
        method(self.request({}), *args)

  def test_platform_list_hides_invites_and_exact_balances(self):
    group, pending = self.store.create_batch(new_group_name="Invitations", count=1, budget_cny=0, max_sessions=1)
    workspace = MagicMock()
    workspace.usage_summaries.return_value = ({}, {})
    handler = self.request({})
    with patch("server.WORKSPACE_STORE", workspace), patch("server_admin.refresh_micu_balances") as refresh:
      Handler.accounts(handler)
    refresh.assert_not_called()
    for account in handler.responses[0][0]["accounts"]:
      self.assertNotIn("inviteToken", account)
      self.assertNotIn("initialBudgetCny", account)
      self.assertNotIn("remaining", account["budget"])

  def test_platform_cascade_preserves_admin_protections(self):
    group = self.store.create_group("Protected")
    Handler.update_account(self.request({"groupId": group["id"]}, "system_admin"), "root")
    with self.assertRaises(StorageError) as caught:
      Handler.delete_group(self.request({}), group["id"])
    self.assertEqual(caught.exception.code, "protected_admin")
    self.assertIn(group["id"], self.store.groups)

  def test_api_reports_forbidden_before_any_allowed_changes(self):
    group = self.store.create_group("Original")
    for path, payload in [
      ("/api/accounts/member", {"groupId": group["id"], "enabled": False}),
      (f"/api/groups/{group['id']}", {"name": "Changed", "diskLimitBytes": 0}),
    ]:
      handler = self.request(payload)
      handler.handle_api("PATCH", path)
      self.assertEqual(handler.responses[0][1], 403)
      self.assertEqual(handler.responses[0][0]["error"], "forbidden")
    self.assertEqual(self.store.groups[group["id"]]["name"], "Original")
    self.assertFalse(self.store.users["member"].get("groupId"))

  def test_create_platform_role_and_invalid_role_have_no_provider_side_effects(self):
    binding = {"status": "ready"}
    with patch("server_admin.provision_micu", return_value=binding) as provider:
      handler = self.request({"username": "example.platform", "password": "example-password", "role": "platform_admin"}, "system_admin")
      Handler.create_account(handler)
      self.assertEqual(handler.responses[0][1], 201)
      self.assertEqual(handler.responses[0][0]["account"]["role"], "platform_admin")
      provider.reset_mock()
      handler = self.request({"username": "example.invalid", "password": "example-password", "role": "unknown"}, "system_admin")
      handler.handle_api("POST", "/api/accounts")
      self.assertEqual(handler.responses[0][1], 400)
      provider.assert_not_called()

  def test_pending_account_membership_and_member_cascade(self):
    source, pending = self.store.create_batch(new_group_name="Source", count=1, budget_cny=0, max_sessions=1)
    target = self.store.create_group("Target")
    Handler.update_account(self.request({"groupId": target["id"]}), pending[0]["id"])
    self.assertEqual(self.store.pending_accounts[pending[0]["id"]]["groupId"], target["id"])
    Handler.update_account(self.request({"groupId": target["id"]}), "member")
    runtime = MagicMock()
    runtime.stop_and_wait_for_users.return_value = []
    runtime.delete_user_resources.return_value = {"workspaces": [], "sessionIds": [], "runIds": []}
    with patch("server.CHAT_RUNTIME", runtime), patch("server.SESSIONS", {}):
      Handler.delete_group(self.request({}), target["id"])
    self.assertNotIn("member", self.store.users)
    self.assertNotIn(pending[0]["id"], self.store.pending_accounts)
    self.assertIn(source["id"], self.store.groups)
    runtime.stop_and_wait_for_users.assert_called_once()
