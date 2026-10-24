import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from account_store import AccountStore
from identity_database import IdentityDatabase
from identity_config import IdentitySettings
from identity_policy import invitation_customer, guard_identity_route
from identity_common import UNIVERSAL_CUSTOMER_ID
from oidc_client import IdentityError, IdentityNameTaken, OidcClient, request_json
import server_oidc


class OidcFlowTest(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.addCleanup(self.directory.cleanup)
    self.store = AccountStore(Path(self.directory.name) / "accounts.json", {})
    self.db = IdentityDatabase(self.store.database.engine)
    self.store.identity_db = self.db
    self.group = self.store.create_group("Default")
    self.settings = SimpleNamespace(enabled=True, auth_mode="oidc", customer_id="business",
      issuer="https://identity.example.test/realms/audit", public_url="https://app.example.test",
      client_id="app", client_secret="synthetic-client-secret", default_group=self.group["id"],
      registration_paused=False, revalidate_seconds=300)
    self.identity = {"sub": "subject-1", "username": "alice", "customerId": "business",
      "micu": {"tokenId": 17, "apiKey": "synthetic-key"}}
    self.gateway = MagicMock()
    self.gateway.provision.return_value = self.identity
    self.gateway.binding.return_value = self.identity
    self.oidc = MagicMock()
    self.oidc.authorization_url.return_value = "https://identity.example.test/login"
    self.stack = ExitStack()
    self.addCleanup(self.stack.close)
    self.stack.enter_context(patch("server_oidc.IDENTITY", self.settings))
    self.stack.enter_context(patch("server_oidc.services", return_value=(self.store, self.db, None, self.gateway, self.oidc)))
    self.stack.enter_context(patch.dict(server_oidc._flows, {}, clear=True))
    self.stack.enter_context(patch.dict(server_oidc._rates, {}, clear=True))

  def handler(self, payload=None):
    return SimpleNamespace(read_json=lambda: payload or {}, headers={},
      request=SimpleNamespace(client=SimpleNamespace(host="synthetic-client")),
      write_json=MagicMock(), write_binary=MagicMock())

  def test_registration_uses_invitation_customer_not_public_payload(self):
    pending = self.store.create_batch(group_id=self.group["id"], count=1,
      budget_cny="1.00", max_sessions=0, customer_id="business")[1][0]
    handler = self.handler({"inviteToken": pending["inviteToken"], "username": "Alice",
      "password": "synthetic-password", "customerId": "attacker", "role": "system_admin", "groupId": "attacker"})
    server_oidc.OidcHandlerMixin.oidc_register(handler)
    self.gateway.provision.assert_called_once_with("invite:" + pending["id"], "alice", "synthetic-password", "business", "1.00")
    response = handler.write_json.call_args.args[0]
    self.assertEqual(response, {"redirectUrl": "/api/oidc/login"})
    self.assertNotIn("sessionToken", response)
    self.assertEqual(self.store.users["alice"]["maxSessions"], 0)
    self.assertEqual(self.store.users["alice"]["role"], "user")
    serialized = json.dumps(self.store.state())
    self.assertNotIn("synthetic-password", serialized)
    self.assertNotIn("passwordHash", self.store.users["alice"])
    server_oidc.OidcHandlerMixin.oidc_register(handler)
    self.assertEqual(self.gateway.provision.call_count, 1)

  def test_registration_failure_keeps_durable_reservation_for_retry(self):
    pending = self.store.create_batch(group_id=self.group["id"], count=1,
      budget_cny="1", max_sessions=1, customer_id="business")[1][0]
    self.gateway.provision.side_effect = IdentityError("Provider unavailable")
    handler = self.handler({"inviteToken": pending["inviteToken"], "username": "alice", "password": "synthetic-password"})
    with self.assertRaises(IdentityError):
      server_oidc.OidcHandlerMixin.oidc_register(handler)
    self.assertEqual(self.store.pending_accounts[pending["id"]]["status"], "provisioning")
    self.assertFalse(self.store.users)

  def test_confirmed_username_conflict_releases_invitation(self):
    pending = self.store.create_batch(group_id=self.group["id"], count=1,
      budget_cny="1", max_sessions=1, customer_id="business")[1][0]
    self.gateway.provision.side_effect = IdentityNameTaken("Username already exists")
    handler = self.handler({"inviteToken": pending["inviteToken"], "username": "alice", "password": "synthetic-password"})
    with self.assertRaises(IdentityNameTaken):
      server_oidc.OidcHandlerMixin.oidc_register(handler)
    self.assertEqual(self.store.pending_accounts[pending["id"]]["status"], "pending")
    self.store.reserve_identity_invite(pending["inviteToken"], "another")

  def test_customer_mismatch_and_disabled_local_profile_deny_sessions(self):
    server_oidc.check_admission({"customerId": UNIVERSAL_CUSTOMER_ID})
    with self.assertRaises(IdentityError):
      server_oidc.check_admission({"customerId": "another"})
    user = self.store.admit_identity(self.settings.issuer, self.identity, self.group["id"])
    user["enabled"] = False
    self.assertFalse(server_oidc._revalidate_session({}, user))

  def test_session_revalidation_rejects_changed_membership(self):
    user = self.store.admit_identity(self.settings.issuer, self.identity, self.group["id"])
    self.oidc.tokens.return_value = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}
    self.oidc.userinfo.return_value = {"sub": "subject-1", "customerId": "another"}
    self.assertFalse(server_oidc._revalidate_session({"oidcTokens": {"refresh_token": "synthetic-refresh"}}, user))

  def test_session_revalidation_fails_closed_when_provider_unavailable(self):
    user = self.store.admit_identity(self.settings.issuer, self.identity, self.group["id"])
    self.oidc.tokens.side_effect = IdentityError("Unavailable")
    self.assertFalse(server_oidc._revalidate_session({"oidcTokens": {"refresh_token": "synthetic-refresh"}}, user))

  def test_login_sets_browser_bound_state_cookie(self):
    handler = self.handler()
    server_oidc.OidcHandlerMixin.oidc_login(handler)
    headers = handler.write_binary.call_args.kwargs["headers"]
    self.assertIn("HttpOnly; Secure; SameSite=Lax", headers["Set-Cookie"])
    flow = next(iter(server_oidc._flows.values()))
    self.assertIn(flow["browser"], headers["Set-Cookie"])
    self.assertGreaterEqual(len(flow["verifier"]), 43)

  def test_callback_rejects_missing_browser_cookie_and_consumes_state(self):
    server_oidc._flows["state"] = {"browser": "browser", "expires": time.time() + 60}
    with self.assertRaisesRegex(IdentityError, "invalid"):
      server_oidc.OidcHandlerMixin.oidc_callback(self.handler(), "state=state&code=synthetic")
    self.assertNotIn("state", server_oidc._flows)
    self.oidc.exchange.assert_not_called()

  def test_dedicated_invitation_customer_is_fixed(self):
    with patch("identity_policy.IDENTITY", self.settings):
      self.assertEqual(invitation_customer({}, {"role": "system_admin"}), "business")
      with self.assertRaises(Exception):
        invitation_customer({"customerId": "another"}, {"role": "system_admin"})
      with self.assertRaises(Exception):
        invitation_customer({"customerId": UNIVERSAL_CUSTOMER_ID}, {"role": "system_admin"})

  def test_universal_cannot_be_configured_as_dedicated_customer(self):
    with patch.dict("os.environ", {
        "AI_AUDIT_AUTH_MODE": "oidc", "AI_AUDIT_OIDC_ISSUER": self.settings.issuer,
        "AI_AUDIT_PUBLIC_URL": self.settings.public_url, "AI_AUDIT_OIDC_CLIENT_ID": "app",
        "AI_AUDIT_OIDC_CLIENT_SECRET": "secret", "AI_AUDIT_OIDC_DEFAULT_GROUP_ID": self.group["id"],
        "AI_AUDIT_DEPLOYMENT_CUSTOMER_ID": UNIVERSAL_CUSTOMER_ID,
        "AI_AUDIT_IDENTITY_CENTRAL_URL": "https://central.example.test",
        "AI_AUDIT_IDENTITY_CENTRAL_TOKEN": "x" * 32,
      }, clear=True):
      with self.assertRaisesRegex(ValueError, "reserved universal"):
        IdentitySettings().validate(self.store.groups)

  def test_dedicated_recharge_routes_are_blocked(self):
    with patch("identity_policy.IDENTITY", self.settings):
      for path in ("/api/recharge/orders", "/api/accounts/alice/reset-budget", "/api/admin/recharge-imports"):
        with self.assertRaises(Exception):
          guard_identity_route(self.handler(), "POST", path)
      with self.assertRaises(Exception):
        guard_identity_route(self.handler(), "GET", "/api/recharge/orders/old-order")

  def test_dedicated_balances_use_micu_management_client_directly(self):
    from server_state import check_micu_budget, refresh_micu_balance
    user = self.store.admit_identity(self.settings.issuer, self.identity, self.group["id"])
    provider = MagicMock()
    provider.balance.return_value = {"remainingCny": "10.00", "remainingPercent": 100,
      "referenceQuota": 5000000, "status": "ready"}
    with patch("server_state.MICU_CLIENT", provider), patch("server_state.ACCOUNT_STORE", self.store):
      check_micu_budget(user)
      refresh_micu_balance(user)
    self.assertEqual(provider.balance.call_count, 2)
    self.gateway.call.assert_not_called()

  def test_workspace_customer_scope_applies_before_admin_bypass(self):
    from workspace_core import WorkspaceCoreMixin
    actor = {"username": "admin", "role": "platform_admin", "customerId": "business"}
    owners = {"alice": {"customerId": "business"}, "bob": {"customerId": "another"}}
    store = SimpleNamespace(account_provider=lambda: (owners, {}))
    with patch("identity_config.IDENTITY", self.settings):
      self.assertTrue(WorkspaceCoreMixin.user_can_access(store, actor, {"owner": "alice"}))
      self.assertFalse(WorkspaceCoreMixin.user_can_access(store, actor, {"owner": "bob"}))
      universal = {"username": "seed", "role": "platform_admin", "customerId": UNIVERSAL_CUSTOMER_ID}
      owners["seed"] = universal
      self.assertTrue(WorkspaceCoreMixin.user_can_access(store, universal, {"owner": "alice"}))
      self.assertTrue(WorkspaceCoreMixin.user_can_access(store, universal, {"owner": "seed"}))

  def test_origin_check_rejects_mutating_browser_requests_from_other_sites(self):
    handler = self.handler()
    handler.headers = {"Origin": "https://another.example.test"}
    with patch("identity_policy.IDENTITY", self.settings), self.assertRaises(Exception):
      guard_identity_route(handler, "POST", "/api/customers")

  def test_oidc_cors_does_not_expose_credentials_to_other_origins(self):
    from server_asgi import cors_headers
    with patch("identity_policy.IDENTITY", self.settings):
      self.assertEqual(cors_headers(SimpleNamespace(headers={"origin": "https://another.example.test"})), {})
      headers = cors_headers(SimpleNamespace(headers={"origin": self.settings.public_url}))
      self.assertEqual(headers["Access-Control-Allow-Origin"], self.settings.public_url)

  def test_provisioning_invitation_cannot_be_deleted(self):
    from server_admin import AdminHandlerMixin
    pending = self.store.create_batch(group_id=self.group["id"], count=1,
      budget_cny="1", max_sessions=1, customer_id="business")[1][0]
    self.store.reserve_identity_invite(pending["inviteToken"], "alice")
    with patch("server_admin.IDENTITY", self.settings), patch("server_admin.ACCOUNT_STORE", self.store), self.assertRaises(Exception):
      AdminHandlerMixin.validate_admin_deletion(self.handler(), {"id": "admin"}, {pending["id"]})

  def test_provider_errors_do_not_expose_response_bodies(self):
    response = SimpleNamespace(status_code=400, content=b"synthetic-private-password")
    with patch("oidc_client.httpx.request", return_value=response):
      with self.assertRaises(IdentityError) as caught:
        request_json("POST", "https://identity.example.test")
    self.assertNotIn("synthetic-private-password", str(caught.exception))


class OidcTokenTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

  def client(self, changes=None):
    settings = SimpleNamespace(issuer="https://identity.example.test/realms/audit", client_id="app",
      client_secret="synthetic-secret", public_url="https://app.example.test")
    client = OidcClient(settings)
    client._metadata = {"authorization_endpoint": settings.issuer + "/auth", "token_endpoint": settings.issuer + "/token"}
    client._jwks = MagicMock()
    client._jwks.get_signing_key_from_jwt.return_value.key = self.key.public_key()
    claims = {"iss": settings.issuer, "sub": "subject", "aud": "app", "iat": int(time.time()),
      "exp": int(time.time()) + 300, "nonce": "nonce"}
    claims.update(changes or {})
    token = jwt.encode(claims, self.key, algorithm="RS256")
    client.tokens = MagicMock(return_value={"id_token": token, "access_token": "synthetic-access"})
    client.userinfo = MagicMock(return_value={"sub": "subject", "customerId": "business"})
    return client

  def test_valid_signed_code_exchange(self):
    client = self.client()
    _, info = client.exchange("code", "verifier", "nonce")
    self.assertEqual(info["sub"], "subject")
    self.assertEqual(client.tokens.call_args.args[0]["code_verifier"], "verifier")

  def test_invalid_nonce_issuer_audience_and_expiration_are_rejected(self):
    for changes in ({"nonce": "wrong"}, {"iss": "https://wrong.example.test"}, {"aud": "other"},
        {"exp": 1}, {"azp": "other"}, {"aud": ["app", "other"]}):
      with self.subTest(changes=changes), self.assertRaises(IdentityError):
        self.client(changes).exchange("code", "verifier", "nonce")

  def test_userinfo_subject_must_match_verified_id_token(self):
    client = self.client()
    client.userinfo.return_value = {"sub": "other"}
    with self.assertRaisesRegex(IdentityError, "subject mismatch"):
      client.exchange("code", "verifier", "nonce")

  def test_authorization_uses_pkce_s256_and_exact_callback(self):
    client = self.client()
    query = parse_qs(urlparse(client.authorization_url("state", "nonce", "verifier")).query)
    self.assertEqual(query["code_challenge_method"], ["S256"])
    self.assertEqual(query["redirect_uri"], ["https://app.example.test/api/oidc/callback"])
    self.assertNotIn("client_secret", query)


if __name__ == "__main__":
  unittest.main()
