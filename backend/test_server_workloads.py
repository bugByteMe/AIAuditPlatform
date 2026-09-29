from __future__ import annotations

import asyncio
import threading
import unittest

from server_workloads import BoundedExecutor, WorkloadBusy, classify_request


class BoundedExecutorTest(unittest.IsolatedAsyncioTestCase):
  async def test_worker_and_queue_capacity_rejects_without_blocking(self) -> None:
    pool = BoundedExecutor("file", 1, 1)
    release = threading.Event()
    started = threading.Event()

    def block(value):
      started.set()
      release.wait(2)
      return value

    first = asyncio.create_task(pool.run(block, "first"))
    self.assertTrue(await asyncio.to_thread(started.wait, 1))
    second = asyncio.create_task(pool.run(block, "second"))
    for _ in range(50):
      if pool.snapshot()["queued"] == 1:
        break
      await asyncio.sleep(0.01)
    with self.assertRaises(WorkloadBusy):
      await pool.run(block, "rejected")
    self.assertEqual(pool.snapshot()["rejected"], 1)
    release.set()
    self.assertEqual(await asyncio.gather(first, second), ["first", "second"])
    self.assertEqual(pool.snapshot()["completed"], 2)
    pool.shutdown()

  async def test_exception_and_queued_cancellation_release_capacity(self) -> None:
    pool = BoundedExecutor("file", 1, 1)
    release = threading.Event()
    started = threading.Event()

    def block():
      started.set()
      release.wait(2)

    first = asyncio.create_task(pool.run(block))
    self.assertTrue(await asyncio.to_thread(started.wait, 1))
    queued = asyncio.create_task(pool.run(lambda: None))
    for _ in range(50):
      if pool.snapshot()["queued"] == 1:
        break
      await asyncio.sleep(0.01)
    queued.cancel()
    with self.assertRaises(asyncio.CancelledError):
      await queued
    release.set()
    await first
    for _ in range(50):
      if pool.snapshot()["queued"] == 0:
        break
      await asyncio.sleep(0.01)

    def fail():
      raise RuntimeError("expected")

    with self.assertRaisesRegex(RuntimeError, "expected"):
      await pool.run(fail)
    snapshot = pool.snapshot()
    self.assertEqual(snapshot["queued"], 0)
    self.assertEqual(snapshot["active"], 0)
    self.assertEqual(snapshot["failed"], 1)
    pool.shutdown()

  async def test_shutdown_rejects_new_work(self) -> None:
    pool = BoundedExecutor("file", 1, 0)
    pool.shutdown()
    with self.assertRaises(WorkloadBusy):
      await pool.run(lambda: None)


class WorkloadRoutingTest(unittest.TestCase):
  def test_routes_heavy_operations_to_dedicated_pools(self) -> None:
    cases = {
      ("GET", "/api/workspaces/ws-1/files/preview"): ("file", "workspace.file_preview"),
      ("GET", "/api/workspaces/ws-1/files/rendered"): ("file", "workspace.file_rendered"),
      ("GET", "/api/workspaces/ws-1/download"): ("file", "workspace.download"),
      ("POST", "/api/workspaces/ws-1/refresh-artifacts"): ("file", "workspace.refresh-artifacts"),
      ("POST", "/api/workspaces/ws-1/fork"): ("file", "workspace.fork"),
      ("DELETE", "/api/workspaces/ws-1"): ("file", "workspace.delete"),
      ("DELETE", "/api/workspaces/ws-1/files"): ("file", "workspace.delete"),
      ("PUT", "/api/uploads/upload-1/files/0"): ("upload", "uploads"),
      ("GET", "/api/uploads/upload-1"): ("upload", "uploads"),
      ("POST", "/api/workspaces/ws-1/chat/runs"): ("external", "chat.run_start"),
      ("GET", "/api/session"): ("external", "session.refresh"),
      ("POST", "/api/recharge/orders"): ("external", "recharge"),
    }
    for request, expected in cases.items():
      with self.subTest(request=request):
        self.assertEqual(classify_request(*request), expected)

  def test_lightweight_and_sse_routes_keep_default_pool(self) -> None:
    for method, path in [
      ("GET", "/api/health"),
      ("GET", "/api/workspaces"),
      ("GET", "/api/workspaces/ws-1/chat/events"),
      ("GET", "/api/workspaces/ws-1/chat/stream"),
      ("GET", "/api/workspaces/ws-1/files/raw"),
    ]:
      with self.subTest(path=path):
        self.assertEqual(classify_request(method, path), (None, "default"))
