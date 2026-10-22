"""Local profiles remain independent of central credentials and customer identity."""
from copy import deepcopy

from sqlalchemy import select

from account_common import new_id, timestamp
from oidc_client import IdentityError


class AccountIdentityMixin:
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
        if user.get("micu") != identity["micu"]:
          user["micu"] = deepcopy(identity["micu"])
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
        "displayName": username, "role": "user", "groupId": group_id, "group": group["name"],
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
