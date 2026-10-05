/**
 * Live-engine geometry tests. Skipped unless CLEARCOTE_LIVE_ENGINE points at a chrome binary
 * (add CLEARCOTE_LICENSE_KEY for a PRO build). These belong in the release gate.
 *
 * WHY THEY EXIST: the unit tests could not catch the bug that actually shipped here. The window fit
 * read the page with `page.evaluate("() => [...]")`, and Playwright's JS binding evaluates an
 * arrow-function STRING to a function object instead of calling it — so the reader got undefined, the
 * plausibility guard said no, and the fit silently did nothing. The faked page in the unit tests
 * sniffed the string and played along. A real browser is the only thing that catches that class of
 * mismatch, and each SDK's binding has its own quirks.
 */
import { existsSync, mkdtempSync, readlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { describe, expect, it } from "vitest";
import type { Page } from "playwright-core";
import { launch, launchPersistentContext, serveNeedsNoSandbox } from "../src/index.js";
import { geometryIsCoherent, headlessGeometry, servedDisplay } from "../src/geometry.js";
import { runFitChild } from "./helpers/fit-child.js";
import { tempDir } from "./helpers/temp.js";

const LIVE_EXE = process.env.CLEARCOTE_LIVE_ENGINE;

const READ = "[[screen.width, screen.height], [screen.availWidth, screen.availHeight], " +
  "[innerWidth, innerHeight], [outerWidth, outerHeight], [screenX, screenY], [window.__resizes], " +
  "[matchMedia(`(device-width: ${screen.width}px) and (device-height: ${screen.height}px)`).matches]]";

async function read(page: Page) {
  const m = (await page.evaluate(READ)) as number[][];
  return { screen: m[0], avail: m[1], inner: m[2], outer: m[3], pos: m[4], resizes: m[5][0], mediaAgrees: m[6][0] };
}

/** The frame the engine draws stays in the range real captures show (it is per-platform). */
function expectPlausibleFrame(m: { inner: number[]; outer: number[] }) {
  const dx = m.outer[0] - m.inner[0];
  const dy = m.outer[1] - m.inner[1];
  expect(dx).toBeGreaterThanOrEqual(0);
  expect(dx).toBeLessThanOrEqual(16);
  expect(dy).toBeGreaterThanOrEqual(60);
  expect(dy).toBeLessThanOrEqual(160);
}

/** A window that stays on its screen: coherent, and not hanging past the work area's edge. */
function expectOnScreen(m: Awaited<ReturnType<typeof read>>) {
  expect(geometryIsCoherent(m.screen, m.avail, m.inner, m.outer)).toBe(true);
  expect(m.pos[0] + m.outer[0]).toBeLessThanOrEqual(m.avail[0]);
  expect(m.pos[1] + m.outer[1]).toBeLessThanOrEqual(m.avail[1]);
}

async function measure(fingerprint?: string, tabs = 1) {
  const dir = mkdtempSync(join(tmpdir(), "cc-live-node-"));
  try {
    const ctx = await launchPersistentContext(dir, {
      executablePath: LIVE_EXE, args: ["--no-sandbox"], quiet: true,
      ...(fingerprint ? { fingerprint } : {}),
    });
    try {
      // Runs before any page script: a window resized after a page starts running JS would show up
      // here as a resize event and a jump in innerWidth.
      await ctx.addInitScript("window.__resizes = 0; addEventListener('resize', () => { window.__resizes++; }, true);");
      const out = [];
      for (let i = 0; i < tabs; i++) {
        const page = await ctx.newPage();
        await page.goto("data:text/html,<body style='margin:0'>geo</body>");
        await page.waitForTimeout(700);   // first paint: innerWidth reads 0 before it
        out.push(await read(page));
      }
      return out;
    } finally {
      await ctx.close();
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

describe.runIf(LIVE_EXE)("live engine geometry", () => {
  it("regime 1: the persona owns the screen and the window is maximized into its work area", async () => {
    const [m] = await measure("live-geo-node");
    expect(geometryIsCoherent(m.screen, m.avail, m.inner, m.outer)).toBe(true);
    // screen must not have collapsed onto the viewport — that collapse is the original bug
    expect(m.screen).not.toEqual(m.inner);
    // the persona reserves a taskbar
    expect(m.avail[1]).toBeLessThan(m.screen[1]);
    // this is the assertion that catches a silently no-op fit (the arrow-function-string bug)
    expect(m.outer).toEqual(m.avail);
    expectPlausibleFrame(m);
    // and it all happened on about:blank, before the page ran a line of script
    expect(m.resizes).toBe(0);
  }, 120_000);

  it("regime 2: the seedless display is the cross-SDK row and the window is maximized into it", async () => {
    const [m] = await measure();
    // the same screen row the Python and .NET SDKs pick for no seed; a taskbar on a Windows host
    const { screen } = headlessGeometry(undefined);
    const display = servedDisplay({});
    expect(m.screen).toEqual([screen.width, screen.height]);
    expect(m.avail).toEqual([display.availWidth, display.availHeight]);
    // a real display, not an emulated screen: device-width media queries agree with screen.*
    expect(m.mediaAgrees).toBe(true);
    expectOnScreen(m);
    // maximized, whatever frame this platform's engine draws (linux 8x131 vs windows 16x134 is why
    // nothing is sized against a hardcoded frame any more)
    expect(m.outer).toEqual(m.avail);
    expect(m.pos).toEqual([0, 0]);
    expectPlausibleFrame(m);
    expect(m.resizes).toBe(0);
  }, 120_000);

  it("regime 2 through launch(): every new context is its own maximized window on the display", async () => {
    const browser = await launch({ executablePath: LIVE_EXE, args: ["--no-sandbox"], quiet: true });
    try {
      const out = [];
      for (const open of [() => browser.newPage(), async () => (await browser.newContext()).newPage()]) {
        const page = await open();
        await page.goto("data:text/html,<body style='margin:0'>geo</body>");
        await page.waitForTimeout(700);
        out.push(await read(page));
      }
      for (const m of out) {
        expectOnScreen(m);
        expect(m.avail[1]).toBe(servedDisplay({}).availHeight);
        expect(m.outer).toEqual(m.avail);
        expect(m.pos).toEqual([0, 0]);   // not Chrome's 10px-per-window cascade
        expectPlausibleFrame(m);
      }
    } finally {
      await browser.close();
    }
  }, 120_000);

  it("regime 2: popups, small and oversized, stay on the display", async () => {
    const dir = mkdtempSync(join(tmpdir(), "cc-live-node-"));
    try {
      const ctx = await launchPersistentContext(dir, { executablePath: LIVE_EXE, args: ["--no-sandbox"], quiet: true });
      try {
        const page = await ctx.newPage();
        await page.goto("data:text/html,<body style='margin:0'>geo</body>");
        for (const [w, h] of [[500, 400], [4000, 3000]]) {
          const [popup] = await Promise.all([
            ctx.waitForEvent("page"),
            page.evaluate(`window.open("about:blank", "_blank", "width=${w},height=${h}")`),
          ]);
          await popup.waitForTimeout(700);
          const m = await read(popup);
          expectOnScreen(m);
          // window.open() features are honoured (no forced size), clamped to the work area
          if (w < m.avail[0]) expect(m.inner[0]).toBe(w);
        }
      } finally {
        await ctx.close();
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }, 120_000);

  it("a second tab in the same window reports the same geometry", async () => {
    const [first, second] = await measure(undefined, 2);
    expect(second.screen).toEqual(first.screen);
    expect(second.inner).toEqual(first.inner);
    expect(second.outer).toEqual(first.outer);
    expect(second.resizes).toBe(0);
  }, 150_000);
});

/**
 * serve() in a Node with no global WebSocket: Node 20, which the SDK supports, or a newer one run with
 * `--no-experimental-websocket`. The fit used to ride on that global and was skipped without it, so
 * the served display was right and the window stayed the engine's default (945-1050px wide inside a
 * 1366-1920px screen), and sites served their tablet layout.
 */
async function serveWithoutWebSocket(opts: Record<string, unknown>) {
  const userDataDir = tempDir("cc-live-serve-");
  const r = await runFitChild({
    FIT_MODE: "serve",
    // No --no-sandbox: serve() adds it itself where it is needed (root on Linux), and anywhere else its
    // "unsupported command-line flag" infobar would come out of the page's height.
    FIT_SERVE_OPTS: JSON.stringify({ executablePath: LIVE_EXE, quiet: true, userDataDir, ...opts }),
  }, 120_000);
  // close() returned once the browser had exited, and took the engine's singleton-socket directory
  // (Linux and macOS; the caller's profile still links to it) along: the profile is all that is left.
  expect(() => process.kill(r.pid as number, 0)).toThrow();
  let socket: string | null = null;
  try { socket = readlinkSync(join(userDataDir, "SingletonSocket")); } catch { /* none on Windows */ }
  if (socket) expect(existsSync(dirname(socket))).toBe(false);
  expect(r.webSocket).toBe("undefined");   // the child really had none
  return r as unknown as Awaited<ReturnType<typeof read>>;
}

/**
 * serve() adds --no-sandbox itself as root on Linux, and the engine then shows its "unsupported
 * command-line flag" infobar, which takes ~50px off the page's height. Not what these tests are about.
 */
const servedFrameIsPlain = !serveNeedsNoSandbox(process.platform, process.getuid?.(), []);

describe.runIf(LIVE_EXE)("live engine geometry: serve() in a Node without a global WebSocket", () => {
  it("regime 2 (lightStealth): the served window is maximized into the seed's display", async () => {
    const m = await serveWithoutWebSocket({ fingerprint: "live-serve-node", lightStealth: true });
    const d = servedDisplay({ seed: "live-serve-node", lightStealth: true });
    expect(m.screen).toEqual([d.width, d.height]);
    expect(m.avail).toEqual([d.availWidth, d.availHeight]);
    expect(m.outer).toEqual(m.avail);
    expectOnScreen(m);
    if (servedFrameIsPlain) expectPlausibleFrame(m);
  }, 150_000);

  it("windowSize is honoured inside the work area and clamped past it", async () => {
    const d = servedDisplay({ screenWidth: 1366, screenHeight: 768 });
    const clamped = await serveWithoutWebSocket({ screenWidth: 1366, screenHeight: 768, windowSize: { width: 1920, height: 1080 } });
    expect(clamped.avail).toEqual([d.availWidth, d.availHeight]);
    expect(clamped.outer).toEqual(clamped.avail);
    expectOnScreen(clamped);
    const inside = await serveWithoutWebSocket({ screenWidth: 1366, screenHeight: 768, windowSize: { width: 1280, height: 700 } });
    expect(inside.outer).toEqual([1280, 700]);
    expectOnScreen(inside);
  }, 300_000);

  it("regime 1: the persona's display and a window maximized into it", async () => {
    // The seed of the launch() regime-1 test above: its persona reserves a taskbar.
    const m = await serveWithoutWebSocket({ fingerprint: "live-geo-node" });
    expect(m.avail[1]).toBeLessThan(m.screen[1]);
    // Past the real 800x600 headless display: the display was made the persona's own first.
    expect(m.outer[0]).toBeGreaterThan(800);
    expect(m.outer).toEqual(m.avail);
    expectOnScreen(m);
    if (servedFrameIsPlain) expectPlausibleFrame(m);
  }, 150_000);
});
