#!/usr/bin/env python3
import sys
from types import ModuleType

import server_admin
import server_asgi
import server_identity
import server_recharge
import server_recharge_api
import server_state
import server_transport
import server_workspace_api
from server_api import Handler
from server_asgi import app
from recharge_import import parse_recharge_workbook
from server_recharge import apply_wechat_transaction
from server_state import *
from server_transport import RequestStopped
from server_utils import ascii_download_filename, static_content_type, verify_password


_PATCH_TARGETS = {
  "ACCOUNT_STORE": (server_state, server_admin, server_identity, server_recharge, server_recharge_api),
  "USERS": (server_state, server_admin, server_identity, server_recharge_api),
  "WORKSPACE_STORE": (server_state, server_admin, server_workspace_api),
  "MICU_CLIENT": (server_state, server_admin, server_recharge, server_recharge_api),
  "WECHAT_PAY": (server_state, server_recharge, server_recharge_api),
  "CHAT_RUNTIME": (server_state, server_admin, server_asgi, server_transport, server_workspace_api),
  "UPLOAD_MANAGER": (server_state, server_asgi, server_workspace_api),
  "SESSIONS": (server_state, server_identity),
  "SETTINGS": (server_state, server_asgi, server_transport),
  "add_audit": (server_state, server_admin, server_identity, server_recharge, server_recharge_api, server_workspace_api),
  "provision_micu": (server_state, server_identity),
  "parse_recharge_workbook": (server_recharge_api,),
  "apply_wechat_transaction": (server_recharge_api,),
}


class _CompatibilityModule(ModuleType):
  def __setattr__(self, name, value):
    super().__setattr__(name, value)
    for target in _PATCH_TARGETS.get(name, ()):
      setattr(target, name, value)


sys.modules[__name__].__class__ = _CompatibilityModule


def main() -> None:
  import argparse
  import uvicorn

  from config import SETTINGS

  parser = argparse.ArgumentParser(description="AI Audit backend")
  parser.add_argument("--host", default=SETTINGS.backend_host)
  parser.add_argument("--port", type=int, default=SETTINGS.backend_port)
  args = parser.parse_args()
  uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
  main()
