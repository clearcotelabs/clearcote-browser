// launch({ cloud: true }) end to end against a REAL browser: the fake hosted API answers the create
// with the CDP WebSocket of a local Chromium, so the SDK's connect, humanize install, close and DELETE
// run for real. Profile sync reads cookies off the same browser over CDP.
// Needs a Chromium: CLEARCOTE_TEST_BINARY, or one Playwright has downloaded. Skips without one.
// Mirrors sdk/python/tests/test_cloud_launch.py.
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium, type Page } from "playwright-core";
import { Cloud, cloudSessionOf, launch, launchPersistentContext } from "../src/index.js";
import { API_KEY, startFakeCloud, type FakeCloud } from "./helpers/fake-cloud.js";
import { LocalChromium, findChromium } from "./helpers/chromium.js";

const BUTTON = "<title>t</title><button id=b onclick=\"document.title='clicked'\">go</button>";
const ENV = ["CLEARCOTE_API_KEY", "CLEARCOTE_API_URL", "CLEARCOTE_CLOUD"];
const humanized = (p: Page) => (p as unknown as { _clearcotePersona?: unknown })._clearcotePersona !== undefined;

describe.skipIf(!findChromium())("cloud launch against a real browser", () => {
  const saved: Record<string, string | undefined> = {};
  let api: FakeCloud;
  let browserProc: LocalChromium;

  beforeEach(async () => {
    for (const k of ENV) saved[k] = process.env[k];
    browserProc = await new LocalChromium().start();
    api = await startFakeCloud();
    api.connectUrl = browserProc.wsUrl;
    process.env.CLEARCOTE_API_KEY = API_KEY;
    process.env.CLEARCOTE_API_URL = api.url;
    delete process.env.CLEARCOTE_CLOUD;
  });

  afterEach(async () => {
    await api.close();
    await browserProc.stop();
    for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
  });

  it("is a working, humanized Playwright Browser; close() disconnects and ends the session", async () => {
    const browser = await launch({ cloud: true, humanize: true, country: "us", identity: "acct-1" });
    try {
      expect(browser.isConnected()).toBe(true);
      // the same Playwright Browser type a plain connectOverCDP (and a local launch) gives
      const direct = await chromium.connectOverCDP(browserProc.wsUrl);
      expect(browser.constructor).toBe(direct.constructor);
      await direct.close();
      expect(api.requests("POST", "/api/v1/browsers")[0].body).toEqual({ country: "us", identity: "acct-1" });
      expect(cloudSessionOf(browser).id).toBe("bs_1");
      const ctx = browser.contexts()[0];
      const first = ctx.pages()[0] ?? (await ctx.newPage());
      expect(humanized(first)).toBe(true);
      expect(humanized(await ctx.newPage())).toBe(true);
      const page = await browser.newPage();
      expect(humanized(page)).toBe(true);
      await page.setContent(BUTTON);
      await page.click("#b");
      expect(await page.title()).toBe("clicked");
    } finally {
      await browser.close();
    }
    expect(browser.isConnected()).toBe(false);
    expect(api.requests("DELETE", "/api/v1/browsers/bs_1")).toHaveLength(1);
    expect(browserProc.alive()).toBe(true); // close() only disconnects: ending the browser is the gateway's job
  }, 60_000);

  it("follows CLEARCOTE_CLOUD", async () => {
    process.env.CLEARCOTE_CLOUD = "true";
    const browser = await launch({ note: "from env" } as never);
    expect(browser.isConnected() && browser.version()).toBeTruthy();
    await browser.close();
    expect(api.requests("POST", "/api/v1/browsers")[0].body).toEqual({ note: "from env" });
  }, 60_000);

  it("a persistent cloud context", async () => {
    const ctx = await launchPersistentContext({ cloud: true, profile: "acct-1", humanize: true });
    expect(api.requests("POST", "/api/v1/browsers")[0].body.profile).toEqual({ name: "acct-1", persist: true });
    const page = await ctx.newPage();
    expect(humanized(page)).toBe(true);
    await page.setContent(BUTTON);
    await page.click("#b");
    expect(await page.title()).toBe("clicked");
    await ctx.close();
    expect(api.requests("DELETE", "/api/v1/browsers/bs_1")).toHaveLength(1);
  }, 60_000);

  // ── profile sync off a real browser ────────────────────────────────────────────────────────────

  const COOKIES = [
    { name: "sid", value: "a1", domain: ".example.com", path: "/", secure: true, httpOnly: true, sameSite: "Lax", expires: 1893456000 },
    { name: "lang", value: "nl", domain: "shop.example.com", path: "/", secure: false, httpOnly: false },
    { name: "trk", value: "zz", domain: ".tracker.net", path: "/", secure: false, httpOnly: false },
  ];

  async function setCookies(): Promise<void> {
    const b = await chromium.connectOverCDP(browserProc.wsUrl);
    await (await b.newBrowserCDPSession()).send("Storage.setCookies", { cookies: COOKIES as never });
    await b.close();
  }

  const served = (calls: unknown[]) => ({
    cdpUrl: browserProc.httpUrl,
    isAlive: () => browserProc.alive(),
    close: async () => { calls.push("close"); },
  });

  it.each(["http", "ws"])("sync from a CDP endpoint (%s)", async (which) => {
    await setCookies();
    const res = await new Cloud().profiles.sync("acct-1", { fromCdp: which === "http" ? browserProc.httpUrl : browserProc.wsUrl, domains: ["example.com"] });
    const sent = api.requests("PUT").at(-1)!.body.cookies;
    expect(sent.map((c: { name: string }) => c.name).sort()).toEqual(["lang", "sid"]);
    const sid = sent.find((c: { name: string }) => c.name === "sid");
    // persistent (Chromium caps the lifetime at 400 days, so not the exact value set)
    expect(sid.domain === ".example.com" && sid.httpOnly === true && sid.expires > Date.now() / 1000).toBe(true);
    expect(sent.find((c: { name: string }) => c.name === "lang").expires).toBe(-1);
    expect(Object.keys(sid).every((k) => ["name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"].includes(k))).toBe(true);
    expect(res.imported).toBe(2);
    expect(browserProc.alive()).toBe(true); // reading cookies never closes the browser it reads from
  }, 60_000);

  it("sync from a profile directory reads a headless local browser", async () => {
    await setCookies();
    const calls: unknown[] = [];
    const dir = mkdtempSync(join(tmpdir(), "cc-prof-"));
    try {
      await new Cloud().profiles.sync("acct-1", {
        fromProfile: dir, domains: ["tracker.net"],
        serve: async (o) => { calls.push(o); return served(calls); },
      });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
    expect(calls[0]).toEqual({ userDataDir: dir, headless: true, quiet: true });
    expect(calls.at(-1)).toBe("close");
    expect(api.requests("PUT").at(-1)!.body.cookies.map((c: { name: string }) => c.name)).toEqual(["trk"]);
  }, 60_000);

  it("sync by login opens the page, waits, then reads the cookies", async () => {
    const calls: unknown[] = [];
    const res = await new Cloud().profiles.sync("acct-1", {
      loginUrl: `${api.url}/login-page`, domains: ["127.0.0.1"],
      serve: async (o) => { calls.push(o); return served(calls); },
      confirm: async () => {
        const deadline = Date.now() + 15_000;
        while (Date.now() < deadline && !api.requests("GET", "/login-page").length) await new Promise((r) => setTimeout(r, 50));
        await new Promise((r) => setTimeout(r, 300));
        calls.push("confirmed");
      },
    });
    expect(calls).toEqual([{ headless: false, quiet: true }, "confirmed", "close"]);
    const tabs = (await (await fetch(`${browserProc.httpUrl}/json/list`)).json()) as Array<{ url: string }>;
    expect(tabs.some((t) => t.url.endsWith("/login-page"))).toBe(true);
    const sent = api.requests("PUT").at(-1)!.body.cookies;
    expect(sent.map((c: { name: string; value: string; domain: string }) => [c.name, c.value, c.domain])).toEqual([["sid", "s3cret", "127.0.0.1"]]);
    expect(res.imported).toBe(1);
  }, 60_000);
});
