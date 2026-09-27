import assert from "node:assert/strict";
import test from "node:test";

import { isEventStreamNearBottom, isEventStreamNearTop } from "./scrollPosition.js";

test("event stream follows updates only when the reader is near the bottom", () => {
  assert.equal(isEventStreamNearBottom({ scrollTop: 752, clientHeight: 200, scrollHeight: 1000 }), true);
  assert.equal(isEventStreamNearBottom({ scrollTop: 600, clientHeight: 200, scrollHeight: 1000 }), false);
});

test("event stream threshold is inclusive", () => {
  assert.equal(isEventStreamNearBottom({ scrollTop: 740, clientHeight: 200, scrollHeight: 1000 }, 60), true);
});

test("event stream requests older history at the top threshold", () => {
  assert.equal(isEventStreamNearTop({ scrollTop: 60 }), true);
  assert.equal(isEventStreamNearTop({ scrollTop: 61 }), false);
});
