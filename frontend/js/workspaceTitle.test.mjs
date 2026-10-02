import assert from "node:assert/strict";
import test from "node:test";
import { workspaceTitle, renderWorkspaceTitle } from "./workspaceTitle.js";
import { state } from "./state.js";

test("workspace title follows asynchronous user and group loading with safe fallbacks", () => {
  state.lang = "zh";
  const title = { textContent: "" };
  const root = { querySelector: () => title };
  renderWorkspaceTitle(root, null, []);
  assert.equal(title.textContent, "工作区");
  const user = { groupId: "example", group: "示例小组" };
  renderWorkspaceTitle(root, user, []);
  assert.equal(title.textContent, "示例小组的工作区");
  renderWorkspaceTitle(root, user, [{ id: "example", name: "更新小组" }]);
  assert.equal(title.textContent, "更新小组的工作区");
  assert.equal(workspaceTitle(user, [{ id: "example" }]), "工作区");
  assert.equal(workspaceTitle({ groupId: "example", group: "  " }), "工作区");
  assert.equal(workspaceTitle({ group: "stale" }), "工作区");
});

test("group names are rendered as text and title is localized", () => {
  const title = { textContent: "" };
  renderWorkspaceTitle({ querySelector: () => title }, { groupId: "example", group: "<example>" }, []);
  assert.equal(title.textContent, "<example>的工作区");
  state.lang = "en";
  assert.equal(workspaceTitle({ groupId: "example", group: "Team" }), "Team's workspaces");
  state.lang = "zh";
});
