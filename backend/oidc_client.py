"""OIDC code flow and narrowly scoped Keycloak admin transport."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from urllib.parse import quote, urlencode

import httpx
import jwt


class IdentityError(ValueError):
  def __init__(self, message: str, *, status_code: int | None = None):
    super().__init__(message)
    self.status_code = status_code


class IdentityNameTaken(IdentityError):
  pass


def identity_http_client() -> httpx.Client:
  """Create the thread-safe client shared by long-lived identity services."""
  return httpx.Client(
    timeout=20,
    follow_redirects=False,
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30),
  )


def request_json(method, url, *, client: httpx.Client | None = None, **kwargs):
  accepted_statuses = kwargs.pop("accepted_statuses", ())
  try:
    request = client.request if client is not None else httpx.request
    response = request(method, url, timeout=20, follow_redirects=False, **kwargs)
    if response.status_code in accepted_statuses:
      return {}
    if response.status_code >= 400:
      if response.status_code == 409:
        try:
          if response.json().get("error") == "identity_username_exists":
            raise IdentityNameTaken("Username already exists; sign in or choose another username")
        except (AttributeError, json.JSONDecodeError):
          pass
      # Never propagate provider bodies: they can contain credentials or tokens.
      raise IdentityError(
        "Identity provider request failed (%s)" % response.status_code,
        status_code=response.status_code,
      )
    return response.json() if response.content else {}
  except (httpx.HTTPError, ValueError) as exc:
    if isinstance(exc, IdentityError):
      raise
    raise IdentityError("Identity provider unavailable") from None


class OidcClient:
  def __init__(self, settings, http_client: httpx.Client | None = None):
    self.settings = settings
    self.http_client = http_client or identity_http_client()
    self._metadata = None
    self._jwks = None

  def metadata(self):
    if self._metadata is None:
      data = request_json(
        "GET", self.settings.issuer + "/.well-known/openid-configuration", client=self.http_client,
      )
      if data.get("issuer") != self.settings.issuer:
        raise IdentityError("OIDC issuer mismatch")
      for field in ("authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"):
        if not str(data.get(field, "")).startswith("https://"):
          raise IdentityError("OIDC endpoints must use HTTPS")
      self._metadata = data
    return self._metadata

  def authorization_url(self, state, nonce, verifier):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return self.metadata()["authorization_endpoint"] + "?" + urlencode({
      "client_id": self.settings.client_id, "redirect_uri": self.callback_url,
      "response_type": "code", "scope": "openid profile", "state": state,
      "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
    })

  @property
  def callback_url(self):
    return self.settings.public_url + "/api/oidc/callback"

  def tokens(self, data):
    return request_json("POST", self.metadata()["token_endpoint"],
      client=self.http_client, auth=(self.settings.client_id, self.settings.client_secret), data=data)

  def exchange(self, code, verifier, nonce):
    tokens = self.tokens({"grant_type": "authorization_code", "code": code,
      "code_verifier": verifier, "redirect_uri": self.callback_url})
    try:
      if self._jwks is None:
        self._jwks = jwt.PyJWKClient(self.metadata()["jwks_uri"], timeout=20)
      raw = tokens["id_token"]
      key = self._jwks.get_signing_key_from_jwt(raw)
      claims = jwt.decode(raw, key.key, algorithms=["RS256", "PS256", "ES256"],
        issuer=self.settings.issuer, audience=self.settings.client_id,
        options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]})
      if claims["nonce"] != nonce or claims.get("azp", self.settings.client_id) != self.settings.client_id:
        raise IdentityError("Invalid OIDC login")
      if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and "azp" not in claims:
        raise IdentityError("Missing authorized party")
      info = self.userinfo(tokens["access_token"])
      if info.get("sub") != claims["sub"]:
        raise IdentityError("OIDC subject mismatch")
      return tokens, info
    except (jwt.PyJWTError, KeyError, TypeError):
      raise IdentityError("Invalid OIDC login") from None

  def userinfo(self, token):
    return request_json(
      "GET", self.metadata()["userinfo_endpoint"], client=self.http_client,
      headers={"Authorization": "Bearer " + token},
    )

  def revoke(self, refresh_token):
    endpoint = self.metadata().get("revocation_endpoint")
    if endpoint:
      request_json("POST", endpoint, client=self.http_client, auth=(self.settings.client_id, self.settings.client_secret),
        data={"token": refresh_token, "token_type_hint": "refresh_token"})

  def close(self):
    self.http_client.close()


class KeycloakAdmin:
  def __init__(self, settings, http_client: httpx.Client | None = None):
    self.settings = settings
    self.base = settings.issuer.replace("/realms/", "/admin/realms/", 1)
    self.http_client = http_client or identity_http_client()
    self._service_token = ""
    self._service_token_expires_at = 0.0
    self._service_token_lock = threading.Lock()

  def service_token(self) -> str:
    with self._service_token_lock:
      now = time.monotonic()
      if self._service_token and now < self._service_token_expires_at:
        return self._service_token
      result = request_json(
        "POST", self.settings.issuer + "/protocol/openid-connect/token", client=self.http_client,
        data={
          "grant_type": "client_credentials", "client_id": self.settings.admin_client,
          "client_secret": self.settings.admin_secret,
        },
      )
      token = str(result.get("access_token") or "")
      if not token:
        raise IdentityError("Identity provider did not return a service-account token")
      try:
        lifetime = max(0.0, float(result.get("expires_in", 60)))
      except (TypeError, ValueError):
        raise IdentityError("Identity provider returned an invalid service-account token expiry") from None
      refresh_skew = min(30.0, max(1.0, lifetime * 0.1))
      self._service_token = token
      self._service_token_expires_at = now + max(0.0, lifetime - refresh_skew)
      return token

  def invalidate_service_token(self, token: str) -> None:
    with self._service_token_lock:
      if self._service_token == token:
        self._service_token = ""
        self._service_token_expires_at = 0.0

  def call(self, method, path, **kwargs):
    for attempt in range(2):
      token = self.service_token()
      try:
        return request_json(
          method, self.base + path, client=self.http_client,
          headers={"Authorization": "Bearer " + token}, **kwargs,
        )
      except IdentityError as exc:
        if attempt or exc.status_code != 401:
          raise
        self.invalidate_service_token(token)
    raise AssertionError("unreachable")

  def user(self, subject):
    return self.call("GET", "/users/" + quote(subject, safe=""))

  def find(self, username):
    users = self.call("GET", "/users", params={"username": username.lower(), "exact": "true"})
    return next((u for u in users if u["username"].casefold() == username.casefold()), None)

  def create(self, username, password, customer, operation):
    existing = self.find(username)
    if existing:
      if existing.get("attributes", {}).get("platformProvisioningId") != [operation]:
        raise IdentityNameTaken("Username already exists; sign in to redeem the invitation")
      return existing
    try:
      self.call("POST", "/users", json={"username": username, "enabled": True,
        "attributes": {"customerId": [customer], "platformProvisioningId": [operation], "platformUsername": [username]},
        "credentials": [{"type": "password", "value": password, "temporary": False}]})
    except IdentityError:
      # Resolve a uniqueness race or an uncertain create response without reset/delete.
      existing = self.find(username)
      if existing:
        if existing.get("attributes", {}).get("platformProvisioningId") == [operation]:
          return existing
        raise IdentityNameTaken("Username already exists; sign in or choose another username") from None
      raise
    user = self.find(username)
    if not user:
      raise IdentityError("Identity creation pending; retry later")
    return user


def customer_of(user):
  value = user.get("attributes", {}).get("customerId", [])
  if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str):
    raise IdentityError("Identity has no valid customer assignment")
  return value[0]


def identity_username(user):
  # Preserve existing platform/Micu names even if Keycloak normalizes login case.
  value = user.get("attributes", {}).get("platformUsername")
  if value is None:
    return user["username"]
  if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str) or not value[0]:
    raise IdentityError("Identity has an invalid platform username")
  return value[0]
