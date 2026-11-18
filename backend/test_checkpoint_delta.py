from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checkpoint_delta import capture_pre_run, normalize_workspace_filenames, read_json_ref, scan_delta, write_baseline, write_result
from workspace_common import StorageError
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

  def test_agent_created_shell_script_is_checkpointed(self) -> None:
    metadata = self.store.load_workspace_metadata(self.workspace["id"])
    workspace = metadata["workspaces"][self.workspace["id"]]
    snapshot = metadata["snapshots"][workspace["latestSnapshotId"]]
    run = {"id": "run-shell-script", "checkpointBaseRef": write_baseline(
      self.store.root, "run-shell-script", snapshot["id"], snapshot["files"],
    )}
    run["checkpointPreRef"] = capture_pre_run(self.store.root, self.store.workspace_path(self.workspace["id"]), run)
    script = self.store.workspace_path(self.workspace["id"]) / "scripts" / "reconcile.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\necho reconciled\n", encoding="utf-8")
    script.chmod(0o755)

    delta = scan_delta(self.store.root, self.store.workspace_path(self.workspace["id"]), self.store.blob_dir, run)
    self.assertIn("scripts/reconcile.sh", delta["upserts"])
    self.assertEqual(delta["upserts"]["scripts/reconcile.sh"]["mode"], 0o755)
    self.store.apply_workspace_delta(metadata, workspace, "completed", "run-shell-script", delta)
    current = self.store.database.file_versions(self.workspace["id"], "scripts/reconcile.sh")["current"]
    self.assertEqual(current["mode"], 0o755)
    self.assertEqual(current["checksum"], delta["upserts"]["scripts/reconcile.sh"]["checksum"])

  def test_gb18030_filename_is_normalized_before_checkpoint(self) -> None:
    metadata = self.store.load_workspace_metadata(self.workspace["id"])
    snapshot = metadata["snapshots"][self.workspace["latestSnapshotId"]]
    run = {"id": "run-gbk", "checkpointBaseRef": write_baseline(
      self.store.root, "run-gbk", snapshot["id"], snapshot["files"],
    )}
    workspace_path = self.store.workspace_path(self.workspace["id"])
    raw_name = "2026年审计.xls".encode("gb18030")
    raw_path = os.path.join(os.fsencode(workspace_path), raw_name)
    descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.write(descriptor, b"legacy filename")
    os.close(descriptor)

    run["checkpointPreRef"] = capture_pre_run(self.store.root, workspace_path, run)
    normalized = workspace_path / "2026年审计.xls"
    self.assertTrue(normalized.is_file())
    self.assertFalse(os.path.lexists(raw_path))
    delta = scan_delta(self.store.root, workspace_path, self.store.blob_dir, run)
    self.assertIn("2026年审计.xls", delta["upserts"])

  def test_filename_normalization_does_not_follow_symlinks(self) -> None:
    workspace_path = self.store.workspace_path(self.workspace["id"])
    outside = Path(self.tempdir.name) / "outside"
    outside.mkdir()
    raw_name = "审计".encode("gb18030")
    raw_path = os.path.join(os.fsencode(outside), raw_name)
    os.mkdir(raw_path)
    (workspace_path / "outside-link").symlink_to(outside, target_is_directory=True)

    report = normalize_workspace_filenames(workspace_path)
    self.assertEqual(report["renameCount"], 0)
    self.assertTrue(os.path.isdir(raw_path))

  def test_filename_normalization_rejects_collision_without_changes(self) -> None:
    workspace_path = self.store.workspace_path(self.workspace["id"])
    target = workspace_path / "财务报表.xls"
    target.write_bytes(b"utf8")
    raw_path = os.path.join(os.fsencode(workspace_path), "财务报表.xls".encode("gb18030"))
    descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.write(descriptor, b"gbk")
    os.close(descriptor)

    with self.assertRaises(StorageError) as context:
      normalize_workspace_filenames(workspace_path)
    self.assertEqual(context.exception.code, "filename_encoding_collision")
    self.assertTrue(target.is_file())
    self.assertTrue(os.path.lexists(raw_path))

  def test_filename_normalization_rejects_unknown_bytes(self) -> None:
    workspace_path = self.store.workspace_path(self.workspace["id"])
    raw_path = os.path.join(os.fsencode(workspace_path), b"invalid-\xff")
    descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.close(descriptor)
    with self.assertRaises(StorageError) as context:
      normalize_workspace_filenames(workspace_path, apply=False)
    self.assertEqual(context.exception.code, "invalid_filename_encoding")

  def test_checkpoint_json_escapes_surrogates_as_failure_safety_net(self) -> None:
    reference = write_result(self.store.root, "run-surrogate", {"path": "bad\udcff-name"})
    self.assertEqual(read_json_ref(self.store.root, reference)["path"], "bad\udcff-name")


if __name__ == "__main__":
  unittest.main()
