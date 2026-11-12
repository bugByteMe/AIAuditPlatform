import assert from "node:assert/strict";
import test from "node:test";

import {
  defaultCapabilityIds, filterCapabilities, localizedCapabilityText, reconcileCapabilityIds,
} from "./capabilities.js";

const capabilities = [
  {
    id: "financial-audit",
    name: { zh: "财务审计", en: "Financial audit" },
    description: { zh: "财务报表工具", en: "Financial statement tools" },
  },
  { id: "documents", name: "Documents", description: "Search files" },
];

test("capability labels use the selected locale and fall back", () => {
  assert.equal(localizedCapabilityText(capabilities[0].name, "zh"), "财务审计");
  assert.equal(localizedCapabilityText({ en: "English" }, "zh"), "English");
  assert.equal(localizedCapabilityText("Shared", "zh"), "Shared");
});

test("capability search covers id, localized name, and description", () => {
  assert.deepEqual(filterCapabilities(capabilities, "财务", "zh"), [capabilities[0]]);
  assert.deepEqual(filterCapabilities(capabilities, "statement", "en"), [capabilities[0]]);
  assert.deepEqual(filterCapabilities(capabilities, "documents", "en"), [capabilities[1]]);
});

test("removed capability selections are reconciled", () => {
  assert.deepEqual(reconcileCapabilityIds(new Set(["documents", "removed"]), capabilities), ["documents"]);
});

test("all configured defaults initialize a new chat", () => {
  assert.deepEqual(defaultCapabilityIds([
    { id: "audit", default: true },
    { id: "documents", default: false },
    { id: "search", default: true },
  ]), ["audit", "search"]);
});
