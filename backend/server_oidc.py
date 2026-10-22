"""OIDC HTTP flows; passwords and provider tokens never enter public responses."""
import hashlib
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from identity_config import IDENTITY
from identity_database import IdentityDatabase
from identity_service import IdentityAuthority, IdentityGateway, canonical_username
from oidc_client import IdentityError, IdentityNameTaken, OidcClient

_lock = threading.RLock()
_flows = {}
_rates = {}
_services = None


def services():
  global _services
  from server_state import ACCOUNT_STORE, provision_micu
  with _lock:
    if _services is None or _services[0] is not ACCOUNT_STORE:
      database = IdentityDatabase(ACCOUNT_STORE.database.engine)
      ACCOUNT_STORE.identity_db = database
      authority = IdentityAuthority(IDENTITY, database, provision_micu, lambda: list(ACCOUNT_STORE.users.values()))
      gateway = IdentityGateway(IDENTITY, authority)
      _services = (ACCOUNT_STORE, database, authority, gateway, OidcClient(IDENTITY))
  return _services


def validate_identity_startup():
  from server_state import ACCOUNT_STORE
  IDENTITY.validate(ACCOUNT_STORE.groups)
  if IDENTITY.enabled:
    _, database, _, _, _ = services()
    if not IDENTITY.customer_id and not database.get(database.customers, "public"):
      database.put(database.customers, "public", {"id": "public", "name": "Public", "enabled": True})


def check_admission(identity):
  if not isinstance(identity.get("customerId"), str) or not identity["customerId"]:
    raise IdentityError("Identity has no customer assignment")
  if IDENTITY.customer_id and identity["customerId"] != IDENTITY.customer_id:
    raise IdentityError("This platform is restricted to another customer")


def rate_limit(key, limit=10):
  with _lock:
    now = time.time()
    # Bound memory even when clients continually change source addresses.
    for stale in [k for k, values in _rates.items() if not values or values[-1] < now - 300]:
      _rates.pop(stale, None)
    hits = [stamp for stamp in _rates.get(key, []) if stamp > now - 300]
    if len(hits) >= limit or len(_rates) > 10000:
      raise IdentityError("Too many identity requests; try again later")
    _rates[key] = hits + [now]


def revalidate_session(session, user):
  from server_state import REGISTRATION_LOCKS
  with REGISTRATION_LOCKS.hold("oidc-session:" + str(session.get("username"))):
    return _revalidate_session(session, user)


def _revalidate_session(session, user):
  if not IDENTITY.enabled:
    return True
  if not user or user.get("authSource") != "oidc" or not user.get("enabled", True) or user.get("status") != "active":
    return False
  if user.get("oidcIssuer") != IDENTITY.issuer:
    return False
  try:
    check_admission(user)
    if session.get("identityCheckedAt", 0) + IDENTITY.revalidate_seconds > time.time():
      return True
    _, _, _, gateway, oidc = services()
    tokens = session.get("oidcTokens")
    if not tokens:
      return False
    refreshed = oidc.tokens({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    info = oidc.userinfo(refreshed["access_token"])
    if info.get("sub") != user["oidcSubject"] or info.get("customerId") != user["customerId"]:
      return False
    binding = gateway.binding(user["oidcSubject"])
    check_admission(binding)
    if binding["username"] != user["username"]:
      return False
    if binding["customerId"] != user["customerId"]:
      return False
    user["micu"] = binding["micu"]
    session.update(oidcTokens=refreshed, identityCheckedAt=time.time())
    return True
  except (IdentityError, KeyError):
    return False


class OidcHandlerMixin:
  def oidc_login(self, query=""):
    if not IDENTITY.enabled:
      raise IdentityError("OIDC is disabled")
    _, _, _, _, oidc = services()
    address = getattr(getattr(self, "request", None), "client", None)
    rate_limit("login:" + str(getattr(address, "host", "local")), 30)
    nonce, verifier, state = (secrets.token_urlsafe(32) for _ in range(3))
    browser = secrets.token_urlsafe(32)
    invite = (parse_qs(query).get("inviteToken") or [""])[0]
    with _lock:
      for key in [key for key, flow in _flows.items() if flow["expires"] < time.time()]:
        _flows.pop(key, None)
      if len(_flows) >= 10000:
        raise IdentityError("Too many login attempts")
      _flows[state] = {"nonce": nonce, "verifier": verifier, "browser": browser,
        "expires": time.time() + 600, "invite": invite}
    self.write_binary(b"", "text/plain", HTTPStatus.FOUND, headers={"Location": oidc.authorization_url(state, nonce, verifier),
      "Set-Cookie": "ai_audit_oidc=" + browser + "; HttpOnly; Secure; SameSite=Lax; Path=/api/oidc; Max-Age=600",
      "Referrer-Policy": "no-referrer"}, no_store=True)

  def oidc_callback(self, query):
    from server_state import SESSION_COOKIE, SESSION_TTL_SECONDS, SESSIONS, SESSION_LOCK, create_user_session
    values = parse_qs(query)
    state = (values.get("state") or [""])[0]
    cookie = SimpleCookie()
    cookie.load(self.headers.get("Cookie", ""))
    browser = cookie.get("ai_audit_oidc")
    with _lock:
      flow = _flows.pop(state, None)
    if not flow or flow["expires"] < time.time() or not browser or not secrets.compare_digest(flow["browser"], browser.value):
      raise IdentityError("Login expired or invalid; sign in again")
    store, _, _, gateway, oidc = services()
    tokens, info = oidc.exchange((values.get("code") or [""])[0], flow["verifier"], flow["nonce"])
    check_admission(info)
    binding = gateway.binding(info["sub"])
    check_admission(binding)
    if binding["customerId"] != info["customerId"]:
      raise IdentityError("Customer claim mismatch")
    user = store.admit_identity(IDENTITY.issuer, binding, IDENTITY.default_group, flow["invite"])
    token, error = create_user_session(user, "OIDC login success")
    if not token:
      raise IdentityError(error)
    with SESSION_LOCK:
      SESSIONS[token].update(oidcTokens=tokens, identityCheckedAt=time.time())
    self.write_binary(b"", "text/plain", HTTPStatus.FOUND, headers={"Location": IDENTITY.public_url + "/",
      "Set-Cookie": f"{SESSION_COOKIE}={token}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={SESSION_TTL_SECONDS}",
      "Referrer-Policy": "no-referrer"}, no_store=True)

  def oidc_register(self):
    if IDENTITY.registration_paused:
      raise IdentityError("Registration is temporarily paused")
    payload = self.read_json()
    origin = self.headers.get("Origin")
    if origin and origin != IDENTITY.public_url:
      raise IdentityError("Registration origin mismatch")
    # Proxy-derived addresses are untrusted; deployments should also rate-limit at Nginx.
    address = getattr(getattr(self, "request", None), "client", None)
    rate_limit("register:" + str(getattr(address, "host", "local")))
    username = canonical_username(payload.get("username", ""))
    password = str(payload.get("password") or "")
    from config import SETTINGS
    if not SETTINGS.registration_min_password_length <= len(password) <= 1024:
      raise IdentityError("Password length is outside the allowed range")
    token = str(payload.get("inviteToken") or "")
    store, _, _, gateway, _ = services()
    from server_state import REGISTRATION_LOCKS
    with REGISTRATION_LOCKS.hold("oidc-invite:" + hashlib.sha256(token.encode()).hexdigest()):
      digest = hashlib.sha256(token.encode()).hexdigest()
      activated = next((u for u in store.users.values() if u.get("activationInviteDigest") == digest
        and u["username"] == username and u.get("authSource") == "oidc"), None)
      if activated:
        self.write_json({"redirectUrl": "/api/oidc/login"})
        return
      pending = store.reserve_identity_invite(token, username)
      customer = pending.get("customerId", "")
      check_admission({"customerId": customer})
      operation = "invite:" + pending["id"]
      try:
        result = gateway.provision(operation, username, password, customer, pending["initialBudgetCny"])
      except IdentityNameTaken:
        store.release_identity_invite(token, username)
        raise
      pending.update(oidcSubject=result["sub"])
      store.pending_accounts[pending["id"]].update(pending)
      store.save_pending(pending)
      store.admit_identity(IDENTITY.issuer, result, IDENTITY.default_group, token)
    # No session is issued based solely on the registration request.
    self.write_json({"redirectUrl": "/api/oidc/login"})

  def identity_customers(self, method):
    from server_state import add_audit
    actor = self.require_management()
    if not IDENTITY.enabled:
      raise IdentityError("OIDC is disabled")
    if IDENTITY.customer_id:
      if method != "GET":
        raise IdentityError("Customers are managed on the main platform")
      self.write_json({"customers": [{"id": IDENTITY.customer_id, "name": IDENTITY.customer_id}]})
      return
    _, db, _, _, _ = services()
    if method == "POST":
      import re
      payload = self.read_json()
      identifier = str(payload.get("id") or "")
      name = str(payload.get("name") or "").strip()
      if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", identifier) or not name or len(name) > 128:
        raise IdentityError("Invalid customer ID or name")
      if db.get(db.customers, identifier):
        raise IdentityError("Customer already exists")
      db.put(db.customers, identifier, {"id": identifier, "name": name, "enabled": True})
      add_audit(actor["username"], "customer created", identifier)
    self.write_json({"customers": db.customer_list()})

  def identity_internal(self, action):
    if not IDENTITY.enabled or IDENTITY.customer_id:
      raise IdentityError("Central identity service is unavailable")
    _, db, authority, _, _ = services()
    client, config = authority.authenticate_client(self.headers.get("Authorization", ""))
    rate_limit("client:" + client, 100)
    payload = self.read_json()
    customer = config["customerId"]
    db.require_customer(customer)
    if action == "binding":
      binding = authority.binding(str(payload.get("sub") or ""), customer)
      self.write_json(binding)
      return
    if action != "provision" or payload.get("customerId") != customer:
      raise IdentityError("Invalid provisioning request")
    if IDENTITY.registration_paused:
      raise IdentityError("Registration is temporarily paused")
    from micu_api import parse_cny
    from config import SETTINGS
    password = str(payload.get("password") or "")
    if not SETTINGS.registration_min_password_length <= len(password) <= 1024:
      raise IdentityError("Invalid password length")
    budget = parse_cny(payload.get("initialBudgetCny", "0"))
    if budget > parse_cny(config.get("maxInitialBudgetCny", "0")):
      raise IdentityError("Initial budget exceeds client allowance")
    operation = str(payload.get("operation") or "")
    if not operation or len(operation) > 180:
      raise IdentityError("Invalid operation ID")
    result = authority.provision(client + ":" + operation, payload.get("username", ""), password, customer, format(budget, ".2f"))
    self.write_json(result)
