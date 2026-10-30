from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workspace_store import StorageError, UploadedFile, WorkspaceStore


GROUP_A = {"id": "group-a", "name": "Audit A"}
GROUP_B = {"id": "group-b", "name": "Audit B"}
OWNER = {"id": "user-owner", "username": "owner", "role": "user", "groupId": "group-a", "group": "Audit A", "enabled": True}
COLLABORATOR = {"id": "user-collaborator", "username": "collaborator", "role": "user", "groupId": "group-a", "group": "Audit A", "enabled": True}
UNSELECTED = {"id": "user-unselected", "username": "unselected", "role": "user", "groupId": "group-a", "group": "Audit A", "enabled": True}
OUTSIDER = {"id": "user-outsider", "username": "outsider", "role": "user", "groupId": "group-b", "group": "Audit B", "enabled": True}
DISABLED = {"id": "user-disabled", "username": "disabled", "role": "user", "groupId": "group-a", "group": "Audit A", "enabled": False}


class WorkspaceAccessTest(unittest.TestCase):
  def setUp(self) -> None:
    self.tempdir = tempfile.TemporaryDirectory()
    self.store = WorkspaceStore(Path(self.tempdir.name) / "workspace_storage")
    self.users = {account["username"]: dict(account) for account in [OWNER, COLLABORATOR, UNSELECTED, OUTSIDER, DISABLED]}
    self.groups = {group["id"]: dict(group) for group in [GROUP_A, GROUP_B]}
    self.store.set_account_provider(lambda: (self.users, self.groups))

  def tearDown(self) -> None:
    self.tempdir.cleanup()

  def test_restricted_visibility_lists_only_selected_group_members(self) -> None:
    workspace = self.store.create_workspace(
      OWNER, "Restricted", False, [UploadedFile("input.txt", b"data")],
      visibility="restricted", user_ids=[COLLABORATOR["id"]],
    )

    self.assertEqual([item["id"] for item in self.store.list_workspaces(COLLABORATOR)], [workspace["id"]])
    self.assertEqual(self.store.list_workspaces(UNSELECTED), [])
    self.assertEqual(self.store.list_workspaces(OUTSIDER), [])
    with self.assertRaises(StorageError):
      self.store.get_workspace(workspace["id"], UNSELECTED)

  def test_group_collaborator_can_upload_and_delete_files(self) -> None:
    workspace = self.store.create_workspace(OWNER, "Group", True, [UploadedFile("input.txt", b"data")])

    updated = self.store.add_files_to_workspace(
      workspace["id"], COLLABORATOR, [UploadedFile("shared/output.txt", b"result")]
    )
    self.assertTrue(any(item["path"] == "shared/output.txt" for item in updated["files"]))
    updated = self.store.delete_workspace_path(workspace["id"], COLLABORATOR, "shared/output.txt")
    self.assertFalse(any(item["path"] == "shared/output.txt" for item in updated["files"]))

    with self.assertRaises(StorageError) as context:
      self.store.update_workspace_access(
        workspace["id"], {**COLLABORATOR, "role": "group_admin"},
        {"visibility": "private", "userIds": []},
      )
    self.assertEqual(context.exception.code, "forbidden")

  def test_access_replacement_and_owner_group_transfer_fail_closed(self) -> None:
    workspace = self.store.create_workspace(OWNER, "Access", False, [], visibility="private")
    updated, access, added, removed = self.store.update_workspace_access(
      workspace["id"], OWNER, {"visibility": "restricted", "userIds": [COLLABORATOR["id"]]},
    )
    self.assertEqual(updated["visibility"], "restricted")
    self.assertEqual({item["id"] for item in access["selectedUsers"]}, {COLLABORATOR["id"]})
    self.assertEqual(added, {COLLABORATOR["id"]})
    self.assertEqual(removed, set())

    self.store.database.transfer_owner_workspaces(OWNER["username"], GROUP_B["id"], GROUP_B["name"])
    transferred = self.store.database.get_workspace_header(workspace["id"])
    self.assertEqual(transferred["visibility"], "private")
    self.assertEqual(transferred["groupId"], GROUP_B["id"])
    self.assertEqual(self.store.database.access_grant_ids(workspace["id"]), set())
    with self.assertRaises(StorageError):
      self.store.get_workspace(workspace["id"], COLLABORATOR)

  def test_restricted_visibility_rejects_empty_and_cross_group_grants(self) -> None:
    for user_ids in ([], [OUTSIDER["id"]], [OWNER["id"]], [DISABLED["id"]]):
      with self.subTest(user_ids=user_ids), self.assertRaises(StorageError) as context:
        self.store.create_workspace(OWNER, "Invalid", False, [], visibility="restricted", user_ids=user_ids)
      self.assertEqual(context.exception.code, "bad_request")


if __name__ == "__main__":
  unittest.main()
