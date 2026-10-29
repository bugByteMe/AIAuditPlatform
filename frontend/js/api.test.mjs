import assert from "node:assert/strict";
import test from "node:test";

const storage = new Map();

globalThis.window = {
  location: {
    protocol: "https:",
    host: "audit.example.test",
    hostname: "audit.example.test",
    href: "https://audit.example.test/",
  },
  localStorage: {
    getItem(key) { return storage.get(key) ?? null; },
    setItem(key, value) { storage.set(key, String(value)); },
    removeItem(key) { storage.delete(key); },
  },
  addEventListener(name, callback) {
    this.listeners ||= new Map();
    const callbacks = this.listeners.get(name) || [];
    callbacks.push(callback);
    this.listeners.set(name, callbacks);
  },
  dispatchEvent(event) {
    (this.listeners?.get(event.type) || []).forEach((callback) => callback(event));
  },
};

const { api, uploadChunkApi } = await import("./api.js");
const { state } = await import("./state.js");

test("OIDC runtime configuration discards legacy bearer tokens", async () => {
  storage.clear();
  storage.set("aiAuditSessionToken", "synthetic-legacy-token");
  globalThis.fetch = async () => ({
    ok: true, status: 200,
    headers: { get: () => "application/json" },
    json: async () => ({ authMode: "oidc" }),
  });
  await api("/api/runtime-config");
  assert.equal(storage.has("aiAuditSessionToken"), false);
});

test("API base trailing slashes do not create a double-slash upload route", async () => {
  storage.clear();
  storage.set("aiAuditApiBase", "https://control.example.test/");
  let requestedUrl = "";
  globalThis.fetch = async (url) => {
    requestedUrl = String(url);
    return {
      ok: true,
      status: 201,
      headers: { get: () => "application/json" },
      json: async () => ({ upload: { id: "upload_1" } }),
    };
  };

  await api("/api/uploads", { method: "POST", body: "{}" });

  assert.equal(requestedUrl, "https://control.example.test/api/uploads");
  assert.equal(storage.get("aiAuditApiBase"), "https://control.example.test");
});

test("queue saturation errors use localized workload-specific messages", async () => {
  state.lang = "en";
  const messages = {
    file: "The file-processing queue is full. Please wait a moment and try again.",
    upload: "The upload queue is full. Please wait a moment and try again.",
    external: "The external-service request queue is full. Please wait a moment and try again.",
  };
  for (const [workload, expected] of Object.entries(messages)) {
    globalThis.fetch = async () => ({
      ok: false,
      status: 503,
      headers: { get: () => "application/json" },
      json: async () => ({ error: "server_busy", workload, message: "server is busy; retry shortly" }),
    });
    await assert.rejects(api("/api/test"), (error) => {
      assert.equal(error.code, "server_busy");
      assert.equal(error.workload, workload);
      assert.equal(error.message, expected);
      return true;
    });
  }
});

test("an authenticated 401 clears credentials and emits one session-expired event", async () => {
  storage.set("aiAuditSessionToken", "expired-token");
  let notifications = 0;
  window.addEventListener("ai-audit:session-expired", () => { notifications += 1; });
  globalThis.fetch = async () => ({
    ok: false, status: 401,
    headers: { get: () => "application/json" },
    json: async () => ({ error: "unauthorized" }),
  });

  await assert.rejects(api("/api/workspaces"), (error) => error.status === 401);
  await assert.rejects(api("/api/workspaces"), (error) => error.status === 401);
  assert.equal(storage.has("aiAuditSessionToken"), false);
  assert.equal(notifications, 1);
});

test("upload 401 uses the same expiration path while startup session probes can suppress it", async () => {
  let notifications = 0;
  window.addEventListener("ai-audit:session-expired", () => { notifications += 1; });
  globalThis.fetch = async () => ({
    ok: true, status: 200,
    headers: { get: () => "application/json" },
    json: async () => ({ sessionToken: "fresh-token", user: { id: "user-1" } }),
  });
  await api("/api/login", { method: "POST", body: "{}" });

  globalThis.XMLHttpRequest = class {
    constructor() { this.upload = {}; }
    open() {}
    setRequestHeader() {}
    getResponseHeader() { return "application/json"; }
    send() {
      this.status = 401;
      this.responseText = '{"error":"unauthorized"}';
      queueMicrotask(() => this.onload());
    }
  };
  await assert.rejects(uploadChunkApi("/api/uploads/upload-1/files/0", new Blob(["x"])), (error) => error.status === 401);
  assert.equal(notifications, 1);

  globalThis.fetch = async () => ({
    ok: false, status: 401,
    headers: { get: () => "text/html" },
    json: async () => ({}),
  });
  await assert.rejects(api("/api/session", { suppressSessionExpiry: true }), (error) => error.status === 401);
  assert.equal(notifications, 1);
});
