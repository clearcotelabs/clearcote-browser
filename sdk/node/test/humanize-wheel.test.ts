import { describe, it, expect } from "vitest";
import { attachHumanize } from "../src/humanize.js";
import { ISO_PLATFORM, ISO_VIEWPORT } from "../src/isolated.js";

// Humanized wheel = whole notches of the persona platform's size (mirrors the Python
// test_humanize wheel tests). Before this, one scroll of 100 was 5 CDP wheel events of
// 39/28/20/10/3 px, each reporting a full notch (wheelDelta -120): events no device emits.
//
// The page's platform is read through the isolated world, so the fake answers it at the CDP level
// (Runtime.evaluate in the isolated context); page.evaluate throws to prove the page world is never
// used. Sleeps are real, so the deltas stay a few notches.
const fakePage = (platform: string | null) => {
  const noop = async () => undefined;
  const wheels: [number, number][] = [];
  const evals: string[] = [];
  const cdp = {
    send: async (method: string, params?: any): Promise<any> => {
      if (method === "Page.getFrameTree") return { frameTree: { frame: { id: "MAIN" } } };
      if (method === "Page.createIsolatedWorld") return { executionContextId: 1 };
      if (method === "Runtime.evaluate") {
        evals.push(params.expression);
        if (params.expression.startsWith(`(${ISO_PLATFORM})`)) return { result: { value: platform } };
        if (params.expression.startsWith(`(${ISO_VIEWPORT})`)) return { result: { value: [1280, 800] } };
      }
      throw new Error(method);
    },
  };
  const locatorProto = {
    fill: noop, click: noop, type: noop, dblclick: noop, hover: noop, press: noop,
    pressSequentially: noop, clear: noop, tap: noop, check: noop, uncheck: noop,
    dragTo: noop, page: () => null,
  };
  const page = {
    mouse: {
      move: noop, click: noop, down: noop, up: noop,
      wheel: async (x: number, y: number) => { wheels.push([x, y]); },
    },
    keyboard: { type: noop, press: noop, down: noop, up: noop, insertText: noop },
    click: noop, hover: noop, dblclick: noop, fill: noop, press: noop, type: noop,
    focus: noop, on: () => undefined, mainFrame: () => ({}), waitForTimeout: noop,
    evaluate: () => { throw new Error("humanize must not evaluate in the page's world"); },
    context: () => ({ newCDPSession: async () => cdp }),
    locator: () => Object.create(locatorProto),
  };
  return { page: page as any, wheels, evals };
};

const wheelOn = async (platform: string | null, dx: number, dy: number) => {
  const f = fakePage(platform);
  await attachHumanize({} as never, f.page, { humanize: true, seed: "t" });
  await f.page.mouse.wheel(dx, dy);
  return f;
};

const SLOW = 20_000; // the scroll anchor glides the cursor in first, with real sleeps

describe("humanized wheel: whole notches", () => {
  it("Windows: 100 px is exactly one notch", async () => {
    expect((await wheelOn("Windows", 0, 100)).wheels).toEqual([[0, 100]]);
  }, SLOW);

  it("Windows: 39 px still scrolls one whole notch", async () => {
    expect((await wheelOn("Windows", 0, 39)).wheels).toEqual([[0, 100]]);
  }, SLOW);

  it("Linux: 250 px is two 120 px notches", async () => {
    expect((await wheelOn("Linux", 0, 250)).wheels).toEqual([[0, 120], [0, 120]]);
  }, SLOW);

  it("macOS: -100 px is three -40 px notches", async () => {
    expect((await wheelOn("macOS", 0, -100)).wheels).toEqual([[0, -40], [0, -40], [0, -40]]);
  }, SLOW);

  it("Windows: horizontal 150 px is two 100 px notches, one axis per event", async () => {
    expect((await wheelOn("Windows", 150, 0)).wheels).toEqual([[100, 0], [100, 0]]);
  }, SLOW);

  it("both axes: vertical notches first, then horizontal, never a diagonal event", async () => {
    const { wheels } = await wheelOn("Win32", -120, 210);
    expect(wheels).toEqual([[0, 100], [0, 100], [-100, 0]]);
  }, SLOW);

  it("every event moves exactly one notch, and the platform is read once per page", async () => {
    const f = fakePage("Linux x86_64");
    await attachHumanize({} as never, f.page, { humanize: true, seed: "t" });
    await f.page.mouse.wheel(0, 130);
    await f.page.mouse.wheel(-90, -300);
    expect(f.wheels.length).toBeGreaterThan(0);
    for (const [x, y] of f.wheels) {
      expect(x === 0 || y === 0).toBe(true);
      expect(Math.abs(x) + Math.abs(y)).toBe(120);
    }
    expect(f.evals.filter((e) => e.startsWith(`(${ISO_PLATFORM})`)).length).toBe(1);
  }, SLOW);

  it("no scroll asked, no wheel event", async () => {
    expect((await wheelOn("Windows", 0, 0)).wheels).toEqual([]);
  }, SLOW);

  it("falls back to the host OS when the page cannot say", async () => {
    const host = process.platform === "win32" ? 100 : process.platform === "darwin" ? 40 : 120;
    expect((await wheelOn(null, 0, host)).wheels).toEqual([[0, host]]);
  }, SLOW);
});
