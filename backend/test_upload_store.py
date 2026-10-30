from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from compute_nodes import WorkerUnavailable
from upload_store import UploadManager, WorkerUploadStore
from worker_upload import UploadStreamTimeout
from workspace_store import StorageError, UploadedFile, WorkspaceStore
from server_workspace_api import WorkspaceHandlerMixin


USER = {"username": "owner", "role": "user", "group": "Audit", "groupId": "group-1"}


def settings(**overrides):
  values = {
    "upload_stream_buffer_bytes": 64 * 1024,
    "upload_max_concurrent_streams": 2,
    "upload_session_ttl_seconds": 3600,
    "upload_session_idle_timeout_seconds": 60,
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


class RemoteUploadClient:
  def __init__(self, root: Path):
    self.worker = WorkerUploadStore(root)

  def initialize_upload(self, payload):
    return self.worker.initialize(payload)

  def upload_status(self, upload_id):
    return self.worker.status(upload_id)

  def complete_upload(self, upload_id):
    return self.worker.complete(upload_id)

  def upload_result(self, upload_id):
    return self.worker.result(upload_id)

  def stream_upload_chunk(self, upload_id, index, offset, source, length, _buffer_bytes):
    return self.worker.write_chunk(upload_id, index, offset, source, length)

  def cancel_upload(self, upload_id):
    return self.worker.cancel(upload_id)

  def purge_upload(self, upload_id):
    return self.worker.purge(upload_id)


class RemoteUploadRegistry:
  def __init__(self, root: Path):
    self.nodes = {"worker": {}}
    self.reservations = set()
    self.worker = RemoteUploadClient(root)

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
    self.manager.shutdown()
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

  def test_blocked_suffix_is_rejected_at_upload_admission(self):
    with self.assertRaises(StorageError) as context:
      self.manager.create(USER, {"mode": "create", "files": [{"path": "scripts/reconcile.sh", "size": 10}]})
    self.assertEqual(context.exception.code, "blocked_file_type")

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

  def test_remote_worker_upload_commits_using_worker_result(self):
    registry = RemoteUploadRegistry(self.store.root)
    manager = UploadManager(self.store, worker_registry=registry, settings=settings())
    manager.local_worker = WorkerUploadStore(Path(self.temp.name) / "control-local")
    upload = manager.create(USER, {"mode": "create", "files": [{"path": "a.txt", "size": 3}]})
    manager.receive_chunk(upload["id"], USER, 0, 0, io.BytesIO(b"abc"), 3)
    manager.complete(upload["id"], USER)
    result = manager.status(upload["id"], USER)
    deadline = time.monotonic() + 5
    while result["status"] != "committed" and time.monotonic() < deadline:
      time.sleep(0.02)
      result = manager.status(upload["id"], USER)
    self.assertEqual(result["status"], "committed")
    self.assertEqual((self.store.workspace_path(result["workspaceId"]) / "a.txt").read_bytes(), b"abc")

  def test_platform_admin_can_start_append_upload_in_private_other_group_workspace(self):
    workspace = self.store.create_workspace(USER, "Existing", False, [UploadedFile("a.txt", b"old")])
    platform_admin = {"username": "platform.admin", "role": "platform_admin", "group": "Other", "groupId": "group-2"}
    upload = self.manager.create(platform_admin, {
      "mode": "append", "workspaceId": workspace["id"], "files": [{"path": "b.txt", "size": 3}],
    })
    self.assertTrue(self.store.get_workspace(workspace["id"], USER)["locked"])
    self.manager.cancel(upload["id"], platform_admin)
    self.assertFalse(self.store.get_workspace(workspace["id"], USER)["locked"])

  def test_append_upload_locks_workspace_and_cancel_releases_it(self):
    workspace = self.store.create_workspace(USER, "Existing", False, [UploadedFile("a.txt", b"old")])
    metadata = self.store.load_workspace_metadata(workspace["id"])
    metadata["workspaces"][workspace["id"]]["sessions"] = [{"id": "chat_existing"}]
    self.store.save_workspace_lifecycle(metadata["workspaces"][workspace["id"]])
    upload = self.manager.create(USER, {"mode": "append", "workspaceId": workspace["id"], "files": [{"path": "a.txt", "size": 3}]})
    self.assertTrue(self.store.get_workspace(workspace["id"], USER)["locked"])
    self.assertEqual(self.store.load_workspace_metadata(workspace["id"])["workspaces"][workspace["id"]]["sessions"], [{"id": "chat_existing"}])
    metadata = self.store.load_workspace_metadata(workspace["id"])
    metadata["workspaces"][workspace["id"]]["sessions"].append({"id": "chat_created_during_upload"})
    self.store.save_workspace_lifecycle(metadata["workspaces"][workspace["id"]])
    self.manager.cancel(upload["id"], USER)
    stored = self.store.get_workspace(workspace["id"], USER)
    self.assertFalse(stored["locked"])
    self.assertNotIn("activeUploadId", stored)
    self.assertEqual(self.store.load_workspace_metadata(workspace["id"])["workspaces"][workspace["id"]]["sessions"], [
      {"id": "chat_existing"}, {"id": "chat_created_during_upload"},
    ])

  def test_stale_upload_lease_repair_preserves_workspace_sessions(self):
    workspace = self.store.create_workspace(USER, "Existing", False, [UploadedFile("a.txt", b"old")])
    metadata = self.store.load_workspace_metadata(workspace["id"])
    metadata["workspaces"][workspace["id"]]["sessions"] = [{"id": "chat_existing"}]
    self.store.save_workspace_lifecycle(metadata["workspaces"][workspace["id"]])
    upload = self.manager.create(USER, {"mode": "append", "workspaceId": workspace["id"], "files": [{"path": "b.txt", "size": 1}]})
    session = self.manager.database.get(upload["id"])
    session["status"] = "cancelled"
    self.manager.database.save(session)
    self.manager._repair_workspace_leases()
    stored = self.store.load_workspace_metadata(workspace["id"])["workspaces"][workspace["id"]]
    self.assertFalse(stored["locked"])
    self.assertNotIn("activeUploadId", stored)
    self.assertEqual(stored["sessions"], [{"id": "chat_existing"}])

  def test_workspace_owner_and_administrators_can_cancel_another_users_active_append(self):
    workspace = self.store.create_workspace(USER, "Shared", True, [UploadedFile("existing/a.txt", b"old")])
    uploader = {"username": "editor", "role": "platform_admin", "group": "Elsewhere"}
    actors = [USER, {"username": "system", "role": "system_admin", "group": "Elsewhere"},
              {"username": "platform-other", "role": "platform_admin", "group": "Elsewhere"}, uploader]
    for actor in actors:
      upload = self.manager.create(uploader, {"mode": "append", "workspaceId": workspace["id"], "files": [{"path": "a.txt", "size": 1}]})
      self.assertEqual(self.manager.cancel(upload["id"], actor)["status"], "cancelled")
      self.assertNotIn("activeUploadId", self.store.get_workspace(workspace["id"], USER))

  def test_append_cancel_rejects_other_group_user_stale_id_and_processing(self):
    workspace = self.store.create_workspace(USER, "Shared", True, [UploadedFile("existing/a.txt", b"old")])
    uploader = {"username": "editor", "role": "platform_admin", "group": "Elsewhere"}
    upload = self.manager.create(uploader, {"mode": "append", "workspaceId": workspace["id"], "files": [{"path": "a.txt", "size": 1}]})
    outsider = {"username": "outsider", "role": "user", "group": "Elsewhere"}
    with self.assertRaises(StorageError) as denied:
      self.manager.cancel(upload["id"], outsider)
    self.assertEqual(denied.exception.code, "forbidden")
    session = self.manager.database.get(upload["id"])
    session["status"] = "processing"
    self.manager.database.save(session)
    with self.assertRaises(StorageError) as processing:
      self.manager.cancel(upload["id"], USER)
    self.assertEqual(processing.exception.code, "upload_commit_started")
    session["status"] = "uploading"
    self.manager.database.save(session)
    stored = self.store.get_workspace(workspace["id"], USER)
    stored["activeUploadId"] = "upload_replaced"
    self.store.save_workspace_lifecycle(stored)
    with self.assertRaises(StorageError) as stale:
      self.manager.cancel(upload["id"], USER)
    self.assertEqual(stale.exception.code, "upload_commit_started")

  def test_new_workspace_upload_cannot_be_cancelled_by_platform_admin(self):
    upload = self.manager.create(USER, {"mode": "create", "files": [{"path": "a.txt", "size": 1}]})
    actor = {"username": "platform", "role": "platform_admin", "group": "Elsewhere"}
    with self.assertRaises(StorageError) as denied:
      self.manager.cancel(upload["id"], actor)
    self.assertEqual(denied.exception.code, "forbidden")

  def test_append_destination_preserves_folder_and_overwrites_only_target_path(self):
    workspace = self.store.create_workspace(USER, "Existing", False, [
      UploadedFile("target/Reports/a.txt", b"old"), UploadedFile("Reports/a.txt", b"root"),
    ])
    upload = self.manager.create(USER, {"mode": "append", "workspaceId": workspace["id"], "destinationPath": "target", "files": [
      {"path": "Reports/a.txt", "size": 3}, {"path": "Reports/b.txt", "size": 1},
    ]})
    session = self.manager.database.get(upload["id"])
    self.assertEqual([item["path"] for item in session["files"]], ["target/Reports/a.txt", "target/Reports/b.txt"])
    self.assertEqual(session["reservedBytes"], 1)
    self.manager.receive_chunk(upload["id"], USER, 0, 0, io.BytesIO(b"new"), 3)
    self.manager.receive_chunk(upload["id"], USER, 1, 0, io.BytesIO(b"b"), 1)
    self.manager.complete(upload["id"], USER)
    self.wait_for_commit(upload["id"])
    root = self.store.workspace_path(workspace["id"])
    self.assertEqual((root / "Reports/a.txt").read_bytes(), b"root")
    self.assertEqual((root / "target/Reports/a.txt").read_bytes(), b"new")
    self.assertEqual((root / "target/Reports/b.txt").read_bytes(), b"b")

  def test_append_destination_must_be_existing_real_directory(self):
    workspace = self.store.create_workspace(USER, "Existing", False, [UploadedFile("target/a.txt", b"a")])
    payload = {"mode": "append", "workspaceId": workspace["id"], "files": [{"path": "a.txt", "size": 1}]}
    for destination in ["missing", "target/a.txt", "../escape"]:
      with self.subTest(destination=destination), self.assertRaises(StorageError):
        self.manager.create(USER, {**payload, "destinationPath": destination})
    (self.store.workspace_path(workspace["id"]) / "linked").symlink_to("target", target_is_directory=True)
    with self.assertRaises(StorageError) as linked:
      self.manager.create(USER, {**payload, "destinationPath": "linked"})
    self.assertEqual(linked.exception.code, "unsafe_path")
    with self.assertRaises(StorageError):
      self.manager.create(USER, {"mode": "create", "destinationPath": "target", "files": [{"path": "a.txt", "size": 1}]})

  def test_workspace_catalog_shows_active_upload_to_group_collaborators(self):
    workspace = self.store.create_workspace(USER, "Shared", True, [UploadedFile("a.txt", b"a")])
    uploader = {"username": "editor", "role": "platform_admin", "group": "Elsewhere"}
    upload = self.manager.create(uploader, {"mode": "append", "workspaceId": workspace["id"], "files": [{"path": "b.txt", "size": 1}]})

    class Handler(WorkspaceHandlerMixin):
      def __init__(self, user):
        self.user = user
        self.payload = None

      def require_user(self):
        return self.user

      def write_json(self, payload, *args, **kwargs):
        self.payload = payload

    runtime = SimpleNamespace(resource_status=lambda: {})
    with patch("server_workspace_api.WORKSPACE_STORE", self.store), patch("server_workspace_api.UPLOAD_MANAGER", self.manager), \
         patch("server_workspace_api.CHAT_RUNTIME", runtime), patch.object(self.manager.database, "statuses", wraps=self.manager.database.statuses) as statuses:
      owner = Handler(USER)
      owner.list_workspaces()
      self.assertEqual(owner.payload["workspaces"][0]["activeUpload"], {"id": upload["id"], "status": "uploading"})
      statuses.assert_called_once_with([upload["id"]])
      owner.workspace_api("GET", f"/api/workspaces/{workspace['id']}", "")
      self.assertEqual(owner.payload["workspace"]["activeUpload"]["id"], upload["id"])

      viewer = Handler({"username": "viewer", "role": "user", "group": USER["group"]})
      viewer.list_workspaces()
      self.assertEqual(viewer.payload["workspaces"][0]["activeUpload"]["id"], upload["id"])
      viewer.workspace_api("GET", f"/api/workspaces/{workspace['id']}", "")
      self.assertEqual(viewer.payload["workspace"]["activeUpload"]["id"], upload["id"])

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

  def test_remote_manager_recovery_recognizes_persisted_active_upload(self):
    upload = self.manager.create(USER, {"mode": "create", "files": [{"path": "resume.txt", "size": 6}]})
    registry = SingleUploadRegistry()

    restarted = UploadManager(self.store, worker_registry=registry, settings=settings())

    session = restarted.load()["sessions"][upload["id"]]
    self.assertEqual(session["status"], "uploading")
    self.assertFalse(session["reservationHeld"])

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

  def test_upload_coordination_is_sql_backed(self):
    upload = self.manager.create(USER, {"mode": "create", "files": [{"path": "a.txt", "size": 3}]})
    self.assertFalse((self.store.root / "upload_sessions.json").exists())
    self.assertTrue((self.store.root / "upload.sqlite3").is_file())
    self.assertEqual(self.manager.database.get(upload["id"])["owner"], USER["username"])

  def test_gc_expires_stale_upload_and_collects_terminal_row(self):
    manager = UploadManager(self.store, settings=settings(upload_session_ttl_seconds=1, upload_terminal_retention_seconds=1))
    upload = manager.create(USER, {"mode": "create", "files": [{"path": "stale.txt", "size": 3}]})
    session = manager.database.get(upload["id"])
    session["updatedAt"] = time.time() - 10
    manager.database.save(session)
    result = manager.collect_garbage()
    self.assertEqual(result["expired"], 1)
    expired = manager.database.get(upload["id"])
    self.assertEqual(expired["status"], "expired")
    expired["terminalAt"] = time.time() - 10
    manager.database.save(expired)
    self.assertEqual(manager.collect_garbage()["deleted"], 1)
    self.assertIsNone(manager.database.get(upload["id"]))
    manager.shutdown()

  def test_status_polling_does_not_keep_an_upload_session_alive(self):
    manager = UploadManager(self.store, settings=settings(upload_session_ttl_seconds=1))
    upload = manager.create(USER, {"mode": "create", "files": [{"path": "stale.txt", "size": 3}]})
    session = manager.database.get(upload["id"])
    session["updatedAt"] = time.time() - 10
    manager.database.save(session)

    self.assertEqual(manager.status(upload["id"], USER)["status"], "uploading")
    self.assertEqual(manager.collect_garbage()["expired"], 1)
    self.assertEqual(manager.database.get(upload["id"])["status"], "expired")
    manager.shutdown()

  def test_stalled_chunk_expires_session_and_releases_append_lock(self):
    workspace = self.store.create_workspace(USER, "Existing", False, [UploadedFile("a.txt", b"old")])
    upload = self.manager.create(USER, {
      "mode": "append", "workspaceId": workspace["id"], "files": [{"path": "a.txt", "size": 3}],
    })

    class StalledReader:
      def read(self, _size=-1):
        raise UploadStreamTimeout("upload stopped because no data was received before the timeout")

    with self.assertRaisesRegex(StorageError, "no data was received") as raised:
      self.manager.receive_chunk(upload["id"], USER, 0, 0, StalledReader(), 3)
    self.assertEqual(raised.exception.code, "upload_session_expired")
    self.assertEqual(self.manager.database.get(upload["id"])["status"], "expired")
    self.assertFalse(self.store.get_workspace(workspace["id"], USER)["locked"])

  def test_expired_session_cannot_reclaim_worker_or_accept_completion(self):
    registry = RemoteUploadRegistry(self.store.root)
    manager = UploadManager(self.store, worker_registry=registry, settings=settings())
    upload = manager.create(USER, {"mode": "create", "files": [{"path": "a.txt", "size": 3}]})
    session = manager.database.get(upload["id"])
    manager._terminal(session, "expired", "idle timeout")
    self.assertEqual(registry.reservations, set())

    with self.assertRaises(StorageError) as chunk_error:
      manager.receive_chunk(upload["id"], USER, 0, 0, io.BytesIO(b"abc"), 3)
    self.assertEqual(chunk_error.exception.code, "upload_session_expired")
    with self.assertRaises(StorageError) as complete_error:
      manager.complete(upload["id"], USER)
    self.assertEqual(complete_error.exception.code, "upload_session_expired")
    self.assertEqual(registry.reservations, set())
    self.assertFalse(manager.local_worker.directory(upload["id"]).exists())
    manager.shutdown()

  def test_worker_orphan_gc_never_removes_workspace_or_blob_data(self):
    worker = WorkerUploadStore(self.store.root)
    upload = worker.initialize({"id": "upload_orphan", "workspaceId": "ws_keep", "mode": "create", "files": [], "parentFiles": {}, "snapshotId": "snap_1"})
    state = worker.load(upload["id"])
    state["updatedAt"] = time.time() - 100
    worker.state_path(upload["id"]).write_text(json.dumps(state), encoding="utf-8")
    workspace_file = self.store.active_dir / "ws_keep" / "keep.txt"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("keep", encoding="utf-8")
    blob_file = self.store.blob_dir / "aa" / "bb" / "aabb"
    blob_file.parent.mkdir(parents=True, exist_ok=True)
    blob_file.write_text("keep", encoding="utf-8")
    self.assertEqual(worker.collect_garbage(time.time() - 10), 1)
    self.assertTrue(workspace_file.is_file())
    self.assertTrue(blob_file.is_file())


if __name__ == "__main__":
  unittest.main()
