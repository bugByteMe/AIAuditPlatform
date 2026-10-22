import assert from "node:assert/strict";
import test from "node:test";
import { configureIdentityUi, renderCustomers } from "./identityUi.js";

function dom() {
  const node = () => ({
    classes: new Set(), disabled: false, value: "", textContent: "",
    classList: { toggle(name, hidden) { this.last = [name, hidden]; } },
    replaceChildren(...children) { this.children = children; },
  });
  const inputs = [node(), node()];
  inputs.forEach((input) => { input.closest = () => node(); });
  const nodes = new Map();
  globalThis.document = {
    querySelectorAll: (selector) => selector === "#login-form input" ? inputs : [],
    querySelector: (selector) => {
      if (!nodes.has(selector)) nodes.set(selector, node());
      return nodes.get(selector);
    },
    createElement: node,
  };
  return { inputs, nodes };
}

test("OIDC sign-in disables legacy password controls and offers existing identity flow", () => {
  const { inputs, nodes } = dom();
  configureIdentityUi({ lang: "en", runtimeConfig: { authMode: "oidc" } }, async () => ({}));
  assert.ok(inputs.every((input) => input.disabled));
  assert.equal(nodes.get('#login-form button[type="submit"]').textContent, "Sign in with shared account");
  const invite = document.querySelector('#register-form [name="inviteToken"]');
  invite.value = "synthetic invite";
  let location;
  globalThis.window = { location: { assign: (value) => { location = value; } } };
  nodes.get("#identity-existing-login").onclick();
  assert.equal(location, "/api/oidc/login?inviteToken=synthetic%20invite");
});

test("dedicated customer creation is hidden and customer names use text nodes", () => {
  const { nodes } = dom();
  configureIdentityUi({ lang: "en", runtimeConfig: { authMode: "oidc", deploymentCustomerId: "business" } }, async () => ({}));
  assert.deepEqual(nodes.get("#identity-add-customer").classList.last, ["hidden", true]);
  renderCustomers([{ id: "business", name: "<synthetic>" }], "business");
  const option = nodes.get("#identity-customer-select").children.find((item) => item.value === "business");
  assert.equal(option.textContent, "<synthetic> (business)");
  assert.equal(option.selected, true);
});
