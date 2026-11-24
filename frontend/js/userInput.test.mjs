import assert from "node:assert/strict";
import test from "node:test";
import { mergeInputRequest, renderInputRequest, inputDrafts } from "./userInput.js";

const request = (id, question = { type: "yes_no", title: "Proceed?", text: "" }) => ({
  id, sessionId: "s", workspaceId: "w", initiator: "owner", status: "pending", question, images: [],
});

test("only initiator gets enabled controls; false is a real answer", () => {
  const item = request("yes-no");
  mergeInputRequest(item);
  inputDrafts.set(item.id, false);
  const markup = renderInputRequest(item, { username: "owner" }, "en", true);
  assert.match(markup, /value="false" checked/);
  assert.match(markup, /data-input-skip/);
  assert.match(renderInputRequest(item, { username: "collaborator" }, "en", true), /disabled/);
  assert.doesNotMatch(renderInputRequest(item, { username: "collaborator" }, "en", true), /type="submit"/);
});

test("late pending snapshots never reopen a resolved request", () => {
  const item = request("resolved");
  mergeInputRequest(item);
  mergeInputRequest({ ...item, status: "answered", result: { value: false } });
  mergeInputRequest(item);
  const markup = renderInputRequest(item, { username: "owner" }, "en", true);
  assert.match(markup, /Answered/);
  assert.doesNotMatch(markup, /type="submit"/);
});

test("text drafts survive rendering and markup is escaped", () => {
  const item = request("text-draft", { type: "text", title: "<script>bad()</script>", text: "**Details**" });
  mergeInputRequest(item);
  inputDrafts.set(item.id, "draft </textarea><script>x</script>");
  const first = renderInputRequest(item, { username: "owner" }, "en", true);
  assert.equal(first, renderInputRequest(item, { username: "owner" }, "en", true));
  assert.doesNotMatch(first, /<script>/);
  assert.match(first, /&lt;\/textarea&gt;/);
});

test("single and multiple choice use radios and checkboxes", () => {
  for (const multiple of [true, false]) {
    const item = request(`choice-${multiple}`, { type: "multiple_choice", title: "Choose", multiple,
      options: [{ id: "a", label: "A" }, { id: "b", label: "B" }] });
    mergeInputRequest(item);
    inputDrafts.set(item.id, ["b"]);
    const markup = renderInputRequest(item, { username: "owner" }, "en", true);
    assert.match(markup, multiple ? /type="checkbox"/ : /type="radio"/);
    assert.match(markup, /value="b" checked/);
  }
});

test("stopped runs cannot submit even with a stale pending snapshot", () => {
  const item = request("stopped");
  mergeInputRequest(item);
  assert.doesNotMatch(renderInputRequest(item, { username: "owner" }, "en", false), /type="submit"/);
});
