from __future__ import annotations

import secrets
import time


def new_id(prefix: str) -> str:
  return f"{prefix}_{secrets.token_urlsafe(12)}"


def timestamp() -> str:
  return time.strftime("%Y-%m-%d %H:%M:%S")

