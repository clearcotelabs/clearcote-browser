/**
 * Live, Node: the Frame API and ElementHandle actions on a humanized page are humanized. Before, they went straight
 * to Playwright (a 0.2-0.4 ms press, one teleporting move, 1-4 ms key holds) and strict mode said nothing (measured
 * 2026-10-28, SC 295). A cross-origin frame's handle falls back to Playwright, reported (strict mode throws).
 * Headless; skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary. Mirrors test_humanize_frame_handle_live.py.
 */
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Browser, Frame, Page } from "playwright-core";
import { launch } from "../src/index.js";

const LIVE = process.env.CLEARCOTE_LIVE_ENGINE;
const T = 90_000;

const REC = `<script>
window.__ev = [];
for (const t of ['mousemove','mousedown','mouseup','keydown','keyup'])
  document.addEventListener(t, (e) => window.__ev.push({ t, tr: e.isTrusted, ts: e.timeStamp, k: e.code || '' }), true);
</script>`;
const CHILD = `<!doctype html><html><head>${REC}</head><body style='margin:8px'>
<button id=fb style='width:120px;height:40px'>b</button><input id=fi></body></html>`;
const MAIN = (cross: string) => `<!doctype html><html><head>${REC}</head><body style='margin:10px'>
<div id=t style='width:40px;height:40px;background:#c33'></div><textarea id=ta></textarea><input id=cb type=checkbox>
<iframe id=same src='/child.html' style='width:400px;height:160px'></iframe>
<iframe id=cross src='${cross}/child.html' style='width:400px;height:160px'></iframe></body></html>`;

type E = { t: string; tr: boolean; ts: number; k: string };

/** What one action sent: moves, the press hold (ms) and the key holds (ms), across the page and a frame. */
async function measure(docs: (Page | Frame)[], act: () => Promise<unknown>) {
  for (const d of docs) await d.evaluate("window.__ev.length = 0");
  await act();
  const ev: E[] = [];
  for (const d of docs) ev.push(...((await d.evaluate("window.__ev")) as E[]));
  const at = (t: string) => ev.filter((e) => e.t === t).map((e) => e.ts);
  const down = at("mousedown"), up = at("mouseup");
  const keyDown = new Map<string, number>(), holds: number[] = [];
  for (const e of ev) {
    if (e.t === "keydown") keyDown.set(e.k, e.ts);
    if (e.t === "keyup" && keyDown.has(e.k)) holds.push(e.ts - keyDown.get(e.k)!);
  }
  return { moves: at("mousemove").length, press: down.length && up.length ? up[0] - down[0] : null, holds,
    trusted: ev.every((e) => e.tr) };
}

describe.runIf(LIVE)("live frame + element handle humanize (node)", () => {
  let server: Server;
  let base = "";
  let browser: Browser;
  let page: Page;
  let same: Frame;

  beforeAll(async () => {
    let pages: Record<string, string> = {};
    server = createServer((req, res) => {
      const body = pages[(req.url ?? "").split("?")[0]];
      if (body === undefined) { res.writeHead(404); res.end(); return; }
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(body);
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
    const port = (server.address() as AddressInfo).port;
    base = `http://127.0.0.1:${port}`;
    pages = { "/main.html": MAIN(`http://localhost:${port}`), "/child.html": CHILD };
    browser = await launch({ executablePath: LIVE, headless: true, humanize: true, fingerprint: "frame-handle-live-node",
      quiet: true, args: ["--no-sandbox"] });
    page = await browser.newPage();
    await page.goto(`${base}/main.html`);
    same = page.frames().find((f) => f.url().endsWith("/child.html") && f.url().startsWith(base))!;
  }, T);

  afterAll(async () => {
    await browser?.close();
    server?.close();
  });

  it("frame.click in a child frame and on the main frame is a human press", async () => {
    for (const [docs, act] of [
      [[page, same], () => same.click("#fb")],
      [[page], () => page.mainFrame().click("#t")],
    ] as const) {
      const m = await measure([...docs], act);
      expect(m.trusted).toBe(true);
      expect(m.moves).toBeGreaterThan(3);
      expect(m.press!).toBeGreaterThan(30);
    }
  }, T);

  it("elementHandle click / type / check are human input", async () => {
    const click = await measure([page], async () => (await page.$("#t"))!.click());
    expect(click.moves).toBeGreaterThan(3);
    expect(click.press!).toBeGreaterThan(30);
    const type = await measure([page], async () => (await page.$("#ta"))!.type("hello"));
    expect(type.holds.length).toBe(5);
    expect(Math.min(...type.holds)).toBeGreaterThan(30);
    expect(await page.inputValue("#ta")).toBe("hello");
    await (await page.$("#cb"))!.check();
    expect(await page.isChecked("#cb")).toBe(true);
  }, T);

  it("an element handle in a child frame is filled with typed keys", async () => {
    const m = await measure([page, same], async () => (await same.$("#fi"))!.fill("handle"));
    expect(m.holds.length).toBe(6);
    expect(await same.inputValue("#fi")).toBe("handle");
  }, T);

  it("a cross-origin frame's handle falls back to Playwright, reported", async () => {
    const cross = page.frames().find((f) => f.url().startsWith("http://localhost"))!;
    (page as any)._clearcoteLastFallback = undefined;
    await (await cross.$("#fb"))!.click();
    const fb = (page as any)._clearcoteLastFallback;
    expect(fb?.api).toBe("elementHandle.click");
    expect(String(fb?.reason)).toContain("cross-origin");
  }, T);
});
