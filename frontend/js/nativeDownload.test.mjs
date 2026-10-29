import assert from "node:assert/strict";
import test from "node:test";

import { cookiePresent, startMonitoredDownload } from "./nativeDownload.js";

test("monitored download reports preparation until response cookie arrives", () => {
  let poll = null;
  let clearedTimer = null;
  let preparing = 0;
  let started = 0;
  const anchor = {
    href: "", rel: "", style: {}, clicked: false,
    click() { this.clicked = true; },
    remove() {},
  };
  const documentObject = {
    cookie: "",
    body: { append(node) { assert.equal(node, anchor); } },
    createElement(name) { assert.equal(name, "a"); return anchor; },
  };
  const result = startMonitoredDownload("/api/workspaces/ws/download?mode=full", {
    documentObject,
    locationObject: { href: "https://audit.example.test/workspace" },
    cryptoObject: { randomUUID: () => "01234567-89ab-cdef-0123-456789abcdef" },
    setIntervalFunction(callback) { poll = callback; return 7; },
    clearIntervalFunction(timer) { clearedTimer = timer; },
    setTimeoutFunction() {},
    onPreparing() { preparing += 1; },
    onStarted() { started += 1; },
  });

  assert.equal(preparing, 1);
  assert.equal(started, 0);
  assert.equal(anchor.clicked, true);
  assert.equal(new URL(anchor.href).searchParams.get("downloadToken"), result.token);
  documentObject.cookie = `ai_audit_download_${result.token}=1`;
  poll();
  assert.equal(started, 1);
  assert.equal(clearedTimer, 7);
});

test("cookie matcher does not accept partial download tokens", () => {
  assert.equal(cookiePresent("ai_audit_download_abc=1", "ai_audit_download_abc"), true);
  assert.equal(cookiePresent("ai_audit_download_abcdef=1", "ai_audit_download_abc"), false);
});
