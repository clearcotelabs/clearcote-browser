/**
 * LIVE: a humanized selectOption whose change handler stalls the page keeps to its timeout, as stock
 * selectOption does. Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary. Mirrors
 * sdk/python/tests/test_humanize_select_commit_live.py. Strict humanize: a fallback to stock Playwright would pass
 * the timing checks too, so it fails the test.
 *
 * - A sync XHR that is never answered blocks the renderer, so the Enter that commits the pick is never
 *   acknowledged: it used to wait forever, stock throws at the timeout.
 * - A change handler that navigates to a server that never answers leaves a document that cannot
 *   answer the read-back: it used to throw at the page default, stock returns the value at once.
 */
import { existsSync, rmSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { errors, type Browser } from "playwright-core";
import { launch } from "../src/index.js";

const LIVE = process.env.CLEARCOTE_LIVE_ENGINE;
const T = 90_000;
const OPTS = '<option value="a">Alpha</option><option value="b">Bravo</option></select>';
const PAGES: Record<string, string> = {
  "/xhr": `<select id="s" onchange="var x=new XMLHttpRequest();x.open('GET','/hang',false);try{x.send()}catch(e){};window.__changed=this.value">${OPTS}`,
  "/nav": `<select id="s" onchange="window.__changed=this.value;location.href='/hang'">${OPTS}`,
  "/fxhr": '<iframe src="/xhr" width="300" height="80"></iframe>',
  "/fnav": '<iframe src="/nav" width="300" height="80"></iframe>',
  "/ok": `<select id="s" onchange="window.__changed=this.value">${OPTS}`,
};

describe.runIf(LIVE)("live humanized selectOption with a stalling change handler", () => {
  let server: Server;
  let base = "";
  let browser: Browser;
  const FC_CACHE = join(tmpdir(), "cc-fc-cache");
  let fcCacheBefore = true;

  beforeAll(async () => {
    fcCacheBefore = existsSync(FC_CACHE);
    server = createServer((req, res) => {
      const path = (req.url ?? "/").split("?")[0];
      if (path === "/hang") return; // never answers while the test runs
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(`<!doctype html><html><body>${PAGES[path] ?? ""}</body></html>`);
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
    base = `http://127.0.0.1:${(server.address() as any).port}`;
  });
  afterAll(async () => {
    server.closeAllConnections();
    await new Promise<void>((r) => server.close(() => r()));
    if (!fcCacheBefore) rmSync(FC_CACHE, { recursive: true, force: true });
  });
  // one browser per test: a page left blocked by a sync XHR must not hold up the next test
  beforeEach(async () => {
    browser = await launch({
      executablePath: LIVE, headless: true, humanize: { strict: true }, fingerprint: "select-commit-1", quiet: true,
      args: ["--no-sandbox"],
    });
  }, 180_000);
  afterEach(async () => { await browser?.close(); }, 60_000);

  it("a sync-XHR change handler throws at the timeout", async () => {
    const p = await browser.newPage();
    await p.goto(base + "/xhr");
    const t0 = Date.now();
    const err = await p.selectOption("#s", "b", { timeout: 5000 }).then(() => null, (e) => e);
    expect(err).toBeInstanceOf(errors.TimeoutError);
    expect(String(err.message)).toContain("5000ms");
    expect(Date.now() - t0).toBeLessThan(9000);
  }, T);

  it("a navigating change handler returns the value", async () => {
    const p = await browser.newPage();
    p.setDefaultTimeout(15_000);
    await p.goto(base + "/nav");
    const t0 = Date.now();
    expect(await p.selectOption("#s", "b", { timeout: 5000 })).toEqual(["b"]);
    expect(Date.now() - t0).toBeLessThan(5000);
  }, T);

  it.each(["/fxhr", "/fnav"])("inside a frame (%s): the frame route keeps the same timeout", async (path) => {
    const p = await browser.newPage();
    p.setDefaultTimeout(15_000);
    await p.goto(base + path);
    const sel = p.frameLocator("iframe").locator("#s");
    await sel.waitFor();
    const t0 = Date.now();
    if (path === "/fxhr") {
      const err = await sel.selectOption("b", { timeout: 5000 }).then(() => null, (e) => e);
      expect(err).toBeInstanceOf(errors.TimeoutError);
      expect(Date.now() - t0).toBeLessThan(9000);
    } else {
      expect(await sel.selectOption("b", { timeout: 5000 })).toEqual(["b"]);
      expect(Date.now() - t0).toBeLessThan(5000);
    }
  }, T);

  it("a healthy page with a short timeout still selects", async () => {
    const p = await browser.newPage();
    await p.goto(base + "/ok");
    expect(await p.selectOption("#s", "b", { timeout: 300 })).toEqual(["b"]);
    expect(await p.evaluate("window.__changed")).toBe("b");
  }, T);
});
