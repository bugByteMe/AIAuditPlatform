from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable


class WorkloadBusy(RuntimeError):
  def __init__(self, workload: str):
    super().__init__(f"{workload} workload is at capacity")
    self.workload = workload


class BoundedExecutor:
  """A dedicated thread pool with fail-fast bounded admission."""

  def __init__(self, name: str, workers: int, queue_size: int):
    if workers <= 0:
      raise ValueError(f"{name} workers must be positive")
    if queue_size < 0:
      raise ValueError(f"{name} queue size must be non-negative")
    self.name = name
    self.workers = workers
    self.queue_size = queue_size
    self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"ai-audit-{name}")
    self.slots = threading.BoundedSemaphore(workers + queue_size)
    self.lock = threading.Lock()
    self.accepting = True
    self.active = 0
    self.queued = 0
    self.completed = 0
    self.rejected = 0
    self.failed = 0

  async def run(self, function: Callable, *args):
    with self.lock:
      admitted = self.accepting and self.slots.acquire(blocking=False)
      if not admitted:
        self.rejected += 1
        raise WorkloadBusy(self.name)
      self.queued += 1

    state = {"started": False}

    def invoke():
      with self.lock:
        state["started"] = True
        self.queued -= 1
        self.active += 1
      try:
        return function(*args)
      except BaseException:
        with self.lock:
          self.failed += 1
        raise
      finally:
        with self.lock:
          self.active -= 1
          self.completed += 1

    try:
      future = self.executor.submit(invoke)
    except BaseException:
      with self.lock:
        self.queued -= 1
      self.slots.release()
      raise
    def release(_future) -> None:
      with self.lock:
        if not state["started"]:
          self.queued -= 1
      self.slots.release()

    future.add_done_callback(release)
    return await asyncio.wrap_future(future)

  def snapshot(self) -> dict:
    with self.lock:
      return {
        "name": self.name,
        "workers": self.workers,
        "queueSize": self.queue_size,
        "active": self.active,
        "queued": self.queued,
        "completed": self.completed,
        "rejected": self.rejected,
        "failed": self.failed,
        "accepting": self.accepting,
      }

  def shutdown(self) -> None:
    with self.lock:
      self.accepting = False
    self.executor.shutdown(wait=True, cancel_futures=True)


class WorkloadPools:
  def __init__(self, settings):
    self.file = BoundedExecutor("file", settings.file_work_workers, settings.file_work_queue)
    self.upload = BoundedExecutor("upload", settings.upload_request_workers, settings.upload_request_queue)
    self.external = BoundedExecutor("external", settings.external_request_workers, settings.external_request_queue)

  def pool(self, workload: str) -> BoundedExecutor:
    return getattr(self, workload)

  def shutdown(self) -> None:
    for pool in [self.file, self.upload, self.external]:
      pool.shutdown()


def classify_request(method: str, path: str) -> tuple[str | None, str]:
  """Return the dedicated workload and a safe route label for one request."""
  parts = [part for part in path.split("/") if part]
  if parts[:2] == ["api", "uploads"]:
    return "upload", "uploads"

  if parts[:2] == ["api", "workspaces"] and len(parts) >= 3:
    action = parts[3] if len(parts) > 3 else ""
    subaction = parts[4] if len(parts) > 4 else ""
    if method == "GET" and action == "files" and subaction in {"preview", "rendered"}:
      return "file", f"workspace.file_{subaction}"
    if method == "GET" and action == "download":
      return "file", "workspace.download"
    if method == "POST" and action in {"fork", "refresh-artifacts"}:
      return "file", f"workspace.{action}"
    if method == "DELETE" and action in {"", "files"}:
      return "file", "workspace.delete"
    if method == "POST" and action == "chat" and subaction == "runs" and len(parts) == 5:
      return "external", "chat.run_start"

  if path == "/api/session" and method == "GET":
    return "external", "session.refresh"
  if path == "/api/register" and method == "POST":
    return "external", "account.register"
  if path == "/api/accounts" or path.startswith("/api/accounts/"):
    return "external", "accounts"
  if path == "/api/recharge" or path.startswith("/api/recharge/"):
    return "external", "recharge"
  if path.startswith("/api/payments/wechat"):
    return "external", "wechat"
  if path.startswith("/api/admin/recharge-imports"):
    return "external", "recharge_import"
  return None, "default"
