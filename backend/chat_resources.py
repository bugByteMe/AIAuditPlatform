from __future__ import annotations

import time


class ChatResourceMixin:
  def resource_status(self) -> dict:
    return self.worker_registry.aggregate_status() if self.worker_registry else self.local_resource_status()

  def local_resource_status(self) -> dict:
    active = len(self.active_runs)
    return {
      "id": "local-docker",
      "healthy": True,
      "cpuTotal": self.requested_cpu * self.capacity,
      "cpuUsed": self.requested_cpu * active,
      "cpuAvailable": self.requested_cpu * max(0, self.capacity - active),
      "memoryTotalBytes": self.requested_memory_bytes * self.capacity,
      "memoryUsedBytes": self.requested_memory_bytes * active,
      "memoryAvailableBytes": self.requested_memory_bytes * max(0, self.capacity - active),
      "activeRunCount": active,
    }

  def worker_status(self) -> list[dict]:
    if self.worker_registry:
      return self.worker_registry.status()
    active = len(self.active_runs)
    return [{
      **self.local_resource_status(),
      "ip": "127.0.0.1",
      "port": None,
      "activeRunIds": sorted(self.active_runs),
      "lastContact": time.strftime("%Y-%m-%d %H:%M:%S"),
      "error": "",
    }]

