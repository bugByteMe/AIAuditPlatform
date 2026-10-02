// Run with node --test frontend/tests/ui.browser.mjs after installing Playwright.
// PLAYWRIGHT_MODULE may point to an existing Playwright index.mjs.
import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import { resolve, extname } from "node:path";
import { tmpdir } from "node:os";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : "playwright");
const root = fileURLToPath(new URL("../", import.meta.url));
const server = createServer(async (req, res) => {
  try {
    const file = resolve(root, `.${req.url === "/" ? "/index.html" : req.url.split("?")[0]}`);
    if (!file.startsWith(root)) throw new Error("outside frontend");
    res.setHeader("Content-Type", ({ ".html": "text/html", ".js": "text/javascript", ".css": "text/css" })[extname(file)] || "application/octet-stream");
    res.end(await readFile(file));
  } catch {
    res.writeHead(404).end();
  }
});
await new Promise((done) => server.listen(0, "127.0.0.1", done));
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || "msedge", headless: true });
test.after(async () => { await browser.close(); await new Promise((done) => server.close(done)); });

async function openApp(role = "platform_admin", viewport = { width: 1280, height: 900 }) {
  const page = await browser.newPage({ viewport });
  const requests = [];
  const user = { id: "actor", username: "example.actor", role, groupId: "team", group: "示例小组", budget: {} };
  const member = { id: "member", username: "example.member", role: "user", groupId: "team", status: "active", enabled: true };
  const group = { id: "team", name: "示例小组", userCount: 2, liveRunLimit: 1 };
  const session = { id: "session", title: "Example chat", status: "completed", events: [], tokens: 0, historyLoaded: true };
  const workspace = { id: "workspace", name: "Example workspace", owner: user.username, fileCount: 0, sizeBytes: 0, files: [], artifacts: [], sessions: [session], detailLoaded: true };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    requests.push({ path, method: request.method(), body: request.postDataJSON() });
    let body = {};
    if (path === "/api/session") body = { user };
    else if (path === "/api/workspaces") body = { workspaces: [workspace] };
    else if (path === "/api/workspaces/workspace") body = { workspace };
    else if (path === "/api/accounts") body = { accounts: [user, member], groups: [group] };
    else if (path.endsWith("/events")) body = { events: [] };
    else if (path.endsWith("/chat/runs")) {
      await route.fulfill({ status: 403, json: { error: "forbidden", message: "Example permission error" } });
      return;
    }
    await route.fulfill({ json: body });
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.waitForFunction(() => window.aiAuditAppReady && !document.querySelector("#app-shell").classList.contains("hidden"));
  await page.waitForFunction(() => document.querySelector("#workspace-overview-title").textContent === "示例小组的工作区");
  return { page, requests };
}

test("administrator settings expose and submit only each role's permitted fields", async () => {
  for (const role of ["platform_admin", "system_admin", "user"]) {
    const { page, requests } = await openApp(role);
    try {
      assert.equal(await page.locator("#admin-nav-button").isVisible(), role !== "user");
      if (role === "user") continue;
      await page.locator("#admin-nav-button").click();
      await page.locator('[data-open-account-settings="member"]').click();
      assert.equal(await page.locator('[name="role"]').isVisible(), role === "system_admin");
      assert.equal(await page.locator('#account-settings-form [name="maxSessions"]').isVisible(), role === "system_admin");
      assert.equal(await page.locator("#account-settings-delete").isVisible(), role === "system_admin");
      if (role === "system_admin") await page.locator('[name="role"]').selectOption("platform_admin");
      await page.locator('#account-settings-form button[type="submit"]').click();
      await page.waitForFunction(() => document.querySelector("#account-settings-modal").classList.contains("hidden"));
      const accountRequest = requests.find((req) => req.method === "PATCH" && req.path === "/api/accounts/member");
      assert.deepEqual(Object.keys(accountRequest.body).sort(), role === "system_admin" ? ["groupId", "maxSessions", "role"] : ["groupId"]);
      if (role === "system_admin") assert.equal(accountRequest.body.role, "platform_admin");
      await page.locator('[data-open-group-settings="team"]').click();
      assert.equal(await page.locator('[name="diskLimitMib"]').isVisible(), role === "system_admin");
      await page.locator('#group-settings-form button[type="submit"]').click();
      await page.waitForFunction(() => document.querySelector("#group-settings-modal").classList.contains("hidden"));
      const groupRequest = requests.find((req) => req.method === "PATCH" && req.path === "/api/groups/team");
      assert.deepEqual(Object.keys(groupRequest.body).sort(), role === "system_admin" ? ["diskLimitBytes", "liveRunLimit", "name"] : ["liveRunLimit", "name"]);
      if (role === "platform_admin") {
        assert.equal(requests.some((req) => ["/api/audit-logs", "/api/workers"].includes(req.path)), false);
        assert.equal(await page.locator("#recharge-import-button").isVisible(), false);
        assert.equal(await page.locator("#batch-create-button").isVisible(), false);
      }
    } finally { await page.close(); }
  }
});

test("native composer drag respects height bounds, multiline submission and disabled input on desktop and narrow screens", async () => {
  for (const width of [1280, 390]) {
    const { page, requests } = await openApp("user", { width, height: 900 });
    try {
      await page.locator('[data-view="chat"]').click();
      const input = page.locator("#composer textarea");
      await input.fill("First line");
      await input.press("Enter");
      await input.pressSequentially("Second line");
      assert.equal(await input.inputValue(), "First line\nSecond line");
      assert.equal(requests.some((req) => req.path.endsWith("/chat/runs")), false);
      const initial = await input.boundingBox();
      const drag = async (delta) => {
        await input.scrollIntoViewIfNeeded();
        const box = await input.boundingBox();
        await page.mouse.move(box.x + box.width - 3, box.y + box.height - 3);
        await page.mouse.down();
        await page.mouse.move(box.x + box.width - 3, box.y + box.height - 3 + delta, { steps: 10 });
        await page.mouse.up();
      };
      await drag(120);
      const expanded = await input.boundingBox();
      assert.ok(expanded.height > initial.height, "mouse dragging must increase input height");
      await drag(1000);
      const max = await input.boundingBox();
      assert.ok(Math.abs(max.height - 315) < 1, `maximum height at width ${width}: ${max.height}`);
      await page.locator('#composer button[type="submit"]').scrollIntoViewIfNeeded();
      assert.ok(await page.locator('#composer button[type="submit"]').evaluate((node) => {
        const box = node.getBoundingClientRect();
        return box.bottom <= window.innerHeight && document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2) === node;
      }), "resizing must keep the send button accessible");
      await drag(-1000);
      assert.equal(Math.round((await input.boundingBox()).height), 82, `minimum height at width ${width}`);
      assert.equal(await input.inputValue(), "First line\nSecond line");
      await page.locator('#composer button[type="submit"]').click();
      await page.waitForFunction(() => document.querySelector("#toast").textContent === "Example permission error");
      assert.equal(requests.find((req) => req.path.endsWith("/chat/runs")).body.prompt, "First line\nSecond line");
      assert.equal(await input.inputValue(), "First line\nSecond line");
      await input.evaluate((node) => { node.disabled = true; });
      assert.equal(await input.isDisabled(), true);
      await page.locator("#run-state-label").click();
      await page.keyboard.press("Enter");
      assert.equal(await input.inputValue(), "First line\nSecond line");
      assert.equal(await page.locator('#composer button[type="submit"]').isVisible(), true);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    } finally { await page.close(); }
  }
});

test("workspace page title uses a safe fallback while loading and when no group name is available", async () => {
  const { page } = await openApp("user");
  try {
    for (const group of ["异步小组", "", "   "]) {
      let release;
      const ready = new Promise((done) => { release = done; });
      await page.route("**/api/session", async (route) => {
        await ready;
        await route.fulfill({ json: { user: { id: "actor", username: "example.actor", role: "user", groupId: "team", group, budget: {} } } });
      });
      await page.reload({ waitUntil: "domcontentloaded" });
      assert.equal(await page.locator("#workspace-overview-title").textContent(), "工作区");
      release();
      await page.waitForFunction(() => window.aiAuditAppReady);
      assert.equal(await page.locator("#workspace-overview-title").textContent(), group.trim() ? `${group}的工作区` : "工作区");
      await page.unroute("**/api/session");
    }
  } finally { await page.close(); }
});

test("recharge cards align and paid orders display a large persistent confirmation", async () => {
  for (const width of [1280, 390]) {
    const { page } = await openApp("user", { width, height: 900 });
    let status = "pending";
    const order = () => ({ id: "example-payment", amountCny: "50.00", status, qrCodeUrl: "/api/recharge/orders/example-payment/qr" });
    await page.route("**/api/recharge**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/qr")) {
        await route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><rect width="200" height="200" fill="white"/></svg>' });
      } else if (path === "/api/recharge") {
        await route.fulfill({ json: { paymentReady: true, products: [1, 50, 100, 200].map(amountCny => ({ amountCny: String(amountCny) })), history: [] } });
      } else await route.fulfill({ json: { order: order() } });
    });
    try {
      await page.locator('[data-view="recharge"]').click();
      await page.waitForFunction(() => !document.querySelector('[data-recharge-amount="50"]').disabled);
      const cards = await page.locator(".recharge-product").all();
      assert.equal(cards.length, 3);
      const boxes = await Promise.all(cards.map(card => card.boundingBox()));
      assert.ok(boxes.every(box => Math.abs(box.height - boxes[0].height) < 1));
      if (width === 1280) assert.ok(boxes.every(box => box.y === boxes[0].y));
      assert.equal(await page.locator('.recharge-products [data-recharge-amount="1"]').count(), 0);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      if (width === 1280) await page.screenshot({ path: resolve(tmpdir(), "audit-recharge-cards.png"), fullPage: true });
      await cards[0].click();
      await page.waitForFunction(() => !document.querySelector("#recharge-qr-image").classList.contains("hidden"));
      status = "paid";
      await page.waitForFunction(() => document.querySelector("#recharge-payment-notice-title").textContent === "支付成功");
      assert.equal(await page.locator("#recharge-qr-image").isVisible(), false);
      assert.equal(await page.locator("#recharge-payment-notice-amount").textContent(), "¥50.00");
      assert.ok((await page.locator("#recharge-payment-notice").boundingBox()).height > 240);
      status = "applied";
      await page.waitForFunction(() => document.querySelector("#recharge-payment-notice-title").textContent === "充值成功");
      assert.equal(await page.locator("#recharge-payment-notice-detail").textContent(), "充值成功，余额已更新。");
      await page.screenshot({ path: resolve(tmpdir(), `audit-recharge-success-${width}.png`) });
      await page.locator('#recharge-qr-modal [data-close-recharge-qr]').first().click();
      status = "pending";
      await cards[0].click();
      assert.equal(await page.locator("#recharge-payment-notice").isVisible(), false);
      await page.waitForFunction(() => !document.querySelector("#recharge-qr-image").classList.contains("hidden"));
    } finally { await page.close(); }
  }
});
