from __future__ import annotations

import hashlib
import hmac
import mimetypes
import secrets
import time
from pathlib import Path

from config import SETTINGS

PBKDF2_ITERATIONS = SETTINGS.pbkdf2_iterations

def invite_digest(token: str) -> str:
  return hashlib.sha256(token.encode("utf-8")).hexdigest()

def static_content_type(file_path: Path) -> str:
  if file_path.suffix.lower() in {".js", ".mjs"}:
    return "text/javascript"
  return mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

def hash_password(password: str, salt: str | None = None) -> str:
  salt = salt or secrets.token_hex(16)
  digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS)
  return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"

def verify_password(password: str, encoded: str) -> bool:
  try:
    scheme, iterations, salt, expected = encoded.split("$", 3)
    if scheme != "pbkdf2_sha256":
      return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(digest.hex(), expected)
  except ValueError:
    return False

def utc_timestamp(epoch: float) -> str:
  return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def ascii_download_filename(filename: str, fallback: str = "download") -> str:
  safe = "".join(char if ord(char) < 128 and (char.isalnum() or char in {".", "-", "_"}) else "_" for char in filename)
  safe = safe.strip("._")
  return safe or fallback

