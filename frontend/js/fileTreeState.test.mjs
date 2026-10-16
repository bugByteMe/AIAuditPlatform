import assert from "node:assert/strict";
import test from "node:test";
import { defaultCollapsedFolders } from "./fileTreeState.js";

test("both file trees default to collapsing only loaded folders over five direct children", () => {
  const collapsed = defaultCollapsedFolders([
    { path: "five", type: "folder", hasChildren: true, childrenLoaded: true, childCount: 5 },
    { path: "six", type: "folder", hasChildren: true, childrenLoaded: true, childCount: 6 },
    { path: "deep", type: "folder", hasChildren: true, childrenLoaded: false, childCount: 1 },
    { path: "empty", type: "folder", hasChildren: false, childrenLoaded: true, childCount: 0 },
  ]);
  assert.deepEqual([...collapsed], ["six", "deep"]);
});
