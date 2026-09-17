from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ROOT, SETTINGS, normalize_compute_nodes, parse_memory_bytes


class ConfigurationTest(unittest.TestCase):
  def test_checked_in_config_covers_runtime_settings_without_secrets(self) -> None:
    payload = json.loads((ROOT / "config" / "ai_audit.json").read_text(encoding="utf-8"))
    expected = {
      "account_max_sessions_limit",
      "audit_log_limit",
      "batch_invite_max_count",
      "blocked_upload_suffixes",
      "chat_poll_interval_ms",
      "compute_nodes",
      "codex_home_root",
      "codex_image",
      "container_gid",
      "container_uid",
      "docker_stop_grace_seconds",
      "docker_stop_timeout_seconds",
      "max_file_bytes",
      "max_file_count",
      "max_text_preview_bytes",
      "max_workspace_bytes",
      "office_preview_timeout_seconds",
      "process_wait_timeout_seconds",
      "registration_min_password_length",
      "run_cpus",
      "run_memory",
      "run_timeout_seconds",
      "scheduler_poll_seconds",
      "session_cookie",
      "sse_max_idle_rounds",
      "sse_retry_ms",
      "sse_wait_timeout_seconds",
      "worker_ca_file",
      "worker_health_interval_seconds",
      "worker_request_timeout_seconds",
      "worker_run_lease_seconds",
      "worker_unhealthy_after_seconds",
    }
    self.assertEqual(set(), expected - set(payload))
    self.assertNotIn("default_codex_api_key", payload)

  def test_configured_limits_are_loaded(self) -> None:
    self.assertGreater(SETTINGS.max_file_bytes, 0)
    self.assertGreater(SETTINGS.max_workspace_bytes, SETTINGS.max_file_bytes)
    self.assertGreaterEqual(SETTINGS.chat_poll_interval_ms, 250)
    self.assertGreater(SETTINGS.sse_wait_timeout_seconds, 0)
    self.assertTrue(SETTINGS.blocked_upload_suffixes)

  def test_compute_node_configuration_is_normalized(self) -> None:
    nodes = normalize_compute_nodes(
      [{
        "id": "gpu-1",
        "ip": "10.0.0.11",
        "port": 9443,
        "cpu": 8,
        "memory": "32GiB",
        "workspace_storage_dir": "/srv/ai-audit",
        "tls_cert_file": "/etc/ai-audit/worker.crt",
        "tls_key_file": "/etc/ai-audit/worker.key",
      }]
    )
    self.assertEqual(nodes[0]["memoryBytes"], 32 * 1024**3)
    self.assertEqual(nodes[0]["workspaceStorageDir"], "/srv/ai-audit")
    self.assertTrue(nodes[0]["enabled"])

  def test_memory_parser_accepts_binary_units(self) -> None:
    self.assertEqual(parse_memory_bytes("1.5g"), int(1.5 * 1024**3))
    self.assertEqual(parse_memory_bytes("512MiB"), 512 * 1024**2)
