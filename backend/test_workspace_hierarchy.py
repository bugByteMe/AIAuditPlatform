from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workspace_store import StorageError, WorkspaceStore


GROUP_A = {"id": "group-a", "name": "Audit A"}
GROUP_B = {"id": "group-b", "name": "Audit B"}
OWNER = {"id": "owner-a", "username": "owner", "role": "user", "groupId": "group-a", "group": "Audit A", "enabled": True}
OTHER = {"id": "other-a", "username": "other", "role": "user", "groupId": "group-a", "group": "Audit A", "enabled": True}
OUTSIDER = {"id": "owner-b", "username": "outsider", "role": "user", "groupId": "group-b", "group": "Audit B", "enabled": True}
ADMIN = {"id": "admin", "username": "admin", "role": "system_admin", "groupId": "group-a", "group": "Audit A", "enabled": True}


class WorkspaceHierarchyTest(unittest.TestCase):
  def setUp(self) -> None:
    self.tempdir = tempfile.TemporaryDirectory()
    self.store = WorkspaceStore(Path(self.tempdir.name) / "workspace_storage")
    self.users = {user["username"]: dict(user) for user in [OWNER, OTHER, OUTSIDER, ADMIN]}
    self.groups = {group["id"]: dict(group) for group in [GROUP_A, GROUP_B]}
    self.store.set_account_provider(lambda: (self.users, self.groups))

  def tearDown(self) -> None:
    self.tempdir.cleanup()

  def create(self, owner: dict, name: str, parent_id: str | None = None, shared: bool = False) -> dict:
    return self.store.create_workspace(
      owner, name, shared, [], parent_workspace_id=parent_id,
    )

  def test_create_move_detach_and_cycle_validation(self) -> None:
    root = self.create(OWNER, "Root")
    child = self.create(OWNER, "Child", root["id"])
    grandchild = self.create(OWNER, "Grandchild", child["id"])
    self.assertEqual(child["parentWorkspaceId"], root["id"])

    moved = self.store.update_workspace(grandchild["id"], OWNER, {"parentWorkspaceId": root["id"]})
    self.assertEqual(moved["parentWorkspaceId"], root["id"])
    detached = self.store.update_workspace(grandchild["id"], OWNER, {"parentWorkspaceId": None})
    self.assertIsNone(detached["parentWorkspaceId"])

    with self.assertRaises(StorageError) as self_parent:
      self.store.update_workspace(root["id"], OWNER, {"parentWorkspaceId": root["id"]})
    self.assertEqual(self_parent.exception.code, "bad_request")
    with self.assertRaises(StorageError) as cycle:
      self.store.update_workspace(root["id"], OWNER, {"parentWorkspaceId": child["id"]})
    self.assertEqual(cycle.exception.code, "bad_request")

  def test_parent_requires_same_group_visibility_and_management_authority(self) -> None:
    root = self.create(OWNER, "Root", shared=True)
    child = self.create(OWNER, "Child", shared=True)
    foreign = self.create(OUTSIDER, "Foreign")

    with self.assertRaises(StorageError) as collaborator:
      self.store.update_workspace(child["id"], OTHER, {"parentWorkspaceId": root["id"]})
    self.assertEqual(collaborator.exception.code, "forbidden")
    with self.assertRaises(StorageError) as cross_group:
      self.store.update_workspace(child["id"], ADMIN, {"parentWorkspaceId": foreign["id"]})
    self.assertEqual(cross_group.exception.code, "bad_request")

  def test_delete_promotes_children_and_fork_inherits_source_parent(self) -> None:
    root = self.create(OWNER, "Root")
    source = self.create(OWNER, "Source", root["id"], shared=True)
    child = self.create(OWNER, "Child", source["id"])
    fork = self.store.fork_workspace(source["id"], OWNER, "Fork")
    self.assertEqual(fork["parentWorkspaceId"], root["id"])

    self.store.delete_workspace(source["id"], OWNER)
    self.assertIsNone(self.store.database.get_workspace_header(child["id"])["parentWorkspaceId"])
    self.assertEqual(self.store.database.get_workspace_header(fork["id"])["parentWorkspaceId"], root["id"])

    foreign_root = self.create(OUTSIDER, "Foreign root")
    foreign_source = self.create(OUTSIDER, "Foreign source", foreign_root["id"])
    admin_fork = self.store.fork_workspace(foreign_source["id"], ADMIN, "Admin fork")
    self.assertIsNone(admin_fork["parentWorkspaceId"])

  def test_group_transfer_preserves_internal_links_and_clears_cross_group_links(self) -> None:
    root = self.create(OWNER, "Root", shared=True)
    owned_child = self.create(OWNER, "Owned child", root["id"])
    other_child = self.create(OTHER, "Other child", root["id"])

    self.store.database.transfer_owner_workspaces(OWNER["username"], GROUP_B["id"], GROUP_B["name"])
    self.assertEqual(self.store.database.get_workspace_header(owned_child["id"])["parentWorkspaceId"], root["id"])
    self.assertIsNone(self.store.database.get_workspace_header(other_child["id"])["parentWorkspaceId"])

  def test_collaborating_candidates_include_visible_descendant_through_hidden_parent(self) -> None:
    root = self.create(OWNER, "Root", shared=True)
    hidden_parent = self.create(OWNER, "Hidden parent", root["id"], shared=False)
    visible_leaf = self.create(OWNER, "Visible leaf", hidden_parent["id"], shared=True)

    candidates = self.store.collaborating_workspace_candidates(root["id"], OTHER)
    self.assertEqual(candidates, [{"id": visible_leaf["id"], "name": "Visible leaf"}])
    resolved = self.store.resolve_collaborating_workspaces(root["id"], OTHER, [visible_leaf["id"]])
    self.assertEqual(resolved[0]["mountPath"], f"/collaborating-workspaces/{visible_leaf['id']}")


if __name__ == "__main__":
  unittest.main()
