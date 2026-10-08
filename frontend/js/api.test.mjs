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
};

const { api } = await import("./api.js");
const { state } = await import("./state.js");

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
