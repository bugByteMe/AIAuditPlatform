#!/usr/bin/env python3
"""Offline, explicit identity migration. Dry-run unless --apply is supplied."""
import argparse
import base64
import json
from copy import deepcopy
from pathlib import Path

from account_store import AccountStore
from config import SETTINGS
from identity_config import IDENTITY
from identity_database import IdentityDatabase
from identity_service import IdentityGateway
from oidc_client import IdentityError, KeycloakAdmin, customer_of, identity_username


def imported_credential(encoded):
  try:
    algorithm, iterations, salt, digest = encoded.split("$")
    salt_bytes, digest_bytes = bytes.fromhex(salt), bytes.fromhex(digest)
    if algorithm != "pbkdf2_sha256" or int(iterations) <= 0 or not salt_bytes or len(digest_bytes) != 32:
      raise ValueError()
  except (ValueError, AttributeError):
    raise IdentityError("Unsupported stored password credential") from None
  return {"type": "password", "temporary": False,
    "credentialData": json.dumps({"algorithm": "pbkdf2-sha256", "hashIterations": int(iterations)}),
    "secretData": json.dumps({"salt": base64.b64encode(salt_bytes).decode(),
      "value": base64.b64encode(digest_bytes).decode()})}


def preflight(store, mapping):
  rows = mapping.get("users", [])
  identifiers = [str(row.get("localUserId") or "") for row in rows]
  if len(identifiers) != len(set(identifiers)) or set(identifiers) != {u["id"] for u in store.users.values()}:
    raise IdentityError("Mapping must contain each local user ID exactly once")
  subjects = [row["subject"] for row in rows if row.get("subject")]
  if len(subjects) != len(set(subjects)):
    raise IdentityError("A central subject cannot map to multiple local accounts")
  token_ids = set()
  for row in rows:
    user = store.user_by_identifier(row["localUserId"])
    if user.get("oidcSubject") and row.get("subject", user["oidcSubject"]) != user["oidcSubject"]:
      raise IdentityError("A linked local profile cannot be silently rebound")
    if not row.get("customerId"):
      raise IdentityError("Every identity needs an explicitly reviewed customerId")
    if IDENTITY.customer_id and row["customerId"] != IDENTITY.customer_id:
      raise IdentityError("Migration includes another customer's account")
    binding = user.get("micu") or {}
    token_id = binding.get("tokenId")
    if not token_id or not binding.get("apiKey") or token_id in token_ids:
      raise IdentityError("Missing or shared Micu binding requires manual resolution")
    token_ids.add(token_id)
    if not row.get("subject") and user.get("authSource") != "oidc":
      imported_credential(user.get("passwordHash", ""))
  invitations = mapping.get("invitations", [])
  invitation_ids = [row.get("invitationId") for row in invitations]
  unmapped = {a["id"] for a in store.pending_accounts.values()
    if a.get("status") == "pending" and not a.get("customerId")}
  if len(invitation_ids) != len(set(invitation_ids)) or not unmapped.issubset(invitation_ids) or not set(invitation_ids).issubset(store.pending_accounts):
    raise IdentityError("Explicitly map every legacy pending invitation customerId")
  if any(not row.get("customerId") or (IDENTITY.customer_id and row["customerId"] != IDENTITY.customer_id)
      for row in invitations):
    raise IdentityError("Invalid invitation customer mapping")
  if any(store.pending_accounts[row["invitationId"]].get("customerId") not in {None, "", row["customerId"]}
      for row in invitations):
    raise IdentityError("Invitation customerId is immutable once assigned")
  return rows


def migrate(store, mapping, apply=False):
  rows = preflight(store, mapping)
  if not apply:
    return {"validatedAccounts": len(rows), "applied": False}
  if not IDENTITY.registration_paused or not IDENTITY.issuer:
    raise IdentityError("Pause registration and configure the OIDC issuer before applying")
  db = IdentityDatabase(store.database.engine)
  admin = KeycloakAdmin(IDENTITY)
  gateway = IdentityGateway(IDENTITY, None)
  for customer in mapping.get("customers", []):
    identifier = str(customer["id"])
    if IDENTITY.customer_id:
      raise IdentityError("Dedicated migrations cannot create customers")
    db.put(db.customers, identifier, {"id": identifier, "name": str(customer["name"]), "enabled": True})
  if not IDENTITY.customer_id and not db.get(db.customers, "public"):
    db.put(db.customers, "public", {"id": "public", "name": "Public", "enabled": True})
  for row in rows:
    original = store.user_by_identifier(row["localUserId"])
    user = deepcopy(original)
    subject = row.get("subject") or user.get("oidcSubject")
    if IDENTITY.customer_id:
      if not subject:
        raise IdentityError("Dedicated migration requires an explicitly mapped central subject")
      result = gateway.binding(subject)
      if result["customerId"] != row["customerId"] or result["username"] != user["username"]:
        raise IdentityError("Explicit central identity does not match local account")
      if result["micu"]["tokenId"] != user["micu"]["tokenId"]:
        raise IdentityError("Micu binding mismatch; resolve before migration")
    else:
      db.require_customer(row["customerId"])
      operation = "migration:" + user["id"]
      if subject:
        central = admin.user(subject)
      else:
        central = admin.find(user["username"])
        if central and central.get("attributes", {}).get("platformProvisioningId") != [operation]:
          raise IdentityError("Existing central identity requires an explicit subject mapping")
        if not central:
          admin.call("POST", "/users", json={"username": user["username"],
            "enabled": bool(user.get("enabled", True)),
            "attributes": {"customerId": [row["customerId"]], "platformProvisioningId": [operation], "platformUsername": [user["username"]]},
            "credentials": [imported_credential(user["passwordHash"])]})
          central = admin.find(user["username"])
        if not central:
          raise IdentityError("Imported identity is not visible yet; retry migration")
        subject = central["id"]
      if identity_username(central) != user["username"] or customer_of(central) != row["customerId"]:
        raise IdentityError("Explicit central identity does not match local account")
      result = {"sub": subject, "username": user["username"], "customerId": row["customerId"], "micu": user["micu"]}
      linked_id = db.linked_user_id(IDENTITY.issuer, subject)
      existing = db.get(db.bindings, subject)
      if linked_id and linked_id != user["id"]:
        raise IdentityError("Identity is already linked to another local account")
      if existing and (existing["username"] != result["username"] or existing["customerId"] != result["customerId"]
          or existing["micu"]["tokenId"] != result["micu"]["tokenId"]):
        raise IdentityError("Existing central binding does not match migration")
      db.put(db.bindings, subject, result, username_key=user["username"].casefold())
    user.update(authSource="oidc", oidcIssuer=IDENTITY.issuer, oidcSubject=subject, customerId=row["customerId"])
    user.pop("passwordHash", None)
    with store.database.engine.begin() as connection:
      linked = connection.execute(db.links.select().where(
        db.links.c.issuer == IDENTITY.issuer, db.links.c.subject == subject)).first()
      if linked and linked.user_id != user["id"]:
        raise IdentityError("Identity is already linked to another local account")
      if not linked:
        connection.execute(db.links.insert().values(issuer=IDENTITY.issuer, subject=subject, user_id=user["id"]))
      connection.execute(store.database.users.update().where(store.database.users.c.id == user["id"]).values(payload=user))
    original.update(user)
    original.pop("passwordHash", None)
  for row in mapping.get("invitations", []):
    if not IDENTITY.customer_id:
      db.require_customer(row["customerId"])
    invitation = store.pending_accounts[row["invitationId"]]
    invitation["customerId"] = row["customerId"]
    store.save_pending(invitation)
  return {"validatedAccounts": len(rows), "applied": True}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--mapping", type=Path, required=True)
  parser.add_argument("--apply", action="store_true")
  args = parser.parse_args()
  try:
    store = AccountStore(SETTINGS.workspace_storage_dir / "accounts.json", {}, SETTINGS.database_url)
    mapping = json.loads(args.mapping.read_text())
    print(json.dumps(migrate(store, mapping, args.apply)))
  except IdentityError as exc:
    parser.exit(1, "Migration stopped: " + str(exc) + "\n")
  except (KeyError, ValueError):
    # Migration inputs/credentials are private; no dumps or exception locals in logs.
    parser.exit(1, "Migration stopped: check the private mapping, bindings, and identity configuration.\n")


if __name__ == "__main__":
  main()
