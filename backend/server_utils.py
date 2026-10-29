from __future__ import annotations

import hashlib
import hmac
import mimetypes
import re
import secrets
import time
from email.utils import formatdate, parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config import SETTINGS

PBKDF2_ITERATIONS = SETTINGS.pbkdf2_iterations

def invite_digest(token: str) -> str:
  return hashlib.sha256(token.encode("utf-8")).hexdigest()

def static_content_type(file_path: Path) -> str:
  if file_path.suffix.lower() in {".js", ".mjs"}:
    return "text/javascript"
  return mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"


def static_cache_headers(file_path: Path) -> dict[str, str]:
  stat = file_path.stat()
  tag = hashlib.sha256(f"{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()
  # Unversioned modules must revalidate on deployment, not stay stale for a TTL.
  return {
    "Cache-Control": "public, no-cache",
    "ETag": f'"{tag}"',
    "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
  }


def static_not_modified(request_headers, response_headers: dict[str, str]) -> bool:
  etags = request_headers.get("If-None-Match")
  if etags is not None:
    return any(tag.strip().removeprefix("W/") in {"*", response_headers["ETag"]} for tag in etags.split(","))
  modified_since = request_headers.get("If-Modified-Since")
  if modified_since:
    try:
      return parsedate_to_datetime(modified_since) >= parsedate_to_datetime(response_headers["Last-Modified"])
    except (TypeError, ValueError, OverflowError):
      pass
  return False

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


def download_started_cookie(raw_url: str, disposition: str) -> str | None:
  if disposition != "attachment":
    return None
  token = (parse_qs(urlparse(raw_url).query).get("downloadToken") or [""])[0]
  if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", token):
    return None
  return f"ai_audit_download_{token}=1; Path=/; Max-Age=120; SameSite=Lax"
