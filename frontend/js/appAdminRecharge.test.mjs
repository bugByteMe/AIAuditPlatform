import assert from "node:assert/strict";
import test from "node:test";
import { createAdminRecharge } from "./appAdminRecharge.js";

function controller(role = "system_admin") {
  let resolveBalance;
  let rejectBalance;
  const balance = new Promise((resolve, reject) => { resolveBalance = resolve; rejectBalance = reject; });
  const calls = [];
  const state = { user: { id: "admin", role }, runtimeConfig: {}, accounts: [] };
  const app = createAdminRecharge({ state, api: async (path) => {
    calls.push(path);
    if (path === "/api/accounts/balances") return balance;
    if (path === "/api/accounts") return { accounts: [{ id: "member", status: "active", budget: { source: "micu", remaining: "1.00" } }], groups: [] };
    return {};
  }, canManageAccounts: () => true, isSystemAdmin: (user) => user.role === "system_admin",
  renderAdmin() {}, renderWorkers() {}, renderWorkspaces() {} });
  return { app, state, calls, resolveBalance, rejectBalance };
}

test("account list becomes available while provider balances are still pending", async () => {
  const c = controller();
  await c.app.loadAccountControlData();
  assert.equal(c.state.adminLoading, false);
  assert.equal(c.state.accounts.length, 1);
  assert.equal(c.state.adminBalancesLoading, true);
  c.resolveBalance({ balances: [{ id: "member", budget: { source: "micu", remaining: "2.00" } }] });
  await new Promise(setImmediate);
  assert.equal(c.state.adminBalancesLoading, false);
  assert.equal(c.state.accounts[0].budget.remaining, "2.00");
});

test("provider failure leaves the account list usable and ends the placeholder", async () => {
  const c = controller();
  await c.app.loadAccountControlData();
  c.rejectBalance(new Error("provider unavailable"));
  await new Promise(setImmediate);
  assert.equal(c.state.adminLoadError, false);
  assert.equal(c.state.adminBalancesLoading, false);
  assert.equal(c.state.accounts[0].budget.remaining, null);
});

test("platform administrators never request system-only balance amounts", async () => {
  const c = controller("platform_admin");
  await c.app.loadAccountControlData();
  assert.equal(c.state.adminBalancesLoading, false);
  assert.equal(c.calls.includes("/api/accounts/balances"), false);
});

test("a balance response cannot update a different signed-in user's accounts", async () => {
  const c = controller();
  await c.app.loadAccountControlData();
  c.state.user = { id: "other", role: "system_admin" };
  c.resolveBalance({ balances: [{ id: "member", budget: { remaining: "2.00" } }] });
  await new Promise(setImmediate);
  assert.equal(c.state.accounts[0].budget.remaining, "1.00");
});
