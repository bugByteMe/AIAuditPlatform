"""Local profiles remain independent of central credentials and customer identity."""
from copy import deepcopy

from sqlalchemy import select

from account_common import new_id, timestamp
from oidc_client import IdentityError
from identity_common import UNIVERSAL_CUSTOMER_ID


class AccountIdentityMixin:
  def link_universal_seed(self, issuer, identity, seed_key):
    subject, username = str(identity.get("sub") or ""), str(identity.get("username") or "")
    if (not subject or username != seed_key or identity.get("customerId") != UNIVERSAL_CUSTOMER_ID
        or not isinstance(identity.get("micu"), dict)):
      raise IdentityError("Universal seed identity does not match the configured profile")
    with self.lock, self.identity_db.exclusive("admit:" + subject):
      user = self.user_by_identifier(username)
      if not user or user.get("universalSeedKey") != seed_key:
        raise IdentityError("Universal seed local profile is missing or changed")
      if (user.get("authSource") == "oidc" and
          (user.get("oidcIssuer") != issuer or user.get("oidcSubject") != subject)):
        raise IdentityError("Universal seed local profile has a conflicting identity link")
      if user.get("customerId") not in {None, "", UNIVERSAL_CUSTOMER_ID}:
        raise IdentityError("Universal seed local profile has a conflicting customer assignment")
      linked_id = self.identity_db.linked_user_id(issuer, subject)
      with self.database.engine.connect() as connection:
        linked_identity = connection.execute(select(
          self.identity_db.links.c.issuer, self.identity_db.links.c.subject).where(
          self.identity_db.links.c.user_id == user["id"])).first()
      if (linked_id not in {None, user["id"]}
          or (linked_identity and tuple(linked_identity) != (issuer, subject))):
        raise IdentityError("Universal seed identity is already linked to another profile")
      row = deepcopy(user)
      row.update(authSource="oidc", oidcIssuer=issuer, oidcSubject=subject,
        customerId=UNIVERSAL_CUSTOMER_ID, providerMode="micu", micu=deepcopy(identity["micu"]))
      row.pop("passwordHash", None)
      with self.database.engine.begin() as connection:
        if linked_id is None:
          connection.execute(self.identity_db.links.insert().values(
            issuer=issuer, subject=subject, user_id=row["id"]))
        connection.execute(self.database.users.update().where(self.database.users.c.id == row["id"]).values(
          username=row["username"], username_key=row["username"].casefold(),
          group_id=row.get("groupId", ""), enabled=bool(row.get("enabled", True)),
          status=row.get("status", "active"), payload=row))
      user.clear()
      user.update(row)
      return user

  def release_identity_invite(self, token, username):
    with self.lock, self.identity_db.exclusive("invite:" + token):
      account = self.pending_by_token(token)
      if not account or account.get("status") != "provisioning" or account.get("provisioningUsername") != username or account.get("oidcSubject"):
        return
      row = deepcopy(account)
      row["status"] = "pending"
      row.pop("provisioningUsername", None)
      self.database.save_pending(row)
      account.clear()
      account.update(row)

  def reserve_identity_invite(self, token, username):
    with self.lock, self.identity_db.exclusive("invite:" + token):
      account = self.pending_by_token(token)
      if not account:
        raise IdentityError("Invalid or already used invitation")
      with self.database.engine.begin() as connection:
        row = connection.execute(select(self.database.pending.c.payload).where(
          self.database.pending.c.id == account["id"])).scalar_one_or_none()
        if not row or row.get("status") not in {"pending", "provisioning"}:
          raise IdentityError("Invalid or revoked invitation")
        if row.get("provisioningUsername", username) != username:
          raise IdentityError("Invitation is reserved for another username")
        row.update(status="provisioning", provisioningUsername=username)
        connection.execute(self.database.pending.update().where(self.database.pending.c.id == row["id"]).values(
          status="provisioning", payload=row))
      account.update(row)
      return deepcopy(row)

  def admit_identity(self, issuer, identity, default_group, invite_token=""):
    subject, username, customer = identity["sub"], identity["username"], identity["customerId"]
    with self.lock, self.identity_db.exclusive("admit:" + subject):
      existing_id = self.identity_db.linked_user_id(issuer, subject)
      if existing_id:
        user = self.user_by_identifier(existing_id)
        if not user or not user.get("enabled", True) or user.get("status") != "active":
          raise IdentityError("Local account is disabled or removed")
        if user.get("customerId") != customer or user["username"] != username:
          raise IdentityError("Identity changed; administrator action required")
        changed = False
        if "displayName" in identity:
          display_name = str(identity.get("displayName") or "").strip() or username
          if user.get("displayName") != display_name:
            user["displayName"] = display_name
            changed = True
        if user.get("micu") != identity["micu"]:
          user["micu"] = deepcopy(identity["micu"])
          changed = True
        if changed:
          self.save_user(user)
        # A profile already admitted here never consumes another invitation.
        return user
      if self.username_exists(username):
        raise IdentityError("Existing local account requires explicit identity migration")
      # A login that follows interrupted registration must finish that reservation,
      # not silently create a profile in the default group instead.
      if not invite_token:
        reservation = next((a for a in self.pending_accounts.values()
          if a.get("status") == "provisioning" and a.get("provisioningUsername") == username
          and a.get("customerId") == customer), None)
        if reservation:
          invite_token = reservation["inviteToken"]
      pending = self.pending_by_token(invite_token) if invite_token else None
      if invite_token and (not pending or pending.get("status") not in {"pending", "provisioning"}):
        raise IdentityError("Invalid invitation")
      if pending and (pending.get("customerId") != customer or
          pending.get("provisioningUsername", username) != username):
        raise IdentityError("Invitation identity or customer mismatch")
      group_id = pending["groupId"] if pending else default_group
      group = self.groups.get(group_id)
      if not group:
        raise IdentityError("Local admission group is not configured")
      profile = {"id": pending["id"] if pending else new_id("usr"), "username": username,
        "displayName": str(identity.get("displayName") or "").strip() or username,
        "role": "user", "groupId": group_id, "group": group["name"],
        "enabled": bool(pending.get("enabled", True)) if pending else True,
        "status": "active", "maxSessions": pending.get("maxSessions", 1) if pending else 1,
        "usedTokens": 0, "createdAt": timestamp(), "authSource": "oidc", "oidcIssuer": issuer,
        "oidcSubject": subject, "customerId": customer, "providerMode": "micu", "micu": identity["micu"],
        "customCodex": {}}
      if pending:
        import hashlib
        profile["activationInviteDigest"] = hashlib.sha256(invite_token.encode()).hexdigest()
      with self.database.engine.begin() as connection:
        if pending:
          deleted = connection.execute(self.database.pending.delete().where(
            self.database.pending.c.id == pending["id"], self.database.pending.c.status.in_(["pending", "provisioning"])))
          if deleted.rowcount != 1:
            raise IdentityError("Invitation already redeemed or revoked")
        connection.execute(self.database.users.insert().values(id=profile["id"], username=username,
          username_key=username.casefold(), group_id=group_id, enabled=profile["enabled"], status="active", payload=profile))
        connection.execute(self.identity_db.links.insert().values(issuer=issuer, subject=subject, user_id=profile["id"]))
      if pending:
        self.pending_accounts.pop(pending["id"], None)
      self.users[username] = profile
      return profile
