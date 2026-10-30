import test from "node:test";
import assert from "node:assert/strict";
import {
  filterCollaboratingWorkspaces,
  reconcileCollaboratingWorkspaceIds,
} from "./collaboratingWorkspaces.js";

const candidates = [
  { id: "ws_prior", name: "Prior year" },
  { id: "ws_contracts", name: "合同资料" },
];

test("collaborating workspace selection keeps eligible IDs in user order", () => {
  assert.deepEqual(
    reconcileCollaboratingWorkspaceIds(["ws_contracts", "missing", "ws_contracts", "ws_prior"], candidates),
    ["ws_contracts", "ws_prior"],
  );
});

test("collaborating workspace search matches names and stable IDs", () => {
  assert.deepEqual(filterCollaboratingWorkspaces(candidates, "PRIOR"), [candidates[0]]);
  assert.deepEqual(filterCollaboratingWorkspaces(candidates, "contracts"), [candidates[1]]);
  assert.deepEqual(filterCollaboratingWorkspaces(candidates, "不存在"), []);
});
