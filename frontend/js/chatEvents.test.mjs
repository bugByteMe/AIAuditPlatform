import assert from "node:assert/strict";
import test from "node:test";

import { findSessionById, initializeEventCursors, mergeSessionEvents, sessionEventCursor } from "./chatEvents.js";

test("initial cursor uses the highest persisted event id, not visible event count", () => {
  const session = { id: "chat-1", events: [["command", "done", "done", "run-1", 8, "completed", "tool-1"]] };
  assert.equal(sessionEventCursor(session), 8);
  const cursors = {};
  initializeEventCursors([{ id: "workspace-1", sessions: [session] }], cursors);
  assert.equal(cursors["chat-1"], 8);
});

test("events merge into their addressed session instead of the selected session", () => {
  const selected = { id: "selected", status: "completed", events: [] };
  const target = { id: "target", status: "running", events: [] };
  const workspaces = [{ id: "workspace-1", sessions: [selected, target] }];
  const resolved = findSessionById(workspaces, "workspace-1", "target");
  mergeSessionEvents(resolved, [{ id: 1, type: "assistant", message: "live", runId: "run-1" }], {});
  assert.equal(selected.events.length, 0);
  assert.equal(target.events[0][1], "live");
});

test("out-of-order batches advance monotonically and coalesce tool updates", () => {
  const session = { id: "chat-1", status: "running", events: [] };
  const cursors = {};
  mergeSessionEvents(
    session,
    [
      { id: 3, type: "command", message: "done", runId: "run-1", toolCallId: "tool-1", status: "completed" },
      { id: 1, type: "command", message: "start", runId: "run-1", toolCallId: "tool-1", status: "started" },
      { id: 2, type: "assistant", message: "working", runId: "run-1" },
    ],
    cursors,
  );
  assert.equal(cursors["chat-1"], 3);
  assert.equal(session.events.length, 2);
  assert.equal(session.events.find((event) => event[0] === "command")[1], "done");
  assert.equal(mergeSessionEvents(session, [{ id: 2, type: "assistant", message: "duplicate" }], cursors), false);
});
