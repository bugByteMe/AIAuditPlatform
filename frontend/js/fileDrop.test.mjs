import assert from "node:assert/strict";
import test from "node:test";
import {
  collectDroppedUpload, insertPathsAtSelection, joinWorkspacePath, normalizeDroppedPath,
} from "./fileDrop.js";

function file(name, relative = "") {
  return { name, webkitRelativePath: relative, size: 1, lastModified: 1 };
}

test("normalizes workspace-relative dropped paths", () => {
  assert.equal(normalizeDroppedPath("/reports/a.xlsx"), "reports/a.xlsx");
  assert.equal(joinWorkspacePath("target", "reports/a.xlsx"), "target/reports/a.xlsx");
  for (const path of ["", "../a", "a/../b", "a\\b", "a//b"]) {
    assert.throws(() => normalizeDroppedPath(path));
  }
});

test("collects fallback files and preserves folder roots", async () => {
  const first = file("a.txt", "reports/a.txt");
  const second = file("b.txt", "reports/nested/b.txt");
  const result = await collectDroppedUpload({ files: [first, second] });
  assert.deepEqual(result.items.map((item) => item.path), ["reports/a.txt", "reports/nested/b.txt"]);
  assert.deepEqual(result.topLevelPaths, ["reports"]);
});

test("collects modern directory handles recursively", async () => {
  const leaf = { name: "a.txt", kind: "file", async getFile() { return file("a.txt"); } };
  const nested = {
    name: "nested", kind: "directory",
    async *values() { yield leaf; },
  };
  const reports = {
    name: "reports", kind: "directory",
    async *values() { yield nested; },
  };
  const result = await collectDroppedUpload({
    items: [{ kind: "file", getAsFileSystemHandle: async () => reports, getAsFile: () => null }],
  });
  assert.deepEqual(result.items.map((item) => item.path), ["reports/nested/a.txt"]);
  assert.deepEqual(result.topLevelPaths, ["reports"]);
});

test("reads all legacy directory entry batches and ignores empty folders", async () => {
  const leaf = {
    name: "a.txt", isFile: true, isDirectory: false,
    file(resolve) { resolve(file("a.txt")); },
  };
  let reads = 0;
  const folder = {
    name: "reports", isFile: false, isDirectory: true,
    createReader() {
      return { readEntries(resolve) { reads += 1; resolve(reads === 1 ? [leaf] : []); } };
    },
  };
  const empty = {
    name: "empty", isFile: false, isDirectory: true,
    createReader() { return { readEntries(resolve) { resolve([]); } }; },
  };
  const result = await collectDroppedUpload({
    items: [folder, empty].map((entry) => ({ kind: "file", webkitGetAsEntry: () => entry, getAsFile: () => null })),
  });
  assert.deepEqual(result.items.map((item) => item.path), ["reports/a.txt"]);
  assert.deepEqual(result.topLevelPaths, ["reports"]);
  assert.equal(reads, 2);
});

test("inserts plain paths at the current selection with line boundaries", () => {
  globalThis.Event = class { constructor(type, options) { this.type = type; this.options = options; } };
  const textarea = {
    value: "Please inspect results", selectionStart: 15, selectionEnd: 22,
    setRangeText(value, start, end) { this.value = this.value.slice(0, start) + value + this.value.slice(end); },
    dispatchEvent() {}, focus() {},
  };
  assert.equal(insertPathsAtSelection(textarea, ["reports/a.xlsx", "notes"]), true);
  assert.equal(textarea.value, "Please inspect \nreports/a.xlsx\nnotes");
});
