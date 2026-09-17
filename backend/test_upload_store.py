from __future__ import annotations

import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from compute_nodes import WorkerUnavailable
from upload_store import UploadManager
from workspace_store import StorageError, UploadedFile, WorkspaceStore


USER = {"username": "owner", "role": "user", "group": "Audit", "groupId": "group-1"}


def settings(**overrides):
  values = {
    "upload_stream_buffer_bytes": 64 * 1024,
    "upload_max_concurrent_streams": 2,
    "upload_session_ttl_seconds": 3600,
    "upload_reservation_idle_seconds": 60,
    "upload_chunk_bytes": 128 * 1024,
    "upload_reservation_cpus": 0.25,
    "upload_reservation_memory_bytes": 64 * 1024 * 1024,
    "max_file_count": 100,
    "max_file_bytes": 1024 * 1024,
    "max_workspace_bytes": 4 * 1024 * 1024,
  }
  values.update(overrides)
  return SimpleNamespace(**values)


class BoundedReader(io.BytesIO):
  def __init__(self, content: bytes):
    super().__init__(content)
    self.largest_read = 0

  def read(self, size=-1):
    if size < 0:
      raise AssertionError("upload streams must always use bounded reads")
    self.largest_read = max(self.largest_read, size)
    return super().read(size)


class FailingUploadClient:
  def initialize_upload(self, _payload):
    return {"status": "uploading"}

  def stream_upload_chunk(self, *_args, **_kwargs):
    raise WorkerUnavailable("worker connection dropped")


class SingleUploadRegistry:
  def __init__(self):
    self.nodes = {"worker": {}}
    self.reservations = set()
    self.worker = FailingUploadClient()

  def claim_upload(self, upload_id, *_args):
    if self.reservations:
      return None
    self.reservations.add(upload_id)
    return "worker"

  def node_status(self, _node_id):
    return {"healthy": True}

  def client(self, _node_id):
    return self.worker

  def release(self, _node_id, upload_id):
    self.reservations.discard(upload_id)


class UploadManagerTest(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.store = WorkspaceStore(Path(self.temp.name))
    self.manager = UploadManager(self.store, settings=settings())

  def tearDown(self):
    self.temp.cleanup()

  def wait_for_commit(self, upload_id: str) -> dict:
    deadline = time.monotonic() + 5
    result = self.manager.status(upload_id, USER)
    while result["status"] != "committed" and time.monotonic() < deadline:
      if result["status"] == "failed":
        self.fail(result["error"])
      time.sleep(0.02)
      result = self.manager.status(upload_id, USER)
    self.assertEqual(result["status"], "committed")
    return result

  def test_new_workspace_upload_streams_with_bounded_reads_and_commits(self):
    content = b"x" * 200_000
    upload = self.manager.create(USER, {
      "mode": "create", "name": "Large", "shared": False,
      "files": [{"path": "folder/data.bin", "size": len(content), "lastModified": 10}],
    })
    reader = BoundedReader(content[:128 * 1024])
    first = self.manager.receive_chunk(upload["id"], USER, 0, 0, reader, 128 * 1024)
    self.assertEqual(first["offsets"], [128 * 1024])
    self.assertLessEqual(reader.largest_read, 64 * 1024)
    self.manager.receive_chunk(upload["id"], USER, 0, 128 * 1024, io.BytesIO(content[128 * 1024:]), len(content) - 128 * 1024)
    self.manager.complete(upload["id"], USER)
    result = self.wait_for_commit(upload["id"])
    self.assertEqual((self.store.workspace_path(result["workspaceId"]) / "folder" / "data.bin").read_bytes(), content)
    self.assertEqual(result["workspace"]["fileCount"], 1)

  def test_duplicate_chunk_is_idempotent_but_conflicting_content_is_rejected(self):
    upload = self.manager.create(USER, {"mode": "create", "files": [{"path": "a.txt", "size": 3}]})
    self.manager.receive_chunk(upload["id"], USER, 0, 0, io.BytesIO(b"abc"), 3)
    repeated = self.manager.receive_chunk(upload["id"], USER, 0, 0, io.BytesIO(b"abc"), 3)
    self.assertEqual(repeated["offsets"], [3])
    with self.assertRaisesRegex(StorageError, "differs"):
      self.manager.receive_chunk(upload["id"], USER, 0, 0, io.BytesIO(b"xyz"), 3)

  def test_retry_overwrites_an_uncommitted_partial_chunk(self):
    upload = self.manager.create(USER, {"mode": "create", "files": [{"path": "partial.txt", "size": 6}]})
    with self.assertRaisesRegex(StorageError, "ended before Content-Length"):
      self.manager.receive_chunk(upload["id"], USER, 0, 0, io.BytesIO(b"abc"), 6)
    result = self.manager.receive_chunk(upload["id"], USER, 0, 0, io.BytesIO(b"abcdef"), 6)
    self.assertEqual(result["offsets"], [6])
    self.assertEqual(self.manager.local_worker.file_path(upload["id"], 0).read_bytes(), b"abcdef")

  def test_worker_chunk_failure_releases_capacity_and_keeps_session_resumable(self):
    registry = SingleUploadRegistry()
    manager = UploadManager(self.store, worker_registry=registry, settings=settings())
    upload = manager.create(USER, {"mode": "create", "files": [{"path": "a.txt", "size": 3}]})
    with self.assertRaisesRegex(StorageError, "worker connection dropped"):
      manager.receive_chunk(upload["id"], USER, 0, 0, io.BytesIO(b"abc"), 3)
    session = manager.load()["sessions"][upload["id"]]
    self.assertEqual(session["status"], "uploading")
    self.assertFalse(session["reservationHeld"])
    self.assertEqual(registry.reservations, set())

  def test_append_upload_locks_workspace_and_cancel_releases_it(self):
    workspace = self.store.create_workspace(USER, "Existing", False, [UploadedFile("a.txt", b"old")])
    upload = self.manager.create(USER, {"mode": "append", "workspaceId": workspace["id"], "files": [{"path": "a.txt", "size": 3}]})
    self.assertTrue(self.store.get_workspace(workspace["id"], USER)["locked"])
    self.manager.cancel(upload["id"], USER)
    stored = self.store.get_workspace(workspace["id"], USER)
    self.assertFalse(stored["locked"])
    self.assertNotIn("activeUploadId", stored)

  def test_offsets_survive_control_and_worker_store_reconstruction(self):
    upload = self.manager.create(USER, {"mode": "create", "files": [{"path": "resume.txt", "size": 6}]})
    self.manager.receive_chunk(upload["id"], USER, 0, 0, io.BytesIO(b"abc"), 3)
    restarted = UploadManager(self.store, settings=settings())
    self.assertEqual(restarted.status(upload["id"], USER)["offsets"], [3])
    restarted.receive_chunk(upload["id"], USER, 0, 3, io.BytesIO(b"def"), 3)
    restarted.complete(upload["id"], USER)
    self.manager = restarted
    result = self.wait_for_commit(upload["id"])
    self.assertEqual((self.store.workspace_path(result["workspaceId"]) / "resume.txt").read_bytes(), b"abcdef")

  def test_append_commit_replaces_file_and_releases_lock(self):
    workspace = self.store.create_workspace(USER, "Existing", False, [UploadedFile("a.txt", b"old")])
    upload = self.manager.create(USER, {"mode": "append", "workspaceId": workspace["id"], "files": [{"path": "a.txt", "size": 3}]})
    self.manager.receive_chunk(upload["id"], USER, 0, 0, io.BytesIO(b"new"), 3)
    self.manager.complete(upload["id"], USER)
    result = self.wait_for_commit(upload["id"])
    self.assertEqual((self.store.workspace_path(workspace["id"]) / "a.txt").read_bytes(), b"new")
    self.assertFalse(result["workspace"]["locked"])
    self.assertEqual(result["workspace"]["artifacts"][0]["status"], "modified")

  def test_commit_recreates_a_prepared_blob_removed_before_metadata_publish(self):
    upload = self.manager.create(USER, {"mode": "create", "files": [{"path": "recover.txt", "size": 7}]})
    self.manager.receive_chunk(upload["id"], USER, 0, 0, io.BytesIO(b"recover"), 7)
    self.manager.complete(upload["id"], USER)
    deadline = time.monotonic() + 5
    worker_state = self.manager.local_worker.load(upload["id"])
    while worker_state["status"] != "ready" and time.monotonic() < deadline:
      time.sleep(0.02)
      worker_state = self.manager.local_worker.load(upload["id"])
    self.assertEqual(worker_state["status"], "ready")
    prepared = self.manager.local_worker.result(upload["id"])
    digest = prepared["files"]["recover.txt"]["blob"]
    self.store.blob_path(digest).unlink()

    result = self.manager.status(upload["id"], USER)
    self.assertEqual(result["status"], "committed")
    self.assertTrue(self.store.blob_path(digest).is_file())

  def test_manifest_limits_are_rejected_before_staging(self):
    manager = UploadManager(self.store, settings=settings(max_file_bytes=2))
    with self.assertRaisesRegex(StorageError, "per-file limit"):
      manager.create(USER, {"mode": "create", "files": [{"path": "large.bin", "size": 3}]})


if __name__ == "__main__":
  unittest.main()
