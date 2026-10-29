import assert from "node:assert/strict";
import test from "node:test";
import { formulaToHtml, markdownToHtml, workspaceLinkPath } from "./chatMarkdown.js";

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

test("GitHub-style tables preserve alignment and inline formatting", () => {
  const html = markdownToHtml("| Account | Amount | Note |\n| :--- | ---: | :---: |\n| Revenue | **120** | $x^2$ |");
  assert.match(html, /<div class="event-table-wrap"><table>/);
  assert.match(html, /<th class="align-left">Account<\/th>/);
  assert.match(html, /<td class="align-right"><strong>120<\/strong><\/td>/);
  assert.match(html, /<td class="align-center"><math/);
});

test("inline and display formulas render as safe MathML", () => {
  const inline = markdownToHtml("Ratio: $R=\\frac{a}{b}$ and \\(x_1\\)");
  assert.match(inline, /<math class="chat-math"/);
  assert.match(inline, /<mfrac>/);
  assert.match(inline, /<msub>/);
  const display = markdownToHtml("$$\\sqrt{x^2}$$");
  assert.match(display, /chat-math-display/);
  assert.match(display, /<msqrt>/);
  assert.doesNotMatch(formulaToHtml("<script>alert(1)</script>"), /<script>/);
});

test("code blocks and inline code do not execute table or formula markup", () => {
  const html = markdownToHtml("`$not_math$`\n```\n| A | B |\n| --- | --- |\n$$x$$\n```");
  assert.match(html, /<code>\$not_math\$<\/code>/);
  assert.match(html, /<pre><code>\n\| A \| B \|/);
  assert.equal((html.match(/<math/g) || []).length, 0);
});

test("currency amounts are not mistaken for formula delimiters", () => {
  const html = markdownToHtml("Revenue was $100 and costs were $80.");
  assert.doesNotMatch(html, /<math/);
  assert.match(html, /\$100 and costs were \$80/);
});
