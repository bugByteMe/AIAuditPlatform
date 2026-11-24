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
const streams = new Set();
const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  if (url.pathname.endsWith("/chat/stream")) {
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store" });
    res.write(": connected\n\n");
    streams.add(res);
    res.on("close", () => streams.delete(res));
    return;
  }
  if (url.pathname.endsWith("/chat/events")) {
    res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ events: [], sessionStatus: "running" }));
    return;
  }
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
      res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ requests: request ? [request] : [] }));
    }
    return;
  }
  if (url.pathname === "/") {
    res.writeHead(200, { "Content-Type": "text/html" }).end('<link rel="stylesheet" href="/styles.css"><main id="event-stream" class="event-stream"></main>');
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

test("new requests and resolutions arrive through named SSE events without refreshing", async () => {
  request = null;
  const page = await browser.newPage();
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/input-requests")),
      initialize(page),
    ]);
    await page.evaluate(async () => {
      const { state } = await import("/js/state.js");
      const events = await import("/js/chatEvents.js");
      const { api, authenticatedApiUrl } = await import("/js/api.js");
      const { renderEvents } = await import("/js/render.js");
      const { createChatStream } = await import("/js/appChatStream.js");
      window.inputStream = createChatStream({
        state, ...events, api, authenticatedApiUrl, renderDynamic: () => renderEvents(),
        safeCurrentWorkspace: () => state.workspaces[0], refreshWorkspaceById: async () => {},
        refreshCurrentUser: async () => {},
      });
      window.inputStream.startChatStreamForSession("w", "s");
    });
    const deadline = Date.now() + 5000;
    while (!streams.size && Date.now() < deadline) await new Promise((done) => setTimeout(done, 20));
    assert.equal(streams.size, 1);
    request = { id: "b".repeat(32), runId: "run_1", sessionId: "s", workspaceId: "w", initiator: "owner",
      status: "pending", question: { type: "yes_no", title: "Live question" }, images: [] };
    const send = (id, type) => {
      const event = { id, type, runId: "run_1", message: request.question.title, inputRequest: request };
      for (const stream of streams) stream.write(`id: ${id}\nevent: ${type}\ndata: ${JSON.stringify(event)}\n\n`);
    };
    send(1, "user_input_requested");
    await page.getByRole("heading", { name: "Live question" }).waitFor({ timeout: 2000 });
    assert.equal(await page.getByRole("button", { name: "Submit", exact: true }).isEnabled(), true);
    request = { ...request, status: "answered", result: { requestId: request.id, status: "answered", value: false } };
    send(2, "user_input_resolved");
    await page.getByRole("status").filter({ hasText: "Answered" }).waitFor({ timeout: 2000 });
    assert.equal(await page.getByRole("button", { name: "Submit", exact: true }).count(), 0);
    assert.equal(await page.evaluate(async () => (await import("/js/state.js")).state.chatLastEventIds.s), 2);
  } finally {
    await page.evaluate(() => window.inputStream?.stopChatStream()).catch(() => {});
    await page.close();
  }
});

test("restored and streamed cards use the available width with production CSS", async () => {
  for (const width of [1280, 390]) {
    const page = await open({ type: "multiple_choice", title: "交互问题", text: "请扫描二维码并选择已经完成的操作。".repeat(8),
      multiple: false, options: [{ id: "done", label: "已确认登录" }, { id: "refresh", label: "刷新二维码" }] });
    try {
      await page.setViewportSize({ width, height: 900 });
      await page.evaluate(async () => { (await import("/js/state.js")).state.lang = "zh"; (await import("/js/render.js")).renderEvents(); });
      const checkLayout = async () => {
        const bounds = await page.locator(".user-input-card").evaluate((card) => {
          const rect = card.getBoundingClientRect();
          const image = card.querySelector("img").getBoundingClientRect();
          const button = card.querySelector("button");
          return { width: rect.width, right: rect.right, imageRight: image.right, viewport: innerWidth,
            overflow: card.scrollWidth > card.clientWidth, buttonHeight: button.getBoundingClientRect().height,
            lineHeight: parseFloat(getComputedStyle(button).lineHeight) };
        });
        assert.ok(bounds.width >= (width < 600 ? 300 : 500), JSON.stringify(bounds));
        assert.ok(bounds.right <= bounds.viewport && bounds.imageRight <= bounds.right, JSON.stringify(bounds));
        assert.equal(bounds.overflow, false);
        assert.ok(bounds.buttonHeight < bounds.lineHeight * 2 + 24, JSON.stringify(bounds));
      };
      await checkLayout();
      // The same request arrives as a history/live event rather than list hydration.
      await page.evaluate(async (inputRequest) => {
        const { state } = await import("/js/state.js");
        state.workspaces[0].sessions[0].events.push(["user_input_requested", "Question", "Question", "run_1", 1, "", "", { inputRequest }]);
        (await import("/js/render.js")).renderEvents();
      }, request);
      await checkLayout();
    } finally { await page.close(); }
  }
});
