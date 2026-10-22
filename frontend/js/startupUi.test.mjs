import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { resolveStartup, setStartupScreen, dataPanelMarkup } from "./startupUi.js";

test("configuration and session must both resolve before authenticated UI appears", async () => {
  const calls = [];
  let release;
  const session = new Promise((resolve) => { release = resolve; });
  const running = resolveStartup({
    loadConfig: async () => { calls.push("config"); },
    loadSession: () => { calls.push("session"); return session; },
    authenticated: (user) => { calls.push(user.id); },
    login: () => { calls.push("login"); },
  });
  await Promise.resolve();
  assert.deepEqual(calls, ["config", "session"]);
  release({ user: { id: "synthetic-user" } });
  assert.equal(await running, true);
  assert.deepEqual(calls, ["config", "session", "synthetic-user"]);
});

test("only an unauthenticated session reveals the login UI", async () => {
  for (const status of [401, 403, 500, undefined]) {
    let login = false;
    const running = resolveStartup({ loadConfig: async () => {},
      loadSession: async () => { throw Object.assign(new Error("synthetic failure"), { status }); },
      authenticated: () => assert.fail("must not authenticate"), login: () => { login = true; } });
    if (status === 401) assert.equal(await running, false);
    else await assert.rejects(running);
    assert.equal(login, status === 401);
  }
});

test("configuration failure never falls back to local login", async () => {
  await assert.rejects(resolveStartup({
    loadConfig: async () => { throw new Error("synthetic unavailable"); },
    loadSession: () => assert.fail("configuration must succeed first"),
    authenticated: () => assert.fail("must stay hidden"), login: () => assert.fail("must stay hidden"),
  }));
});

test("startup has retry/error states and hides the screen only when ready", () => {
  const nodes = new Map(["#startup-screen", "#startup-message", "#startup-retry"].map((key) => [key, {
    setAttribute(name, value) { this[name] = value; },
  }]));
  globalThis.document = { documentElement: { dataset: {} }, querySelector: (selector) => nodes.get(selector) };
  setStartupScreen("loading", "en");
  assert.equal(document.documentElement.dataset.startup, "loading");
  assert.equal(nodes.get("#startup-screen").hidden, false);
  assert.equal(nodes.get("#startup-retry").hidden, true);
  setStartupScreen("error", "en");
  assert.equal(nodes.get("#startup-retry").hidden, false);
  assert.match(nodes.get("#startup-message").textContent, /retry/i);
  setStartupScreen("ready", "en");
  assert.equal(nodes.get("#startup-screen").hidden, true);
});

test("initial HTML guards login and application before any module executes", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /<html[^>]+data-startup="loading"/);
  assert.match(html, /html:not\(\[data-startup="ready"\]\) #auth-screen/);
  assert.match(html, /html:not\(\[data-startup="ready"\]\) #app-shell/);
  assert.match(html, /id="startup-screen"/);
});

test("data placeholders distinguish loading and failure instead of empty content", () => {
  assert.match(dataPanelMarkup("loading", "en"), /role="status".*Loading/s);
  assert.match(dataPanelMarkup("error", "en"), /role="alert".*Refresh to retry/s);
  assert.doesNotMatch(dataPanelMarkup("error", "en"), /loading-dot/);
  assert.match(dataPanelMarkup("loading", "zh"), /正在加载/);
});
