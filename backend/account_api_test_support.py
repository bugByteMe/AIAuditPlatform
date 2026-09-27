from __future__ import annotations

import sys
import tempfile
import threading
import unittest
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from account_store import AccountStore
from server import Handler, RequestStopped, admin_account, apply_wechat_transaction, create_user_session, public_user, verify_password



class AccountApiTestBase(unittest.TestCase):
  def handler(self, payload: dict, actor: dict | None = None):
    handler = object.__new__(Handler)
    handler.read_json = lambda: payload
    handler.require_admin = lambda: actor
    handler.responses = []
    handler.write_json = lambda body, status=200, headers=None: handler.responses.append((body, int(status), headers or {}))
    return handler

