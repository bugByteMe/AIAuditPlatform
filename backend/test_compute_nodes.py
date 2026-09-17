from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compute_nodes import WorkerRegistry


class FakeWorkerClient:
  health_by_id = {}

  def __init__(self, node, **_kwargs):
    self.node = node

  def health(self):
    return self.health_by_id[self.node["id"]]


def settings(nodes):
  return SimpleNamespace(
    compute_nodes=nodes,
    worker_auth_token="test-token",
    worker_ca_file="/tmp/test-ca.pem",
    worker_request_timeout_seconds=1,
    worker_run_lease_seconds=30,
    worker_health_interval_seconds=5,
    worker_unhealthy_after_seconds=15,
  )


def node(node_id, cpu, memory):
  return {
    "id": node_id,
    "ip": "127.0.0.1",
    "port": 9443,
    "cpu": float(cpu),
    "memoryBytes": memory,
    "enabled": True,
  }


class WorkerRegistryTest(unittest.TestCase):
  def setUp(self):
    FakeWorkerClient.health_by_id = {
      "small": {"nodeId": "small", "activeRuns": []},
      "large": {"nodeId": "large", "activeRuns": []},
    }
    self.registry = WorkerRegistry(
      settings([node("small", 4, 8_000), node("large", 8, 16_000)]),
      client_factory=FakeWorkerClient,
      start_monitor=False,
    )
    self.registry.refresh_all()

  def test_selects_least_utilized_compatible_healthy_worker(self):
    self.registry.reserve("small", "existing", 3, 1_000)
    self.assertEqual(self.registry.select(2, 2_000), "large")

  def test_reservation_changes_available_and_aggregate_resources(self):
    self.registry.reserve("large", "run-1", 2, 4_000)
    status = self.registry.node_status("large")
    aggregate = self.registry.aggregate_status()
    self.assertEqual(status["cpuAvailable"], 6)
    self.assertEqual(status["memoryAvailableBytes"], 12_000)
    self.assertEqual(aggregate["cpuTotal"], 12)
    self.assertEqual(aggregate["cpuAvailable"], 10)

  def test_claim_selects_and_reserves_atomically(self):
    worker_id = self.registry.claim("run-2", 4, 8_000)
    self.assertEqual(worker_id, "large")
    self.assertIn("run-2", self.registry.states["large"]["reservations"])

  def test_unhealthy_worker_is_excluded(self):
    self.registry.states["small"]["healthy"] = False
    self.assertEqual(self.registry.select(2, 2_000), "large")

  def test_rejects_request_larger_than_every_node(self):
    self.assertFalse(self.registry.compatible(9, 2_000))
    self.assertIsNone(self.registry.select(9, 2_000))


if __name__ == "__main__":
  unittest.main()
