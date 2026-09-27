#!/usr/bin/env python3
from server_api import Handler
from server_asgi import app
from server_recharge import apply_wechat_transaction
from server_state import *
from server_transport import RequestStopped
from server_utils import ascii_download_filename, static_content_type, verify_password


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

