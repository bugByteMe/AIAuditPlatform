"""Opt-in shared identity configuration; credentials are environment-only."""
import json
import os
from urllib.parse import urlparse


class IdentitySettings:
  def __init__(self):
    def value(name, default=""):
      return os.environ.get("AI_AUDIT_" + name, default).strip()
    self.auth_mode = value("AUTH_MODE", "local")
    self.issuer = value("OIDC_ISSUER").rstrip("/")
    self.client_id = value("OIDC_CLIENT_ID")
    self.client_secret = value("OIDC_CLIENT_SECRET")
    self.public_url = value("PUBLIC_URL").rstrip("/")
    self.customer_id = value("DEPLOYMENT_CUSTOMER_ID")
    self.default_group = value("OIDC_DEFAULT_GROUP_ID")
    self.central_url = value("IDENTITY_CENTRAL_URL").rstrip("/")
    self.central_token = value("IDENTITY_CENTRAL_TOKEN")
    self.clients = json.loads(value("IDENTITY_CLIENTS_JSON", "{}"))
    self.admin_client = value("KEYCLOAK_ADMIN_CLIENT_ID")
    self.admin_secret = value("KEYCLOAK_ADMIN_CLIENT_SECRET")
    self.revalidate_seconds = int(value("OIDC_REVALIDATE_SECONDS", "300"))
    self.registration_paused = value("REGISTRATION_PAUSED", "false").lower() == "true"

  @property
  def enabled(self):
    return self.auth_mode == "oidc"

  def validate(self, groups):
    if self.auth_mode not in {"local", "oidc"}:
      raise ValueError("AUTH_MODE must be local or oidc")
    if not self.enabled:
      return
    for name, url in (("OIDC_ISSUER", self.issuer), ("PUBLIC_URL", self.public_url)):
      parsed = urlparse(url)
      if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
        raise ValueError(name + " must be a trusted HTTPS URL")
    if urlparse(self.public_url).path not in {"", "/"}:
      raise ValueError("PUBLIC_URL must be an origin without a path")
    if not self.client_id or not self.client_secret or self.default_group not in groups:
      raise ValueError("OIDC requires client credentials and an existing OIDC_DEFAULT_GROUP_ID")
    if not 30 <= self.revalidate_seconds <= 900:
      raise ValueError("OIDC_REVALIDATE_SECONDS must be between 30 and 900")
    if self.customer_id:
      parsed = urlparse(self.central_url)
      if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"} or parsed.username or parsed.query or parsed.fragment or len(self.central_token) < 32:
        raise ValueError("Dedicated OIDC deployments require HTTPS IDENTITY_CENTRAL_URL and token")
      if self.admin_secret or self.clients:
        raise ValueError("Dedicated instances must not hold central administration credentials")
    elif not self.admin_client or not self.admin_secret:
      raise ValueError("Main OIDC deployment requires Keycloak service-account credentials")
    tokens = set()
    for client, entry in self.clients.items():
      token = entry.get("token", "")
      if len(token) < 32 or token in tokens or not entry.get("customerId") or entry["customerId"] == "public":
        raise ValueError("Each identity client requires a unique >=32 character token and business customerId")
      tokens.add(token)


IDENTITY = IdentitySettings()
