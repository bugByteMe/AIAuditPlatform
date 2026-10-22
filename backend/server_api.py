from server_admin import AdminHandlerMixin
from server_identity import IdentityHandlerMixin
from server_recharge_api import RechargeHandlerMixin
from server_transport import BaseHandler
from server_workspace_api import WorkspaceHandlerMixin
from server_oidc import OidcHandlerMixin


class Handler(OidcHandlerMixin, IdentityHandlerMixin, AdminHandlerMixin, RechargeHandlerMixin, WorkspaceHandlerMixin, BaseHandler):
  pass
