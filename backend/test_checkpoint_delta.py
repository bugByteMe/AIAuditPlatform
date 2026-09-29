from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checkpoint_delta import capture_pre_run, scan_delta, write_baseline
from workspace_store import UploadedFile, WorkspaceStore


OWNER = {"username": "owner", "role": "group_admin", "group": "audit"}


class CheckpointDeltaTest(unittest.TestCase):
  def setUp(self) -> None:
    self.tempdir = tempfile.TemporaryDirectory()
    self.store = WorkspaceStore(Path(self.tempdir.name) / "workspace_storage")
    self.workspace = self.store.create_workspace(OWNER, "Audit", True, [
      UploadedFile("workpapers/income.txt", b"initial income"),
      UploadedFile("reports/summary.md", b"# Summary\n"),
    ])

  def tearDown(self) -> None:
    self.tempdir.cleanup()

  def test_scan_skips_unchanged_files(self) -> None:
    metadata = self.store.load_workspace_metadata(self.workspace["id"])
    snapshot = metadata["snapshots"][self.workspace["latestSnapshotId"]]
    run = {"id": "run-incremental", "checkpointBaseRef": write_baseline(
      self.store.root, "run-incremental", snapshot["id"], snapshot["files"],
    )}
    run["checkpointPreRef"] = capture_pre_run(self.store.root, self.store.workspace_path(self.workspace["id"]), run)
    with mock.patch("checkpoint_delta.stream_sha256", side_effect=AssertionError("unchanged file was hashed")):
      delta = scan_delta(self.store.root, self.store.workspace_path(self.workspace["id"]), self.store.blob_dir, run)
    self.assertEqual(delta["upserts"], {})
    self.assertEqual(delta["deletes"], [])

  def test_delta_commit_preserves_untouched_rows(self) -> None:
    metadata = self.store.load_workspace_metadata(self.workspace["id"])
    workspace = metadata["workspaces"][self.workspace["id"]]
    untouched = self.store.database.file_versions(self.workspace["id"], "reports/summary.md")["current"]
    changed = self.store.workspace_path(self.workspace["id"]) / "workpapers" / "income.txt"
    changed.write_text("incremental result", encoding="utf-8")
    files = self.store.scan_workspace(self.workspace["id"])
    delta = {"version": 2, "baseSnapshotId": workspace["latestSnapshotId"], "upserts": {
      "workpapers/income.txt": files["workpapers/income.txt"],
    }, "deletes": []}
    self.store.apply_workspace_delta(metadata, workspace, "completed", "run-1", delta)
    self.assertEqual(self.store.database.file_versions(self.workspace["id"], "reports/summary.md")["current"]["blob"], untouched["blob"])
    self.assertEqual(self.store.database.file_versions(self.workspace["id"], "workpapers/income.txt")["current"]["blob"], files["workpapers/income.txt"]["blob"])


if __name__ == "__main__":
  unittest.main()
