from __future__ import annotations

import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workspace_store import StorageError, UploadedFile, WorkspaceStore


OWNER = {"username": "li.review", "role": "group_admin", "group": "审计一组"}


class WorkspaceScopedPersistenceTest(unittest.TestCase):
  def setUp(self) -> None:
    self.tempdir = tempfile.TemporaryDirectory()
    self.store = WorkspaceStore(Path(self.tempdir.name) / "workspace_storage")

  def tearDown(self) -> None:
    self.tempdir.cleanup()

  def create_workspace(self) -> dict:
    return self.store.create_workspace(OWNER, "Audit Upload", True, [UploadedFile("input.txt", b"initial")])

  def test_mutation_does_not_load_or_rewrite_unrelated_catalog(self) -> None:
    changed = self.create_workspace()
    untouched = self.create_workspace()
    untouched_before = self.store.load_workspace_metadata(untouched["id"])
    with mock.patch.object(self.store, "load_metadata", side_effect=AssertionError("catalog load")):
      self.store.add_files_to_workspace(changed["id"], OWNER, [UploadedFile("new.txt", b"scoped")])
    self.assertEqual(self.store.load_workspace_metadata(untouched["id"]), untouched_before)

  def test_stale_snapshot_parent_is_rejected(self) -> None:
    workspace = self.create_workspace()
    first = self.store.load_workspace_metadata(workspace["id"])
    stale = deepcopy(first)
    parent_id = workspace["latestSnapshotId"]
    for metadata, snapshot_id in [(first, "snap_first"), (stale, "snap_stale")]:
      snapshot = deepcopy(metadata["snapshots"][parent_id])
      snapshot.update({"id": snapshot_id, "parentSnapshotId": parent_id, "reason": "test"})
      metadata["snapshots"][snapshot_id] = snapshot
      metadata["workspaces"][workspace["id"]]["latestSnapshotId"] = snapshot_id
    self.store.save_workspace_metadata(workspace["id"], first)
    with self.assertRaises(StorageError) as context:
      self.store.save_workspace_metadata(workspace["id"], stale)
    self.assertEqual(context.exception.code, "workspace_changed")


if __name__ == "__main__":
  unittest.main()
