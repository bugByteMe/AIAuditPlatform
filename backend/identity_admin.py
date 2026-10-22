"""Main-only central identity administration, separate from local account CRUD."""
from copy import deepcopy
from urllib.parse import quote

from identity_config import IDENTITY
from oidc_client import IdentityError, identity_username
from workspace_store import StorageError


def require_central_admin(handler):
  actor = handler.require_management()
  if not IDENTITY.enabled or IDENTITY.customer_id:
    raise StorageError("forbidden", "Central identity administration is available on main only")
  return actor


def full_delete(handler, identifier):
  actor = require_central_admin(handler)
  # Same stronger permission as existing local deletion.
  handler.require_admin()
  from server_state import ACCOUNT_STORE, add_audit
  from server_oidc import services
  user = ACCOUNT_STORE.user_by_identifier(identifier)
  if not user or not user.get("oidcSubject") or user.get("oidcIssuer") != IDENTITY.issuer:
    raise IdentityError("An explicitly linked central identity is required")
  handler.validate_admin_deletion(actor, {user["id"]})
  if handler.read_json().get("confirmSubject") != user["oidcSubject"]:
    raise IdentityError("Full deletion requires confirmation of the central subject")
  _, db, authority, _, _ = services()
  with ACCOUNT_STORE.lock, db.exclusive("admit:" + user["oidcSubject"]):
    # Fail closed if remote deletion succeeds but subsequent local cleanup fails.
    user["enabled"] = False
    ACCOUNT_STORE.save_user(user)
    handler.invalidate_user_sessions({user["username"]})
    authority.admin.call("DELETE", "/users/" + quote(user["oidcSubject"], safe=""), accepted_statuses=(404,))
    add_audit(actor["username"], "central identity deleted", user["id"])
    # The regular path handles resources, sessions, link removal, and local audit.
    handler.delete_account(user["id"])


def change_customer(handler, identifier):
  actor = require_central_admin(handler)
  from server_state import ACCOUNT_STORE, add_audit
  from server_oidc import services
  user = ACCOUNT_STORE.user_by_identifier(identifier)
  if not user or not user.get("oidcSubject") or user.get("oidcIssuer") != IDENTITY.issuer:
    raise IdentityError("An explicitly linked central identity is required")
  payload = handler.read_json()
  if set(payload) != {"customerId"}:
    raise IdentityError("Only customerId may be changed through this endpoint")
  customer = str(payload["customerId"])
  _, db, authority, _, _ = services()
  db.require_customer(customer)
  with ACCOUNT_STORE.lock, db.exclusive("admit:" + user["oidcSubject"]):
    central = authority.admin.user(user["oidcSubject"])
    binding = db.get(db.bindings, user["oidcSubject"])
    if identity_username(central) != user["username"] or not binding or binding["username"] != user["username"]:
      raise IdentityError("Central identity binding does not match local account")
    attributes = deepcopy(central.get("attributes") or {})
    attributes["customerId"] = [customer]
    authority.admin.call("PUT", "/users/" + quote(user["oidcSubject"], safe=""), json={"attributes": attributes})
    # A remote-success/local-failure leaves login fail-closed; retry repairs it.
    db.put(db.bindings, user["oidcSubject"], {**binding, "customerId": customer}, username_key=user["username"].casefold())
    user["customerId"] = customer
    ACCOUNT_STORE.save_user(user)
    handler.invalidate_user_sessions({user["username"]})
    add_audit(actor["username"], "central customer changed", user["id"])
    handler.write_json({"customerId": customer})
