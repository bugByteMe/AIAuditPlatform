import assert from "node:assert/strict";
import test from "node:test";

import { applyChatModelSettings } from "./chatModelSettings.js";

function select(values, value = values[0]) {
  return { value, options: values.map((item) => ({ value: item })) };
}

test("entering a chat applies its latest model and reasoning settings once", () => {
  const model = select(["gpt-5.6-sol", "gpt-6-sol"]);
  const reasoning = select(["high", "medium"]);
  const root = { querySelector: (query) => query.includes('model') ? model : reasoning };
  const session = { id: "chat-1", model: "gpt-6-sol", reasoning: "medium" };

  const applied = applyChatModelSettings(root, session);
  assert.equal(applied, "chat-1");
  assert.equal(model.value, "gpt-6-sol");
  assert.equal(reasoning.value, "medium");

  model.value = "gpt-5.6-sol";
  applyChatModelSettings(root, session, applied);
  assert.equal(model.value, "gpt-5.6-sol", "rerenders must not overwrite a manual selection");
});
