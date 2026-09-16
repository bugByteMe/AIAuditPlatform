# Runtime Configuration

The backend reads `config/ai_audit.json` by default. Set `AI_AUDIT_CONFIG` to use another file. Every setting can be overridden with its corresponding environment variable; environment variables take precedence over the JSON file.

## Service and Storage

- `host`, `port`, `frontend_dir`, and `workspace_storage_dir` configure the HTTP service and managed data paths.
- `session_cookie`, `session_ttl_seconds`, `pbkdf2_iterations`, and `audit_log_limit` configure authentication persistence and audit retention.
- `max_file_bytes`, `max_workspace_bytes`, `max_file_count`, `max_text_preview_bytes`, `blocked_upload_suffixes`, and `office_preview_timeout_seconds` configure upload and preview limits.

## Chat Streaming

- `chat_poll_interval_ms` controls the browser reconciliation watchdog that runs alongside SSE.
- `sse_wait_timeout_seconds`, `sse_max_idle_rounds`, and `sse_retry_ms` control server-side SSE keepalives and reconnect guidance.
- The public `/api/runtime-config` endpoint exposes only browser-safe values such as polling and form-validation limits.

## Codex Runtime

- `local_run_capacity`, `codex_image`, `codex_root`, `codex_home_root`, and `skill_path` configure scheduling and runtime paths.
- `container_uid`, `container_gid`, `run_timeout_seconds`, `run_network`, `run_cpus`, and `run_memory` configure container execution.
- `process_wait_timeout_seconds`, `docker_stop_grace_seconds`, `docker_stop_timeout_seconds`, and `scheduler_poll_seconds` configure process shutdown and scheduling timing.

## Account Policy

- `registration_min_password_length`, `batch_invite_max_count`, and `account_max_sessions_limit` configure invitation and registration validation.
- `default_codex_base_url` is safe to store in the JSON file. Keep `default_codex_api_key` out of source-controlled configuration and provide it through `AI_AUDIT_DEFAULT_CODEX_API_KEY`.

Environment variable names use the `AI_AUDIT_` prefix and uppercase setting name, except the existing `SKILL_PATH` override for `skill_path`. List values such as `AI_AUDIT_BLOCKED_UPLOAD_SUFFIXES` use comma-separated entries.
