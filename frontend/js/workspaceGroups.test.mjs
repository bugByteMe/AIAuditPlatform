import assert from "node:assert/strict";
import test from "node:test";

import { defaultExpandedWorkspaceGroups, groupWorkspaces, resolveWorkspaceGroup } from "./workspaceGroups.js";

const user = { username: "admin", groupId: "group-a", group: "Audit A" };
const groups = [{ id: "group-a", name: "Audit A" }, { id: "group-b", name: "Audit B" }];
const accounts = [
  { username: "owner-a", groupId: "group-a" },
  { username: "owner-b", groupId: "group-b" },
];

test("admin workspaces group by the owner's current group with own group first", () => {
  const workspaces = [
    { workspace: { id: "b", owner: "owner-b", group: "Old B" }, index: 0 },
    { workspace: { id: "a", owner: "owner-a", group: "Old A" }, index: 1 },
  ];
  const sections = groupWorkspaces(workspaces, user, groups, accounts);
  assert.deepEqual(sections.map((section) => [section.id, section.name, section.own]), [
    ["group-a", "Audit A", true],
    ["group-b", "Audit B", false],
  ]);
  assert.equal(sections[0].workspaces[0].index, 1);
  assert.deepEqual([...defaultExpandedWorkspaceGroups(sections)], ["group-a"]);
});

test("stored workspace group names remain a safe fallback before accounts load", () => {
  assert.deepEqual(resolveWorkspaceGroup({ owner: "unknown", group: "Audit A" }, user, groups, []), {
    key: "group-a", id: "group-a", name: "Audit A", own: true,
  });
});
