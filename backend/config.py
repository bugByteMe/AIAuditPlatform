from __future__ import annotations

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _config_file() -> Path:
  raw = os.environ.get("AI_AUDIT_CONFIG", "config/ai_audit.json")
  path = Path(raw)
  return path if path.is_absolute() else ROOT / path


def _load_file_config() -> dict:
  path = _config_file()
  if not path.exists() or path.stat().st_size == 0:
    return {}
  return json.loads(path.read_text(encoding="utf-8"))


FILE_CONFIG = _load_file_config()


def _value(key: str, env_name: str, default):
  value = os.environ.get(env_name)
  if value not in {None, ""}:
    return value
  return FILE_CONFIG.get(key, default)


def _int(key: str, env_name: str, default: int) -> int:
  return int(_value(key, env_name, default))


def _path(key: str, env_name: str, default: str | Path) -> Path:
  raw = _value(key, env_name, str(default))
  path = Path(raw)
  return path if path.is_absolute() else ROOT / path


class Settings:
  def __init__(self) -> None:
    self.root = ROOT
    self.frontend_dir = _path("frontend_dir", "AI_AUDIT_FRONTEND_DIR", "frontend")
    self.workspace_storage_dir = _path("workspace_storage_dir", "AI_AUDIT_WORKSPACE_STORAGE_DIR", "workspace_storage")
    self.session_cookie = str(_value("session_cookie", "AI_AUDIT_SESSION_COOKIE", "ai_audit_session"))
    self.session_ttl_seconds = _int("session_ttl_seconds", "AI_AUDIT_SESSION_TTL_SECONDS", 8 * 60 * 60)
    self.pbkdf2_iterations = _int("pbkdf2_iterations", "AI_AUDIT_PBKDF2_ITERATIONS", 2_000)
    self.host = str(_value("host", "AI_AUDIT_HOST", "0.0.0.0"))
    self.port = _int("port", "AI_AUDIT_PORT", 8000)
    self.local_run_capacity = _int("local_run_capacity", "AI_AUDIT_LOCAL_RUN_CAPACITY", 1)
    self.default_codex_base_url = str(_value("default_codex_base_url", "AI_AUDIT_DEFAULT_CODEX_BASE_URL", "https://api.openai.com/v1"))
    self.default_codex_api_key = str(_value("default_codex_api_key", "AI_AUDIT_DEFAULT_CODEX_API_KEY", ""))
    self.codex_image = str(_value("codex_image", "AI_AUDIT_CODEX_IMAGE", "ai-audit-codex-runner:0.153.4"))
    self.codex_root = _path("codex_root", "AI_AUDIT_CODEX_ROOT", "workspace_storage/codex")
    self.codex_home_root = self._codex_home_root()
    raw_skill_path = str(_value("skill_path", "SKILL_PATH", ""))
    self.skill_path = Path(raw_skill_path).expanduser() if raw_skill_path else None
    self.container_uid = _int("container_uid", "AI_AUDIT_CODEX_UID", 1001)
    self.container_gid = _int("container_gid", "AI_AUDIT_CODEX_GID", 1001)
    self.run_timeout_seconds = _int("run_timeout_seconds", "AI_AUDIT_RUN_TIMEOUT_SECONDS", 3600)
    self.run_network = str(_value("run_network", "AI_AUDIT_RUN_NETWORK", "bridge"))
    self.run_cpus = str(_value("run_cpus", "AI_AUDIT_RUN_CPUS", "2"))
    self.run_memory = str(_value("run_memory", "AI_AUDIT_RUN_MEMORY", "4g"))

  def _codex_home_root(self) -> Path:
    raw = _value("codex_home_root", "AI_AUDIT_CODEX_HOME_ROOT", "")
    path = Path(raw) if raw else self.codex_root / "homes"
    return path if path.is_absolute() else ROOT / path


SETTINGS = Settings()
