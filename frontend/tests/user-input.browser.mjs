// Remote/browser integration: PLAYWRIGHT_MODULE and CHROMIUM_EXECUTABLE may select installed tools.
import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { resolve, extname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : "playwright");
const root = fileURLToPath(new URL("../", import.meta.url));
let request;
let submissions;
let rejectNext = false;
const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  if (url.pathname.includes("/input-requests")) {
    if (req.method === "POST") {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const answer = JSON.parse(Buffer.concat(chunks));
      submissions.push(answer);
      if (rejectNext) {
        rejectNext = false;
        res.writeHead(503, { "Content-Type": "application/json" }).end(JSON.stringify({ error: "retry", message: "Retry this answer" }));
        return;
      }
      request = { ...request, status: answer.status, result: { requestId: request.id, ...answer } };
      res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ request }));
    } else if (url.searchParams.has("image")) {
      res.writeHead(200, { "Content-Type": "image/png" }).end(Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aU1sAAAAASUVORK5CYII=", "base64"));
    } else {
      res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ requests: [request] }));
    }
    return;
  }
  if (url.pathname === "/") {
    res.writeHead(200, { "Content-Type": "text/html" }).end('<main id="event-stream"></main>');
    return;
  }
  try {
    const file = resolve(root, `.${url.pathname}`);
    if (!file.startsWith(root)) throw new Error("outside root");
    const data = await readFile(file);
    res.writeHead(200, { "Content-Type": ({ ".js": "text/javascript", ".css": "text/css" })[extname(file)] || "application/octet-stream" }).end(data);
  } catch { res.writeHead(404).end(); }
});
await new Promise((done) => server.listen(0, "127.0.0.1", done));
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || "/usr/bin/chromium", args: ["--no-sandbox"], headless: true });
test.after(async () => { await browser.close(); await new Promise((done) => server.close(done)); });

async function open(question, username = "owner") {
  request = { id: "a".repeat(32), runId: "run_1", sessionId: "s", workspaceId: "w", initiator: "owner", status: "pending", question,
    images: [{ index: 0, name: "question.png", contentType: "image/png" }] };
  submissions = [];
  const page = await browser.newPage();
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await initialize(page, username);
  await page.locator(".user-input-card").waitFor();
  return page;
}

async function initialize(page, username = "owner") {
  await page.evaluate(async (username) => {
    const { state } = await import("/js/state.js");
    state.workspacesLoading = false;
    state.lang = "en";
    state.user = { username };
    state.workspaces = [{ id: "w", sessions: [{ id: "s", status: "running", latestRunId: "run_1", events: [], historyLoaded: true }] }];
    const { renderEvents } = await import("/js/render.js");
    renderEvents();
  }, username);
}

test("pending cards restore after reload, retain focused drafts during streaming, and retry failed submissions", async () => {
  const page = await open({ type: "text", title: "Which year?", text: "**Explain** the period." });
  try {
    await page.locator("img").evaluate((image) => image.decode());
    await page.reload();
    await initialize(page);
    await page.locator("textarea").fill("2026 period");
    await page.evaluate(async () => {
      const { state } = await import("/js/state.js");
      state.workspaces[0].sessions[0].events.push(["assistant", "Still working", "Still working", "run_1", 1]);
      (await import("/js/render.js")).renderEvents();
    });
    assert.equal(await page.locator("textarea").inputValue(), "2026 period");
    assert.equal(await page.locator("textarea").evaluate((node) => node === document.activeElement), true);
    rejectNext = true;
    await page.getByRole("button", { name: "Submit", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "Retry this answer" }).waitFor();
    assert.equal(await page.locator("textarea").inputValue(), "2026 period");
    await page.getByRole("button", { name: "Submit", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "Answered" }).waitFor();
    assert.deepEqual(submissions.at(-1), { status: "answered", value: "2026 period" });
    assert.equal(await page.getByRole("button", { name: "Submit", exact: true }).count(), 0);
  } finally { await page.close(); }
});

test("yes/no submits false and collaborators cannot answer", async () => {
  let page = await open({ type: "yes_no", title: "Proceed?" });
  try {
    await page.getByLabel("No", { exact: true }).check();
    await page.getByRole("button", { name: "Submit", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "Answered" }).waitFor();
    assert.equal(submissions[0].value, false);
  } finally { await page.close(); }
  page = await open({ type: "yes_no", title: "Proceed?" }, "collaborator");
  try {
    assert.equal(await page.getByRole("button", { name: "Submit", exact: true }).count(), 0);
    assert.equal(await page.getByLabel("Yes", { exact: true }).isDisabled(), true);
  } finally { await page.close(); }
});

test("multi-select and skip submit structured results", async () => {
  let page = await open({ type: "multiple_choice", title: "Choose", multiple: true, options: [{ id: "a", label: "A" }, { id: "b", label: "B" }] });
  try {
    await page.getByLabel("A", { exact: true }).check();
    await page.getByLabel("B", { exact: true }).check();
    await page.getByRole("button", { name: "Submit", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "Answered" }).waitFor();
    assert.deepEqual(submissions[0].value, ["a", "b"]);
  } finally { await page.close(); }
  page = await open({ type: "text", title: "Optional question" });
  try {
    await page.getByRole("button", { name: "Skip", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "Skipped" }).waitFor();
    assert.deepEqual(submissions[0], { status: "skipped" });
  } finally { await page.close(); }
});
