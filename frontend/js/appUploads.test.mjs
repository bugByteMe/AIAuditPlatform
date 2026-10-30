import assert from "node:assert/strict";
import test from "node:test";
import { createUploadActions } from "./appUploads.js";

test("resumable fingerprints distinguish existing workspace destinations", () => {
  const { uploadFingerprint } = createUploadActions({});
  const files = [{ path: "Reports/a.txt", file: { size: 3, lastModified: 123 } }];
  assert.notEqual(
    uploadFingerprint(files, "append", "ws_1", "", "private", [], "Workpapers"),
    uploadFingerprint(files, "append", "ws_1", "", "private", [], "Other"),
  );
  assert.equal(JSON.parse(uploadFingerprint(files, "append", "ws_1")).destinationPath, "");
});
