"""Backend boundaries for optional shared identity deployments."""
from urllib.parse import unquote

from identity_config import IDENTITY
from identity_common import UNIVERSAL_CUSTOMER_ID, customer_allowed
from workspace_store import StorageError


def visible_customer(account):
  return not IDENTITY.enabled or customer_allowed(account.get("customerId", ""), IDENTITY.customer_id)


def allowed_browser_origin(origin):
  return not IDENTITY.enabled or origin == IDENTITY.public_url


def invitation_customer(payload, actor):
  from server_oidc import services
  if not IDENTITY.enabled:
    return ""
  requested = str(payload.get("customerId") or "")
  if requested == UNIVERSAL_CUSTOMER_ID:
    raise StorageError("forbidden", "The universal customer is reserved for platform seed identities")
  if IDENTITY.customer_id:
    if requested and requested != IDENTITY.customer_id:
      raise StorageError("forbidden", "Invitation customer cannot be overridden")
    return IDENTITY.customer_id
  if actor.get("role") not in {"system_admin", "platform_admin"}:
    requested = actor.get("customerId", "")
  if not requested:
    raise ValueError("customerId is required for invitations")
  services()[1].require_customer(requested)
  return requested


def guard_identity_route(handler, method, path):
  if not IDENTITY.enabled:
    return
  from server_state import ACCOUNT_STORE
  if method == "DELETE" and path == "/api/groups/" + IDENTITY.default_group:
    raise StorageError("forbidden", "Configure another default admission group before deleting this group")
  if method == "POST" and path == "/api/accounts":
    raise StorageError("forbidden", "Use invitation provisioning in OIDC mode")
  if IDENTITY.customer_id and method != "GET" and (
    path.startswith("/api/recharge/") or path.startswith("/api/admin/recharge-imports")
    or path.endswith("/reset-budget") or path == "/api/payments/wechat/notify"
  ):
    raise StorageError("forbidden", "Recharge is managed on the main platform")
  if IDENTITY.customer_id and path.startswith("/api/recharge/orders"):
    # Reading an order can reconcile/credit it, so GET must be blocked too.
    raise StorageError("forbidden", "Recharge is managed on the main platform")
  if method in {"POST", "PUT", "PATCH", "DELETE"} and not path.startswith("/api/internal/identity/") and path != "/api/payments/wechat/notify":
    origin = getattr(handler, "headers", {}).get("Origin")
    if origin and origin != IDENTITY.public_url:
      raise StorageError("forbidden", "Request origin does not match this platform")
  if path.startswith("/api/accounts/") and not (method == "GET" and path == "/api/accounts/balances"):
    if IDENTITY.customer_id:
      handler.require_management()
    identifier = unquote(path.split("/")[3])
    account = ACCOUNT_STORE.user_by_identifier(identifier) or ACCOUNT_STORE.pending_accounts.get(identifier)
    if account and not visible_customer(account):
      raise StorageError("forbidden", "Account belongs to another customer")
  if method in {"DELETE", "PATCH"} and path.startswith("/api/groups/") and IDENTITY.customer_id:
    handler.require_management()
    group_id = unquote(path.rsplit("/", 1)[-1])
    if any(not visible_customer(account) for account in [*ACCOUNT_STORE.users.values(), *ACCOUNT_STORE.pending_accounts.values()]
        if account.get("groupId") == group_id):
      raise StorageError("forbidden", "Group contains another customer's accounts")
