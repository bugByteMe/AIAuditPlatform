import assert from "node:assert/strict";
import test from "node:test";
import { markdownToHtml, workspaceLinkPath } from "./chatMarkdown.js";

test("agent workspace links render as safe download actions", () => {
  const html = markdownToHtml("[点击打开或下载：ZA-货币资金_已编制.xlsx](/workspace/ZA-货币资金_已编制.xlsx)");
  assert.match(html, /<a href="#" data-workspace-file="ZA-货币资金_已编制\.xlsx">点击打开或下载：ZA-货币资金_已编制\.xlsx<\/a>/);
  assert.doesNotMatch(html, /href="\/workspace\//);
});

test("workspace links preserve encoded paths and escape labels and filenames", () => {
  const html = markdownToHtml('[<download>](/workspace/reports/My%20File%20%26%20%22Final%22.xlsx)');
  assert.match(html, /data-workspace-file="reports\/My File &amp; &quot;Final&quot;\.xlsx"/);
  assert.match(html, />&lt;download&gt;<\/a>/);
  assert.equal(workspaceLinkPath("/workspace/reports/My%20File.xlsx"), "reports/My File.xlsx");
});

test("unsafe or unrelated paths never become workspace downloads", () => {
  for (const path of ["/workspace/../secret", "/workspace/%2e%2e/secret", "/workspace/%2fetc/passwd", "/workspace/a%5cb", "/workspace/a//b", "/workspace/%ZZ", "/workspace/"]) {
    assert.equal(workspaceLinkPath(path), null);
    assert.doesNotMatch(markdownToHtml(`[bad](${path})`), /data-workspace-file=/);
  }
  assert.doesNotMatch(markdownToHtml("[bad](javascript:alert(1))"), /<a /);
  assert.doesNotMatch(markdownToHtml("[bad](/other/report.xlsx)"), /<a /);
  assert.doesNotMatch(markdownToHtml("```\n[code](/workspace/file.xlsx)\n```"), /<a /);
  assert.match(markdownToHtml("[site](https://example.com)"), /href="https:\/\/example\.com" target="_blank"/);
});
