from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workspace_store import StorageError, UploadedFile, WorkspaceStore, normalize_relative_path, parse_multipart
from server import ascii_download_filename


ADMIN = {"username": "chen.audit", "role": "system_admin", "group": "审计一组"}
OWNER = {"username": "li.review", "role": "group_admin", "group": "审计一组"}
SAME_GROUP = {"username": "wang.audit", "role": "user", "group": "审计一组"}
OTHER_GROUP = {"username": "zhao.audit", "role": "user", "group": "审计二组"}


class WorkspaceStoreTest(unittest.TestCase):
  def setUp(self) -> None:
    self.tempdir = tempfile.TemporaryDirectory()
    self.store = WorkspaceStore(Path(self.tempdir.name) / "workspace_storage")

  def tearDown(self) -> None:
    self.tempdir.cleanup()

  def create_workspace(self, shared: bool = True) -> dict:
    return self.store.create_workspace(
      OWNER,
      "Audit Upload",
      shared,
      [
        UploadedFile("workpapers/income.txt", b"initial income"),
        UploadedFile("reports/summary.md", b"# Summary\n"),
      ],
    )

  def create_preview_workspace(self) -> dict:
    return self.store.create_workspace(
      OWNER,
      "Preview Files",
      True,
      [
        UploadedFile("notes/readme.txt", b"hello preview"),
        UploadedFile("images/pixel.png", b"\x89PNG\r\n\x1a\n"),
        UploadedFile("docs/manual.pdf", b"%PDF-1.4\n"),
        UploadedFile("sheets/report.xlsx", b"not a real workbook"),
      ],
    )

  def test_ascii_download_filename_strips_non_latin_header_chars(self) -> None:
    filename = ascii_download_filename("EK 其他流动资产（新准则）.xlsx")
    filename.encode("latin-1")
    self.assertEqual(filename, "EK____________.xlsx")

  def test_workspace_creation_writes_files_and_initial_snapshot(self) -> None:
    workspace = self.create_workspace()
    metadata = self.store.load_metadata()
    self.assertEqual(workspace["fileCount"], 2)
    self.assertEqual(workspace["sessions"], [])
    self.assertIn(workspace["id"], metadata["workspaces"])
    self.assertTrue((self.store.workspace_path(workspace["id"]) / "workpapers" / "income.txt").exists())
    snapshot = metadata["snapshots"][workspace["latestSnapshotId"]]
    self.assertIn("workpapers/income.txt", snapshot["files"])
    self.assertTrue(self.store.blob_path(snapshot["files"]["workpapers/income.txt"]["blob"]).exists())

  def test_duplicate_workspace_names_create_distinct_generated_paths(self) -> None:
    first = self.store.create_workspace(OWNER, "Same Name", False, [UploadedFile("folder/a.txt", b"one")])
    second = self.store.create_workspace(OWNER, "Same Name", False, [UploadedFile("folder/a.txt", b"two")])
    self.assertNotEqual(first["id"], second["id"])
    self.assertEqual(first["name"], second["name"])
    self.assertEqual((self.store.workspace_path(first["id"]) / "folder" / "a.txt").read_text(encoding="utf-8"), "one")
    self.assertEqual((self.store.workspace_path(second["id"]) / "folder" / "a.txt").read_text(encoding="utf-8"), "two")

  def test_workspace_fork_does_not_create_setup_chat_session(self) -> None:
    source = self.create_workspace()
    fork = self.store.fork_workspace(source["id"], OWNER, "Forked")
    self.assertEqual(fork["sessions"], [])

  def test_unsafe_upload_path_is_rejected(self) -> None:
    with self.assertRaises(StorageError):
      self.store.create_workspace(OWNER, "Bad", False, [UploadedFile("../escape.txt", b"nope")])
    with self.assertRaises(StorageError):
      normalize_relative_path("/absolute.txt")

  def test_multipart_upload_rejects_unsafe_relative_path(self) -> None:
    boundary = "test-boundary"
    body = (
      f"--{boundary}\r\n"
      'Content-Disposition: form-data; name="name"\r\n\r\nBad\r\n'
      f"--{boundary}\r\n"
      'Content-Disposition: form-data; name="paths"\r\n\r\n../bad.txt\r\n'
      f"--{boundary}\r\n"
      'Content-Disposition: form-data; name="files"; filename="bad.txt"\r\n'
      "Content-Type: text/plain\r\n\r\npayload\n\r\n"
      f"--{boundary}--\r\n"
    ).encode()
    fields, files = parse_multipart(f"multipart/form-data; boundary={boundary}", body)
    self.assertEqual(fields["name"], "Bad")
    self.assertEqual(files[0].content, b"payload\n")
    with self.assertRaises(StorageError):
      self.store.create_workspace(OWNER, "Bad", False, files)

  def test_file_tree_lists_folders_and_files(self) -> None:
    workspace = self.create_workspace()
    paths = {item["path"]: item for item in self.store.file_tree(workspace["id"])}
    self.assertEqual(paths["workpapers"]["type"], "folder")
    self.assertEqual(paths["workpapers/income.txt"]["type"], "file")

  def test_file_preview_rejects_unsafe_and_missing_paths(self) -> None:
    workspace = self.create_preview_workspace()
    with self.assertRaises(StorageError):
      self.store.file_metadata(workspace["id"], "../secret.txt")
    with self.assertRaises(StorageError):
      self.store.file_metadata(workspace["id"], "missing.txt")

  def test_text_preview_returns_capped_decoded_content(self) -> None:
    workspace = self.store.create_workspace(OWNER, "Long Text", True, [UploadedFile("long.txt", b"a" * (300 * 1024))])
    preview = self.store.preview_metadata(workspace["id"], "long.txt")
    self.assertEqual(preview["mode"], "text")
    self.assertTrue(preview["truncated"])
    self.assertLessEqual(len(preview["text"].encode("utf-8")), 256 * 1024)

  def test_image_pdf_and_office_preview_modes(self) -> None:
    workspace = self.create_preview_workspace()
    self.assertEqual(self.store.preview_metadata(workspace["id"], "images/pixel.png")["mode"], "image")
    self.assertEqual(self.store.preview_metadata(workspace["id"], "docs/manual.pdf")["mode"], "pdf")
    self.assertEqual(self.store.preview_metadata(workspace["id"], "sheets/report.xlsx")["mode"], "office")

  def test_office_preview_cache_path_stays_under_storage(self) -> None:
    workspace = self.create_preview_workspace()
    metadata = self.store.file_metadata(workspace["id"], "sheets/report.xlsx")
    cache_dir = self.store.preview_dir / workspace["id"] / metadata["blob"]
    cache_dir.mkdir(parents=True)
    cached = cache_dir / "preview.pdf"
    cached.write_bytes(b"%PDF cached")
    self.assertEqual(self.store.rendered_preview_path(workspace["id"], "sheets/report.xlsx"), cached)

  def test_fork_materializes_independent_workspace(self) -> None:
    source = self.create_workspace(shared=True)
    fork = self.store.fork_workspace(source["id"], SAME_GROUP, "Forked")
    source_file = self.store.workspace_path(source["id"]) / "workpapers" / "income.txt"
    fork_file = self.store.workspace_path(fork["id"]) / "workpapers" / "income.txt"
    fork_file.write_text("changed in fork", encoding="utf-8")
    self.assertEqual(source_file.read_text(encoding="utf-8"), "initial income")
    self.assertEqual(fork_file.read_text(encoding="utf-8"), "changed in fork")

  def test_permissions_for_private_and_shared_workspaces(self) -> None:
    private = self.create_workspace(shared=False)
    shared = self.create_workspace(shared=True)
    self.assertEqual(len(self.store.list_workspaces(OWNER)), 2)
    self.assertEqual([item["id"] for item in self.store.list_workspaces(SAME_GROUP)], [shared["id"]])
    self.assertEqual(self.store.list_workspaces(OTHER_GROUP), [])
    self.assertEqual(len(self.store.list_workspaces(ADMIN)), 2)
    with self.assertRaises(StorageError):
      self.store.get_workspace(private["id"], SAME_GROUP)

  def test_delete_workspace_requires_owner_or_admin_and_removes_active_files(self) -> None:
    workspace = self.create_workspace(shared=True)
    preview_cache = self.store.preview_dir / workspace["id"] / "cache"
    preview_cache.mkdir(parents=True)
    (preview_cache / "preview.pdf").write_bytes(b"cached")
    with self.assertRaises(StorageError):
      self.store.delete_workspace(workspace["id"], SAME_GROUP)
    deleted = self.store.delete_workspace(workspace["id"], OWNER)
    self.assertEqual(deleted["id"], workspace["id"])
    self.assertFalse(self.store.workspace_path(workspace["id"]).exists())
    self.assertFalse((self.store.preview_dir / workspace["id"]).exists())
    self.assertEqual(self.store.load_metadata()["workspaces"], {})

  def test_add_files_to_workspace_refreshes_tree_and_artifacts(self) -> None:
    workspace = self.create_workspace(shared=True)
    updated = self.store.add_files_to_workspace(workspace["id"], OWNER, [UploadedFile("uploads/new.txt", b"new")])
    paths = {item["path"] for item in updated["files"]}
    artifact_statuses = {item["path"]: item["status"] for item in updated["artifacts"]}
    self.assertIn("uploads/new.txt", paths)
    self.assertEqual(artifact_statuses["uploads/new.txt"], "added")
    self.assertNotEqual(workspace["latestSnapshotId"], updated["latestSnapshotId"])

  def test_add_files_to_workspace_requires_owner_or_admin(self) -> None:
    workspace = self.create_workspace(shared=True)
    with self.assertRaises(StorageError):
      self.store.add_files_to_workspace(workspace["id"], SAME_GROUP, [UploadedFile("uploads/new.txt", b"new")])

  def test_delete_workspace_path_removes_file_or_folder_and_records_diff(self) -> None:
    workspace = self.create_workspace(shared=True)
    updated = self.store.delete_workspace_path(workspace["id"], OWNER, "workpapers")
    paths = {item["path"] for item in updated["files"]}
    artifact_statuses = {item["path"]: item["status"] for item in updated["artifacts"]}
    self.assertNotIn("workpapers/income.txt", paths)
    self.assertEqual(artifact_statuses["workpapers/income.txt"], "deleted")

  def test_refresh_artifacts_detects_added_modified_and_deleted(self) -> None:
    workspace = self.create_workspace()
    root = self.store.workspace_path(workspace["id"])
    (root / "workpapers" / "income.txt").write_text("changed", encoding="utf-8")
    (root / "reports" / "summary.md").unlink()
    (root / "output").mkdir()
    (root / "output" / "findings.md").write_text("new finding", encoding="utf-8")
    artifacts = self.store.refresh_artifacts(workspace["id"], OWNER)
    statuses = {item["path"]: item["status"] for item in artifacts}
    self.assertEqual(statuses["workpapers/income.txt"], "modified")
    self.assertEqual(statuses["reports/summary.md"], "deleted")
    self.assertEqual(statuses["output/findings.md"], "added")

  def test_download_filters_selected_changes_and_writes_manifest(self) -> None:
    workspace = self.create_workspace()
    root = self.store.workspace_path(workspace["id"])
    (root / "workpapers" / "income.txt").write_text("changed", encoding="utf-8")
    (root / "output.txt").write_text("new", encoding="utf-8")
    self.store.refresh_artifacts(workspace["id"], OWNER)
    filename, body = self.store.build_download_zip(workspace["id"], OWNER, "changes", ["output.txt"])
    self.assertTrue(filename.endswith("-changes.zip"))
    with zipfile.ZipFile(BytesIO(body)) as archive:
      names = archive.namelist()
      self.assertTrue(any(name.endswith("/manifest.json") for name in names))
      self.assertTrue(any(name.endswith("/files/output.txt") for name in names))
      self.assertFalse(any(name.endswith("/files/workpapers/income.txt") for name in names))
      manifest_name = next(name for name in names if name.endswith("/manifest.json"))
      manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
      self.assertEqual([item["path"] for item in manifest["included"]], ["output.txt"])

  def test_download_rejects_non_downloadable_path(self) -> None:
    workspace = self.create_workspace()
    with self.assertRaises(StorageError):
      self.store.build_download_zip(workspace["id"], OWNER, "changes", ["../secret.txt"])


if __name__ == "__main__":
  unittest.main()
