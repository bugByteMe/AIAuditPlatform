import assert from "node:assert/strict";
import test from "node:test";

import { adminGroupMatches } from "./adminSearch.js";

const group = { id: "audit-east", name: "East Audit" };
const accounts = [{ id: "one", username: "Alice.Reviewer" }, { id: "two", username: "bob.audit" }];

test("admin groups match partial group names case-insensitively", () => {
  assert.equal(adminGroupMatches(group, accounts, "AUD"), true);
  assert.equal(adminGroupMatches(group, accounts, "west"), false);
});

test("admin groups match a partial username within the group", () => {
  assert.equal(adminGroupMatches(group, accounts, "review"), true);
  assert.equal(adminGroupMatches(group, accounts, "charlie"), false);
});

test("an empty admin search includes every group", () => {
  assert.equal(adminGroupMatches(group, accounts, "  "), true);
});
