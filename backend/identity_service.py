"""Central identity authority. All remote clients have a fixed customer scope."""
import hmac
import re

from oidc_client import IdentityError, IdentityNameTaken, KeycloakAdmin, customer_of, identity_username, request_json
from identity_common import UNIVERSAL_CUSTOMER_ID, UNIVERSAL_SEED_USERNAMES, customer_allowed


def canonical_username(value):
  username = str(value).strip().casefold()
  if not username or len(username) > 128 or any(c.isspace() or ord(c) < 32 or c in "/\\<>\"'" for c in username):
    raise IdentityError("Username must use 1-128 non-whitespace characters without path separators or quotes")
  return username


class IdentityAuthority:
  def __init__(self, settings, database, provision_micu, profiles=None):
    self.settings = settings
    self.db = database
    self.admin = KeycloakAdmin(settings)
    self.provision_micu = provision_micu
    self.profiles = profiles or (lambda: [])

  def authenticate_client(self, authorization):
    if not authorization.startswith("Bearer "):
      raise IdentityError("Invalid identity service credentials")
    token = authorization.removeprefix("Bearer ")
    for client_id, config in self.settings.clients.items():
      expected = str(config.get("token") or "")
      if expected and hmac.compare_digest(expected, token):
        if not config.get("customerId") or config["customerId"] == "public":
          raise IdentityError("Dedicated client requires a business customer scope")
        return client_id, config
    raise IdentityError("Invalid identity service credentials")

  def provision(self, operation, username, password, customer, initial_budget):
    username = canonical_username(username)
    if customer == UNIVERSAL_CUSTOMER_ID:
      raise IdentityError("The universal customer is reserved for platform seed identities")
    self.db.require_customer(customer)
    fingerprint = {"username": username, "customerId": customer, "initialBudgetCny": str(initial_budget)}
    with self.db.exclusive("operation:" + operation), self.db.exclusive("provision:" + username):
      record = self.db.get(self.db.operations, operation)
      if record and record["request"] != fingerprint:
        raise IdentityError("Provisioning retry does not match the original request")
      if record and record.get("result"):
        return record["result"]
      if record is None:
        from sqlalchemy import select
        with self.db.engine.connect() as connection:
          reserved = connection.execute(select(self.db.bindings.c.subject).where(
            self.db.bindings.c.username_key == username)).first()
        if reserved:
          raise IdentityNameTaken("Username is reserved by an existing shared binding; contact an administrator")
        record = {"request": fingerprint}
        self.db.put(self.db.operations, operation, record)
      try:
        identity = self.admin.create(username, password, customer, operation)
      except IdentityNameTaken:
        if record.get("subject"):
          raise IdentityError("Interrupted provisioning needs administrator review") from None
        with self.db.engine.begin() as connection:
          connection.execute(self.db.operations.delete().where(self.db.operations.c.id == operation))
        raise
      subject = identity["id"]
      if not identity.get("enabled") or customer_of(identity) != customer:
        raise IdentityError("Identity is disabled or customer does not match")
      record["subject"] = subject
      self.db.put(self.db.operations, operation, record)
      # ensure_binding reuses an exact-name token without adding the initial credit twice.
      binding = self.provision_micu(username, initial_budget)
      result = {"sub": subject, "username": username, "customerId": customer, "micu": binding}
      self.db.put(self.db.bindings, subject, result, username_key=username.casefold())
      record["result"] = result
      self.db.put(self.db.operations, operation, record)
      return result

  def universal_seed(self, username, initial_budget):
    username = canonical_username(username)
    if username not in UNIVERSAL_SEED_USERNAMES:
      raise IdentityError("Unknown universal seed identity")
    self.db.require_customer(UNIVERSAL_CUSTOMER_ID)
    with self.db.exclusive("universal-seed:" + username):
      identity = self.admin.find(username)
      if (not identity or not identity.get("enabled") or identity_username(identity) != username
          or customer_of(identity) != UNIVERSAL_CUSTOMER_ID):
        raise IdentityError("Universal seed Keycloak identity is missing, disabled, or incorrectly assigned")
      subject = str(identity.get("id") or "")
      if not subject:
        raise IdentityError("Universal seed Keycloak identity has no subject")
      result = self.db.get(self.db.bindings, subject)
      if result:
        if (result.get("username") != username or result.get("customerId") != UNIVERSAL_CUSTOMER_ID
            or not isinstance(result.get("micu"), dict)):
          raise IdentityError("Universal seed binding conflicts with the Keycloak identity")
        return result
      binding = self.provision_micu(username, initial_budget)
      result = {"sub": subject, "username": username, "customerId": UNIVERSAL_CUSTOMER_ID, "micu": binding}
      self.db.put(self.db.bindings, subject, result, username_key=username.casefold())
      return result

  def binding(self, subject, customer=""):
    identity = self.admin.user(subject)
    actual_customer = customer_of(identity)
    self.db.require_customer(actual_customer)
    if not identity.get("enabled") or (customer and not customer_allowed(actual_customer, customer)):
      raise IdentityError("Identity is disabled or customer does not match")
    result = self.db.get(self.db.bindings, subject)
    if not result or result["username"] != identity_username(identity) or result["customerId"] != actual_customer:
      raise IdentityError("Identity binding missing or changed; administrator action required")
    # Main-platform recharge updates its persisted baseline; expose that same baseline everywhere.
    local = next((u for u in self.profiles() if u.get("oidcSubject") == subject
      and u.get("oidcIssuer") == self.settings.issuer), None)
    if local and (local.get("micu") or {}).get("tokenId") == result["micu"].get("tokenId"):
      result = {**result, "micu": local["micu"]}
    return result


class IdentityGateway:
  def __init__(self, settings, authority):
    self.settings, self.authority = settings, authority

  def call(self, action, payload):
    return request_json("POST", self.settings.central_url + "/api/internal/identity/" + action,
      headers={"Authorization": "Bearer " + self.settings.central_token}, json=payload)

  def provision(self, operation, username, password, customer, budget):
    if self.settings.customer_id:
      return self.call("provision", {"operation": operation, "username": username,
        "password": password, "customerId": customer, "initialBudgetCny": str(budget)})
    return self.authority.provision(operation, username, password, customer, budget)

  def binding(self, subject):
    if self.settings.customer_id:
      return self.call("binding", {"sub": subject})
    return self.authority.binding(subject)

  def universal_seed(self, username, budget):
    if self.settings.customer_id:
      return self.call("universal-seed", {"username": username})
    return self.authority.universal_seed(username, budget)
