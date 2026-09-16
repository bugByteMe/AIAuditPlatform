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


def _float(key: str, env_name: str, default: float) -> float:
  return float(_value(key, env_name, default))


def _path(key: str, env_name: str, default: str | Path) -> Path:
  raw = _value(key, env_name, str(default))
  path = Path(raw)
  return path if path.is_absolute() else ROOT / path


def _list(key: str, env_name: str, default: list[str]) -> list[str]:
  value = _value(key, env_name, default)
  if isinstance(value, str):
    return [item.strip() for item in value.split(",") if item.strip()]
  return [str(item).strip() for item in value if str(item).strip()]


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
    self.max_file_bytes = _int("max_file_bytes", "AI_AUDIT_MAX_FILE_BYTES", 250 * 1024 * 1024)
    self.max_workspace_bytes = _int("max_workspace_bytes", "AI_AUDIT_MAX_WORKSPACE_BYTES", 2 * 1024 * 1024 * 1024)
    self.max_file_count = _int("max_file_count", "AI_AUDIT_MAX_FILE_COUNT", 10_000)
    self.max_text_preview_bytes = _int("max_text_preview_bytes", "AI_AUDIT_MAX_TEXT_PREVIEW_BYTES", 256 * 1024)
    self.blocked_upload_suffixes = _list(
      "blocked_upload_suffixes",
      "AI_AUDIT_BLOCKED_UPLOAD_SUFFIXES",
      [".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".ps1", ".sh"],
    )
    self.office_preview_timeout_seconds = _int("office_preview_timeout_seconds", "AI_AUDIT_OFFICE_PREVIEW_TIMEOUT_SECONDS", 45)
    self.sse_wait_timeout_seconds = _float("sse_wait_timeout_seconds", "AI_AUDIT_SSE_WAIT_TIMEOUT_SECONDS", 5)
    self.sse_max_idle_rounds = _int("sse_max_idle_rounds", "AI_AUDIT_SSE_MAX_IDLE_ROUNDS", 24)
    self.sse_retry_ms = _int("sse_retry_ms", "AI_AUDIT_SSE_RETRY_MS", 2_000)
    self.chat_poll_interval_ms = _int("chat_poll_interval_ms", "AI_AUDIT_CHAT_POLL_INTERVAL_MS", 2_000)
    self.process_wait_timeout_seconds = _int("process_wait_timeout_seconds", "AI_AUDIT_PROCESS_WAIT_TIMEOUT_SECONDS", 10)
    self.docker_stop_grace_seconds = _int("docker_stop_grace_seconds", "AI_AUDIT_DOCKER_STOP_GRACE_SECONDS", 10)
    self.docker_stop_timeout_seconds = _int("docker_stop_timeout_seconds", "AI_AUDIT_DOCKER_STOP_TIMEOUT_SECONDS", 20)
    self.scheduler_poll_seconds = _float("scheduler_poll_seconds", "AI_AUDIT_SCHEDULER_POLL_SECONDS", 0.1)
    self.registration_min_password_length = _int("registration_min_password_length", "AI_AUDIT_REGISTRATION_MIN_PASSWORD_LENGTH", 8)
    self.batch_invite_max_count = _int("batch_invite_max_count", "AI_AUDIT_BATCH_INVITE_MAX_COUNT", 100)
    self.account_max_sessions_limit = _int("account_max_sessions_limit", "AI_AUDIT_ACCOUNT_MAX_SESSIONS_LIMIT", 10)
    self.audit_log_limit = _int("audit_log_limit", "AI_AUDIT_AUDIT_LOG_LIMIT", 100)

  def _codex_home_root(self) -> Path:
    raw = _value("codex_home_root", "AI_AUDIT_CODEX_HOME_ROOT", "")
    path = Path(raw) if raw else self.codex_root / "homes"
    return path if path.is_absolute() else ROOT / path


SETTINGS = Settings()
