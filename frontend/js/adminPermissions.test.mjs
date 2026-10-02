import assert from "node:assert/strict";
import test from "node:test";
import { canManageAccounts, isSystemAdmin, applyAdminPermissions, accountSettingsPayload, groupSettingsPayload } from "./adminPermissions.js";

const form = new Map([['groupId', 'example'], ['role', 'system_admin'], ['maxSessions', '4'], ['name', 'Team'], ['liveRunLimit', '2'], ['diskLimitMib', '8']]);

test("management permissions are explicit and unknown roles fail closed", () => {
  for (const role of ["user", "group_admin", "unknown", undefined]) {
    assert.equal(canManageAccounts({ role }), false);
    assert.equal(isSystemAdmin({ role }), false);
  }
  assert.equal(canManageAccounts({ role: "platform_admin" }), true);
  assert.equal(isSystemAdmin({ role: "platform_admin" }), false);
  assert.equal(isSystemAdmin({ role: "system_admin" }), true);
});

test("platform saves only allowed fields even when form contains restricted values", () => {
  const actor = { role: "platform_admin" };
  assert.deepEqual(accountSettingsPayload(actor, form), { groupId: "example" });
  assert.deepEqual(groupSettingsPayload(actor, form), { name: "Team", liveRunLimit: 2 });
  assert.deepEqual(accountSettingsPayload({ role: "system_admin" }, form), { groupId: "example", role: "system_admin", maxSessions: 4 });
  assert.equal(groupSettingsPayload({ role: "system_admin" }, form).diskLimitBytes, 8 * 1024 * 1024);
});

test("restricted settings are hidden and disabled, and restored for system admins", () => {
  const classes = new Set();
  const control = { disabled: false };
  const node = {
    matches: () => false,
    querySelectorAll: () => [control],
    classList: { toggle: (name, hidden) => hidden ? classes.add(name) : classes.delete(name) },
  };
  const root = { querySelectorAll: () => [node] };
  applyAdminPermissions(root, { role: "platform_admin" });
  assert.equal(control.disabled, true);
  assert.equal(classes.has("hidden"), true);
  applyAdminPermissions(root, { role: "system_admin" });
  assert.equal(control.disabled, false);
  assert.equal(classes.has("hidden"), false);
});
