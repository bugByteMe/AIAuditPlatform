# Runtime Configuration

The backend reads `config/ai_audit.json` by default. Set `AI_AUDIT_CONFIG` to use another file. Every setting can be overridden with its corresponding environment variable; environment variables take precedence over the JSON file.

## Service and Storage

- `host`, `port`, `frontend_dir`, and `workspace_storage_dir` configure the HTTP service and managed data paths.
- `session_cookie`, `session_ttl_seconds`, `pbkdf2_iterations`, and `audit_log_limit` configure authentication persistence and audit retention.
- `max_file_bytes`, `max_workspace_bytes`, `max_file_count`, `max_text_preview_bytes`, `blocked_upload_suffixes`, and `office_preview_timeout_seconds` configure upload and preview limits.
- `upload_chunk_bytes` sets the resumable boundary exposed to browsers; `upload_stream_buffer_bytes` bounds each control/worker copy operation.
- `upload_session_ttl_seconds` controls abandoned-session retention, `upload_reservation_idle_seconds` releases idle worker reservations without deleting resumable state, and `upload_max_concurrent_streams` caps simultaneous control-plane streams.
- `upload_reservation_cpus` and `upload_reservation_memory` reserve worker capacity during active upload sessions. Each compute node may set `upload_slots` (default `1`).

## Chat Streaming

- `chat_poll_interval_ms` controls the browser reconciliation watchdog that runs alongside SSE.
- `sse_wait_timeout_seconds`, `sse_max_idle_rounds`, and `sse_retry_ms` control server-side SSE keepalives and reconnect guidance.
- The public `/api/runtime-config` endpoint exposes only browser-safe values such as polling and form-validation limits.

## Codex Runtime

- `codex_image`, `codex_root`, `codex_home_root`, and `skill_path` configure runtime paths.
- `container_uid`, `container_gid`, `run_timeout_seconds`, `run_network`, `run_cpus`, and `run_memory` configure container execution.
- `process_wait_timeout_seconds`, `docker_stop_grace_seconds`, `docker_stop_timeout_seconds`, and `scheduler_poll_seconds` configure process shutdown and scheduling timing.
- `run_cpus` and `run_memory` are also the reservation requested by every run. A worker is eligible only when both resources are available.

## Compute Workers

`compute_nodes` is the static scheduler inventory. When it is nonempty, chat runs are remote-only; the main node is not an implicit fallback. When it is empty, `local_run_capacity` retains the local Docker execution mode used by a single-node installation.

```json
{
  "compute_nodes": [
    {
      "id": "worker-01",
      "ip": "10.20.0.11",
      "port": 9443,
      "cpu": 16,
      "memory": "64GiB",
      "workspace_storage_dir": "/srv/ai-audit/workspace_storage",
      "tls_cert_file": "/etc/ai-audit/worker.crt",
      "tls_key_file": "/etc/ai-audit/worker.key",
      "enabled": true,
      "upload_slots": 1
    }
  ],
  "worker_ca_file": "/etc/ai-audit/cluster-ca.crt"
}
```

The shared filesystem must expose the same `active/`, `codex/homes/`, snapshot, and blob content to the control plane and every worker. Mount points may differ, so each node declares its local `workspace_storage_dir`. Start a node agent with:

```text
AI_AUDIT_WORKER_AUTH_TOKEN=<secret> AI_AUDIT_WORKER_NODE_ID=worker-01 python backend/worker_agent.py
```

The control plane requires the same bearer token through `AI_AUDIT_WORKER_AUTH_TOKEN`. Keep it out of the JSON file. `worker_ca_file` is the CA used by the control plane to verify worker HTTPS certificates; every node's `tls_cert_file` and `tls_key_file` are used only by that node's HTTPS server. Certificates must cover the configured IP address or hostname.

- `worker_health_interval_seconds` controls control-plane health polling.
- `worker_unhealthy_after_seconds` controls when a node is excluded after lost contact.
- `worker_request_timeout_seconds` bounds individual HTTPS calls.
- `worker_run_lease_seconds` fences orphaned work. A worker stops an active container if the control plane no longer renews its lease.

The agent API is intentionally narrow: health; run start, status/events, stop, lease renewal, and terminal acknowledgement; plus upload initialization, bounded chunks, status, finalization, and cleanup. It requires HTTPS and constant-time bearer authentication. Codex API keys are written by the control plane into the session-scoped shared `CODEX_HOME`; they are not included in scheduler requests or worker run records.

## Account Policy

- `registration_min_password_length`, `batch_invite_max_count`, and `account_max_sessions_limit` configure invitation and registration validation.
- `default_codex_base_url` is safe to store in the JSON file. Keep `default_codex_api_key` out of source-controlled configuration and provide it through `AI_AUDIT_DEFAULT_CODEX_API_KEY`.

Environment variable names use the `AI_AUDIT_` prefix and uppercase setting name, except the existing `SKILL_PATH` override for `skill_path`. List values such as `AI_AUDIT_BLOCKED_UPLOAD_SUFFIXES` use comma-separated entries.
