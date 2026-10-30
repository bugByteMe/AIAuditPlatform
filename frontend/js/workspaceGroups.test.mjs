import assert from "node:assert/strict";
import test from "node:test";

import {
  buildWorkspaceHierarchy, defaultExpandedWorkspaceGroups, flattenWorkspaceHierarchy, groupWorkspaces,
  resolveWorkspaceGroup, workspaceAncestorIds, workspaceDescendantIds, workspaceGroupsMatch, workspaceNodeInitiallyExpanded,
} from "./workspaceGroups.js";

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

test("workspace hierarchy preserves sibling order and promotes missing or cyclic parents", () => {
  const items = [
    { workspace: { id: "root", parentWorkspaceId: null }, index: 0 },
    { workspace: { id: "child-new", parentWorkspaceId: "root" }, index: 1 },
    { workspace: { id: "child-old", parentWorkspaceId: "root" }, index: 2 },
    { workspace: { id: "orphan", parentWorkspaceId: "hidden" }, index: 3 },
    { workspace: { id: "cycle-a", parentWorkspaceId: "cycle-b" }, index: 4 },
    { workspace: { id: "cycle-b", parentWorkspaceId: "cycle-a" }, index: 5 },
  ];
  const roots = buildWorkspaceHierarchy(items);
  assert.deepEqual(roots.map((node) => node.workspace.id), ["root", "orphan", "cycle-a", "cycle-b"]);
  assert.deepEqual(roots[0].children.map((node) => node.workspace.id), ["child-new", "child-old"]);
  assert.equal(roots[0].children[0].depth, 1);
});

test("search context includes ancestors and default folding shows through level two", () => {
  const items = [
    { workspace: { id: "root", parentWorkspaceId: null }, index: 0 },
    { workspace: { id: "child", parentWorkspaceId: "root" }, index: 1 },
    { workspace: { id: "grandchild", parentWorkspaceId: "child" }, index: 2 },
  ];
  const matches = new Set(["grandchild"]);
  const included = new Set([...matches, ...workspaceAncestorIds(items, matches)]);
  assert.deepEqual([...included].sort(), ["child", "grandchild", "root"]);
  assert.deepEqual(flattenWorkspaceHierarchy(buildWorkspaceHierarchy(items, included)).map((node) => node.workspace.id), [
    "root", "child", "grandchild",
  ]);
  assert.equal(workspaceNodeInitiallyExpanded(0), true);
  assert.equal(workspaceNodeInitiallyExpanded(1), false);
  assert.deepEqual([...workspaceDescendantIds(items, "root")].sort(), ["child", "grandchild"]);
  assert.equal(workspaceGroupsMatch({ groupId: "group-a" }, { groupId: "group-a" }), true);
  assert.equal(workspaceGroupsMatch({ groupId: "group-a" }, { groupId: "group-b" }), false);
});
