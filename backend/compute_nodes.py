from __future__ import annotations

import json
import http.client
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy


class WorkerUnavailable(RuntimeError):
  pass


class WorkerRunFailed(RuntimeError):
  pass


class WorkerClient:
  def __init__(self, node: dict, *, token: str, ca_file, timeout: float, lease_seconds: float, upload_timeout: float = 300):
    self.node = deepcopy(node)
    self.token = token
    self.timeout = timeout
    self.upload_timeout = upload_timeout
    self.lease_seconds = lease_seconds
    if not token:
      raise ValueError("AI_AUDIT_WORKER_AUTH_TOKEN is required for remote compute nodes")
    if not ca_file:
      raise ValueError("worker_ca_file is required for remote compute nodes")
    self.ssl_context = ssl.create_default_context(cafile=str(ca_file))
    host = f"[{node['ip']}]" if ":" in node["ip"] else node["ip"]
    self.base_url = f"https://{host}:{node['port']}"

  def request(self, method: str, path: str, payload: dict | None = None, timeout: float | None = None) -> dict:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
      f"{self.base_url}{path}",
      data=body,
      method=method,
      headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
    )
    try:
      with urllib.request.urlopen(request, timeout=timeout or self.timeout, context=self.ssl_context) as response:
        return json.loads(response.read().decode("utf-8") or "{}")
    except (OSError, ValueError, urllib.error.HTTPError, urllib.error.URLError) as exc:
      raise WorkerUnavailable(f"worker {self.node['id']} request failed: {exc}") from exc

  def health(self) -> dict:
    return self.request("GET", "/v1/health")

  def start(self, run: dict) -> tuple[dict, object]:
    payload = {
      key: run.get(key)
      for key in [
        "id",
        "workspaceId",
        "sessionId",
        "user",
        "prompt",
        "model",
        "reasoning",
        "codexResume",
        "codexSessionId",
        "requestedCpu",
        "requestedMemoryBytes",
      ]
    }
    started = self.request("POST", "/v1/runs", payload)

    def events():
      cursor = int(run.get("workerEventCursor") or 0)
      last_lease = 0.0
      terminal = {"completed", "stopped", "failed"}
      while True:
        now = time.monotonic()
        if now - last_lease >= max(1.0, self.lease_seconds / 3):
          self.request("POST", f"/v1/runs/{urllib.parse.quote(run['id'])}/lease", {})
          last_lease = now
        result = self.request(
          "GET",
          f"/v1/runs/{urllib.parse.quote(run['id'])}/events?after={cursor}&wait={min(5, max(1, int(self.lease_seconds / 3)))}",
          timeout=max(self.timeout, min(10, self.lease_seconds / 2)),
        )
        run["container"] = str(result.get("container") or run.get("container") or "")
        for event in result.get("events") or []:
          cursor = max(cursor, int(event.get("id") or 0))
          run["workerEventCursor"] = cursor
          yield {key: value for key, value in event.items() if key != "id"}
        remote_status = str(result.get("status") or "")
        if remote_status in terminal:
          run["remoteStatus"] = remote_status
          if result.get("error"):
            raise WorkerRunFailed(str(result["error"]))
          return

    return started, events()

  def stop(self, run_id: str) -> dict:
    return self.request("POST", f"/v1/runs/{urllib.parse.quote(run_id)}/stop", {})

  def run_status(self, run_id: str) -> dict:
    return self.request("GET", f"/v1/runs/{urllib.parse.quote(run_id)}")

  def acknowledge(self, run_id: str) -> dict:
    return self.request("DELETE", f"/v1/runs/{urllib.parse.quote(run_id)}")

  def initialize_upload(self, upload: dict) -> dict:
    return self.request("POST", "/v1/uploads", upload)

  def upload_status(self, upload_id: str) -> dict:
    return self.request("GET", f"/v1/uploads/{urllib.parse.quote(upload_id)}")

  def complete_upload(self, upload_id: str) -> dict:
    return self.request("POST", f"/v1/uploads/{urllib.parse.quote(upload_id)}/complete", {})

  def cancel_upload(self, upload_id: str) -> dict:
    return self.request("DELETE", f"/v1/uploads/{urllib.parse.quote(upload_id)}")

  def stream_upload_chunk(self, upload_id: str, file_index: int, offset: int, source, length: int, buffer_bytes: int) -> dict:
    parsed = urllib.parse.urlparse(self.base_url)
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=max(self.timeout, self.upload_timeout), context=self.ssl_context)
    path = f"/v1/uploads/{urllib.parse.quote(upload_id)}/files/{file_index}?offset={offset}"
    try:
      connection.putrequest("PUT", path)
      connection.putheader("Authorization", f"Bearer {self.token}")
      connection.putheader("Content-Type", "application/octet-stream")
      connection.putheader("Content-Length", str(length))
      connection.endheaders()
      remaining = length
      while remaining:
        block = source.read(min(buffer_bytes, remaining))
        if not block:
          raise WorkerUnavailable("client upload stream ended before Content-Length")
        connection.send(block)
        remaining -= len(block)
      response = connection.getresponse()
      payload = json.loads(response.read().decode("utf-8") or "{}")
      if response.status < 200 or response.status >= 300:
        raise WorkerUnavailable(str(payload.get("message") or payload.get("error") or f"worker upload HTTP {response.status}"))
      return payload
    except (OSError, ValueError, http.client.HTTPException) as exc:
      if isinstance(exc, WorkerUnavailable):
        raise
      raise WorkerUnavailable(f"worker {self.node['id']} upload failed: {exc}") from exc
    finally:
      connection.close()


class WorkerRegistry:
  def __init__(self, settings, *, client_factory=WorkerClient, start_monitor: bool = True):
    self.settings = settings
    self.lock = threading.RLock()
    self.stop_event = threading.Event()
    self.configured_nodes = {node["id"]: deepcopy(node) for node in settings.compute_nodes}
    self.nodes = {node_id: node for node_id, node in self.configured_nodes.items() if node.get("enabled", True)}
    self.clients = {
      node_id: client_factory(
        node,
        token=settings.worker_auth_token,
        ca_file=settings.worker_ca_file,
        timeout=settings.worker_request_timeout_seconds,
        lease_seconds=settings.worker_run_lease_seconds,
        upload_timeout=getattr(settings, "upload_chunk_timeout_seconds", 300),
      )
      for node_id, node in self.nodes.items()
    }
    self.states = {
      node_id: {
        "healthy": False,
        "lastContact": None,
        "lastContactMonotonic": 0.0,
        "error": "not contacted",
        "reservations": {},
        "activeRuns": [],
      }
      for node_id in self.nodes
    }
    self.monitor = None
    if start_monitor and self.nodes:
      self.monitor = threading.Thread(target=self.monitor_loop, name="ai-audit-worker-health", daemon=True)
      self.monitor.start()

  def monitor_loop(self) -> None:
    while not self.stop_event.is_set():
      self.refresh_all()
      self.stop_event.wait(max(0.25, self.settings.worker_health_interval_seconds))

  def refresh_all(self) -> None:
    for node_id in self.nodes:
      self.refresh(node_id)

  def refresh(self, node_id: str) -> None:
    try:
      health = self.clients[node_id].health()
      if health.get("nodeId") != node_id:
        raise WorkerUnavailable("worker identity mismatch")
      with self.lock:
        state = self.states[node_id]
        state.update(
          {
            "healthy": True,
            "lastContact": time.strftime("%Y-%m-%d %H:%M:%S"),
            "lastContactMonotonic": time.monotonic(),
            "error": "",
            "activeRuns": list(health.get("activeRuns") or []),
          }
        )
    except Exception as exc:
      with self.lock:
        state = self.states[node_id]
        elapsed = time.monotonic() - float(state.get("lastContactMonotonic") or 0)
        if not state.get("lastContactMonotonic") or elapsed >= self.settings.worker_unhealthy_after_seconds:
          state["healthy"] = False
        state["error"] = str(exc)

  def compatible(self, cpu: float, memory_bytes: int) -> bool:
    return any(float(node["cpu"]) >= cpu and int(node["memoryBytes"]) >= memory_bytes for node in self.nodes.values())

  def select(self, cpu: float, memory_bytes: int) -> str | None:
    with self.lock:
      return self._select_locked(cpu, memory_bytes)

  def claim(self, run_id: str, cpu: float, memory_bytes: int, kind: str = "run") -> str | None:
    with self.lock:
      node_id = self._select_locked(cpu, memory_bytes, kind)
      if node_id:
        self.states[node_id]["reservations"][run_id] = {"cpu": cpu, "memoryBytes": memory_bytes, "kind": kind}
      return node_id

  def claim_upload(self, upload_id: str, cpu: float, memory_bytes: int) -> str | None:
    return self.claim(upload_id, cpu, memory_bytes, "upload")

  def _select_locked(self, cpu: float, memory_bytes: int, kind: str = "run") -> str | None:
    candidates = []
    now = time.monotonic()
    for order, (node_id, node) in enumerate(self.nodes.items()):
      state = self.states[node_id]
      if not state.get("healthy") or now - float(state.get("lastContactMonotonic") or 0) >= self.settings.worker_unhealthy_after_seconds:
        continue
      used_cpu = sum(float(item["cpu"]) for item in state["reservations"].values())
      used_memory = sum(int(item["memoryBytes"]) for item in state["reservations"].values())
      if kind == "upload" and sum(1 for item in state["reservations"].values() if item.get("kind") == "upload") >= int(node.get("uploadSlots") or 1):
        continue
      if used_cpu + cpu > float(node["cpu"]) or used_memory + memory_bytes > int(node["memoryBytes"]):
        continue
      utilization = max((used_cpu + cpu) / float(node["cpu"]), (used_memory + memory_bytes) / int(node["memoryBytes"]))
      candidates.append((utilization, order, node_id))
    return min(candidates)[2] if candidates else None

  def reserve(self, node_id: str, run_id: str, cpu: float, memory_bytes: int, kind: str = "run") -> None:
    with self.lock:
      self.states[node_id]["reservations"][run_id] = {"cpu": cpu, "memoryBytes": memory_bytes, "kind": kind}

  def release(self, node_id: str | None, run_id: str) -> None:
    if not node_id or node_id not in self.states:
      return
    with self.lock:
      self.states[node_id]["reservations"].pop(run_id, None)

  def client(self, node_id: str) -> WorkerClient:
    return self.clients[node_id]

  def status(self) -> list[dict]:
    now = time.monotonic()
    result = []
    with self.lock:
      for node_id, node in self.configured_nodes.items():
        if node_id not in self.states:
          result.append(
            {
              "id": node_id,
              "ip": node["ip"],
              "port": node["port"],
              "enabled": False,
              "healthy": False,
              "cpuTotal": float(node["cpu"]),
              "cpuUsed": 0.0,
              "cpuAvailable": 0.0,
              "memoryTotalBytes": int(node["memoryBytes"]),
              "memoryUsedBytes": 0,
              "memoryAvailableBytes": 0,
              "activeRunCount": 0,
              "activeRunIds": [],
              "activeUploadCount": 0,
              "lastContact": None,
              "error": "disabled by configuration",
            }
          )
          continue
        state = self.states[node_id]
        reservations = state["reservations"]
        upload_reservations = {key: value for key, value in reservations.items() if value.get("kind") == "upload"}
        run_reservations = {key: value for key, value in reservations.items() if value.get("kind") != "upload"}
        used_cpu = sum(float(item["cpu"]) for item in reservations.values())
        used_memory = sum(int(item["memoryBytes"]) for item in reservations.values())
        healthy = bool(state.get("healthy")) and now - float(state.get("lastContactMonotonic") or 0) < self.settings.worker_unhealthy_after_seconds
        result.append(
          {
            "id": node_id,
            "ip": node["ip"],
            "port": node["port"],
            "enabled": True,
            "healthy": healthy,
            "cpuTotal": float(node["cpu"]),
            "cpuUsed": used_cpu,
            "cpuAvailable": max(0.0, float(node["cpu"]) - used_cpu),
            "memoryTotalBytes": int(node["memoryBytes"]),
            "memoryUsedBytes": used_memory,
            "memoryAvailableBytes": max(0, int(node["memoryBytes"]) - used_memory),
            "activeRunCount": len(run_reservations),
            "activeRunIds": sorted(run_reservations),
            "activeUploadCount": len(upload_reservations),
            "lastContact": state.get("lastContact"),
            "error": state.get("error") or "",
          }
        )
    return result

  def node_status(self, node_id: str) -> dict | None:
    return next((item for item in self.status() if item["id"] == node_id), None)

  def aggregate_status(self) -> dict:
    workers = self.status()
    healthy = [worker for worker in workers if worker["healthy"]]
    return {
      "id": "compute-pool",
      "healthy": bool(healthy),
      "cpuTotal": sum(worker["cpuTotal"] for worker in healthy),
      "cpuUsed": sum(worker["cpuUsed"] for worker in healthy),
      "cpuAvailable": sum(worker["cpuAvailable"] for worker in healthy),
      "memoryTotalBytes": sum(worker["memoryTotalBytes"] for worker in healthy),
      "memoryUsedBytes": sum(worker["memoryUsedBytes"] for worker in healthy),
      "memoryAvailableBytes": sum(worker["memoryAvailableBytes"] for worker in healthy),
      "activeRunCount": sum(worker["activeRunCount"] for worker in healthy),
      "activeUploadCount": sum(worker.get("activeUploadCount", 0) for worker in healthy),
    }
