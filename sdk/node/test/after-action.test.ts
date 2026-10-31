/**
 * After-action behaviour (SC 232 lane C) -- a port of sdk/python/tests/test_humanize_after_action.py, on a
 * fake page under vitest fake timers (Date + performance + setTimeout), so it is exact and fast.
 *
 * Measured on r42 / SDK 0.51.0 (the review): a hover stopped dead on arrival -- 0 pointer samples after the
 * glide's last one -- and after a click the pointer never left the pressed pixel. Bands (EVAL.md rows C):
 *   hover dwell -- >= 3 samples after arrival, within 3 px of it and inside the box, 150-900 ms per seed;
 *   move-off    -- 60-85 % of clicks per seed (40 clicks), 200-700 ms after the release, <= 200 px, downward;
 *   attention   -- the first action after a load waits 0.8-4 s, longer with more text; idle drifts after > 1.5 s;
 *                  ON unless attention: false (SC 299);
 *   eager move-off -- (SC 299) the click itself waits the move-off delay and drifts off before it returns, unless the
 *                  page says the point is not safe to leave (a field, a menu, a popup trigger: ISO_MOVE_OFF_SAFE);
 *   ambient     -- "load" is the per-load burst, "idle" the attention model; nothing on trial / held / hold.
 * The planners are pinned to Python's by sdk/testdata/humanize_after_vectors.json.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { attachHumanize } from "../src/humanize.js";
import * as ISO from "../src/isolated.js";
import { ISO_CSS_BOX, ISO_FRAMES, ISO_PLATFORM, ISO_VIEWPORT } from "../src/isolated.js";
import * as M from "../src/motion.js";
import * as A from "../src/attention.js";

const TEXT_LENGTH: string | undefined = (A as any).TEXT_LENGTH;
const MOVE_OFF_SAFE: string | undefined = (ISO as any).ISO_MOVE_OFF_SAFE;   // 0.67.1: no eager move-off
const SEEDS = ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"];
const BOX = { x: 400, y: 300, width: 120, height: 40 };
type Ev = { op: string; t: number; x?: number; y?: number; a?: any };

const fakePage = () => {
  const log: Ev[] = [];
  const handlers: Record<string, (() => void)[]> = {};
  // moveOffSafe: what ISO_MOVE_OFF_SAFE answers (null: the page cannot answer -> the lazy rule)
  const st: { textLen: number; moveOffSafe: boolean | null } = { textLen: 0, moveOffSafe: null };
  const ev = async (op: string, x?: number, y?: number, a?: any) => { log.push({ op, t: Date.now(), x, y, a }); };
  const noop = async () => undefined;
  const cdp = {
    send: async (method: string, params: any = {}): Promise<any> => {
      switch (method) {
        case "Page.getFrameTree": return { frameTree: { frame: { id: "MAIN" } } };
        case "Page.createIsolatedWorld": return { executionContextId: 1 };
        case "Runtime.evaluate": {
          const e: string = params.expression;
          if (e.startsWith(`(${ISO_PLATFORM})`)) return { result: { value: "Win32" } };
          if (e.startsWith(`(${ISO_VIEWPORT})`)) return { result: { value: [1280, 800] } };
          if (e.startsWith(`(${ISO_FRAMES})`)) return { result: { value: true } };
          if (e.startsWith(`(${ISO_CSS_BOX})`)) return { result: { value: null } };
          if (TEXT_LENGTH && e.startsWith(`(${TEXT_LENGTH})`)) return { result: { value: st.textLen } };
          if (MOVE_OFF_SAFE && e.startsWith(`(${MOVE_OFF_SAFE})`)) return { result: { value: st.moveOffSafe } };
          return { result: { value: [0, 0] } };
        }
        case "DOM.getNodeForLocation": return { backendNodeId: 7, frameId: "MAIN" };
        case "DOM.resolveNode": return { object: { objectId: "o7" } };
        case "Runtime.callFunctionOn": return { result: { value: true } };
        case "Runtime.releaseObject": return {};
        default: throw new Error(`unexpected CDP ${method}`);
      }
    },
  };
  const MAIN = {};
  const locatorProto = {
    fill: noop, click: noop, type: noop, dblclick: noop, hover: noop, press: noop,
    pressSequentially: noop, clear: noop, tap: noop, check: noop, uncheck: noop, dragTo: noop, page: () => null,
  };
  const page: any = {
    mouse: {
      move: (x: number, y: number) => ev("move", x, y),
      down: (o?: any) => ev("down", undefined, undefined, o), up: (o?: any) => ev("up", undefined, undefined, o),
      click: (x: number, y: number) => ev("click", x, y),
      wheel: (dx: number, dy: number) => ev("wheel", dx, dy), dblclick: noop,
    },
    keyboard: { type: noop, press: noop, down: noop, up: noop, insertText: noop },
    click: (s: string, o: any) => ev("native-click", undefined, undefined, o),
    hover: (s: string, o: any) => ev("native-hover", undefined, undefined, o),
    dblclick: noop, fill: noop, press: noop, type: noop, focus: noop,
    on: (name: string, fn: () => void) => { (handlers[name] ??= []).push(fn); },
    mainFrame: () => MAIN, waitForTimeout: noop,
    viewportSize: () => ({ width: 1280, height: 800 }),
    evaluate: () => { throw new Error("humanize must not evaluate in the page's world"); },
    context: () => ({ newCDPSession: async () => cdp, browser: () => null }),
    locator: (selector: string) => {
      const loc: any = Object.create(locatorProto);
      Object.assign(loc, {
        _selector: selector, _frame: MAIN, page: () => page, first: () => loc,
        isVisible: async () => true, isEnabled: async () => true, count: async () => 1,
        waitFor: noop, scrollIntoViewIfNeeded: noop, boundingBox: async () => ({ ...BOX }),
      });
      return loc;
    },
  };
  const fire = (name: string) => { for (const fn of handlers[name] ?? []) fn(); };
  return { page, log, fire, st };
};

const drive = async <T>(p: Promise<T>): Promise<T> => {
  let done = false;
  const q = p.finally(() => { done = true; });
  for (let i = 0; !done && i < 200_000; i++) await vi.advanceTimersByTimeAsync(2);
  return q;
};

const setup = async (seed: string, humanize: any = true) => {
  const f = fakePage();
  await attachHumanize({} as never, f.page, { humanize, seed });
  return f;
};
const moves = (f: { log: Ev[] }, since = -Infinity) => f.log.filter((e) => e.op === "move" && e.t >= since);
const trace = (f: { page: any }): any[][] => (Array.isArray(f.page._clearcoteAfterTrace) ? f.page._clearcoteAfterTrace : []);
const settle = (f: { page: any }) => (typeof f.page.settle === "function" ? f.page.settle() : Promise.resolve(undefined));

beforeEach(() => {
  process.env.CLEARCOTE_HUMANIZE_ENGINE_CLICKS = "off";
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "performance", "Date"] });
});
afterEach(() => { vi.useRealTimers(); delete process.env.CLEARCOTE_HUMANIZE_ENGINE_CLICKS; });

const afterArrival = (f: { log: Ev[] }, t0: number) => {
  const mv = moves(f, t0);
  let arrive = -1;
  mv.forEach((e, i) => { if (Number.isInteger(e.x) && Number.isInteger(e.y)) arrive = i; });
  return { at: mv[arrive], rest: mv.slice(arrive + 1) };
};

describe("hover dwell", () => {
  it.each(SEEDS)("%s: >= 3 samples after arrival, within 3 px and the box, 150-900 ms", async (seed) => {
    const f = await setup(seed);
    const t0 = Date.now();
    await drive(f.page.hover("#t"));
    const end = Date.now();
    const { at, rest } = afterArrival(f, t0);
    expect(rest.length, `${rest.length} samples after arrival`).toBeGreaterThanOrEqual(3);
    for (const e of rest) {
      expect(Math.hypot(e.x! - at.x!, e.y! - at.y!)).toBeLessThanOrEqual(3 + 1e-9);
      expect(e.x! > BOX.x && e.x! < BOX.x + BOX.width && e.y! > BOX.y && e.y! < BOX.y + BOX.height).toBe(true);
    }
    expect(end - at.t).toBeGreaterThanOrEqual(150);
    expect(end - at.t).toBeLessThanOrEqual(900);
  }, 60_000);

  it("dwell: 0 disables, a number overrides, and it never reaches a native hover", async () => {
    const f = await setup("ha-1");
    let t0 = Date.now();
    await drive(f.page.hover("#t", { dwell: 0 }));
    expect(afterArrival(f, t0).rest).toEqual([]);
    t0 = Date.now();
    await drive(f.page.hover("#t", { dwell: 400 }));
    const { at, rest } = afterArrival(f, t0);
    expect(rest.length).toBeGreaterThanOrEqual(3);
    expect(Date.now() - at.t).toBeGreaterThanOrEqual(380);
    f.page.locator = () => ({ first() { return this; }, isVisible: async () => true, boundingBox: async () => null, count: async () => 1 });
    await drive(f.page.hover("#t", { dwell: 300 }));
    const nat = f.log.filter((e) => e.op === "native-hover");
    expect(nat.length).toBe(1);
    expect(nat[0].a?.dwell).toBeUndefined();
  }, 60_000);
});

describe("move-off after a click", () => {
  it.each(SEEDS)("%s: 60-85 %% of 40 clicks, 200-700 ms after the release, <= 200 px, downward", async (seed) => {
    const f = await setup(seed);
    let off = 0;
    for (let i = 0; i < 40; i++) {
      const x = 200 + (i * 137) % 800, y = 150 + (i * 89) % 450;
      await drive(f.page.mouse.click(x, y));
      const ups = f.log.filter((e) => e.op === "up");
      const tUp = ups[ups.length - 1].t;
      const t = Date.now();
      await drive(settle(f));
      const mv = moves(f, t + 0.001);
      if (mv.length) {
        off++;
        const lag = mv[0].t - tUp;
        expect(lag).toBeGreaterThanOrEqual(200);
        expect(lag).toBeLessThanOrEqual(705);
        const last = mv[mv.length - 1];
        const d = Math.hypot(last.x! - x, last.y! - y);
        expect(d).toBeGreaterThanOrEqual(12);
        expect(d).toBeLessThanOrEqual(200 + 1e-6);
        expect(last.y!).toBeGreaterThan(y);
      }
      await vi.advanceTimersByTimeAsync(50);
    }
    expect(off / 40).toBeGreaterThanOrEqual(0.6);
    expect(off / 40).toBeLessThanOrEqual(0.85);
  }, 120_000);

  it("is dropped when the next action comes first; runs at the next action once due", async () => {
    const f = await setup("ha-2");
    for (let i = 0; i < 5; i++) {
      await drive(f.page.mouse.click(500, 300));
      await drive(f.page.mouse.click(700, 400));
    }
    expect(trace(f).filter((e) => e[0] === "move_off")).toEqual([]);
    let ran = 0;
    for (let i = 0; i < 10; i++) {
      await drive(f.page.mouse.click(500, 300));
      await vi.advanceTimersByTimeAsync(750);
      const b = trace(f).filter((e) => e[0] === "move_off").length;
      await drive(f.page.mouse.click(600, 200));
      ran += trace(f).filter((e) => e[0] === "move_off").length - b;
      await vi.advanceTimersByTimeAsync(750);
    }
    expect(ran).toBeGreaterThanOrEqual(3);
  }, 120_000);
});

describe("eager move-off (SC 299)", () => {
  it.each(SEEDS)("%s: leaves the pressed pixel before the click returns, then nothing moves through a 1.6 s rest", async (seed) => {
    // SC 299 (r54 / 0.67.1): with the lazy rule alone the pointer stayed on the pressed pixel through every 1.6 s
    // pause after a click (0 of 12). Now the click waits the persona's 200-700 ms and drifts off before it
    // returns, in its 60-85 % of clicks, when the page says the point may be left.
    const f = await setup(seed);
    f.st.moveOffSafe = true;
    let off = 0;
    const n = 40;
    for (let i = 0; i < n; i++) {
      const x = 200 + (i * 137) % 800, y = 150 + (i * 89) % 450;
      await drive(f.page.mouse.click(x, y));   // returns after any eager move-off
      const ups = f.log.filter((e) => e.op === "up");
      const tUp = ups[ups.length - 1].t;
      const mv = moves(f, tUp);
      if (mv.length) {
        off++;
        const lag = mv[0].t - tUp;
        expect(lag).toBeGreaterThanOrEqual(200);
        expect(lag).toBeLessThanOrEqual(705);
        const last = mv[mv.length - 1];
        const d = Math.hypot(last.x! - x, last.y! - y);
        expect(d).toBeGreaterThanOrEqual(12);
        expect(d).toBeLessThanOrEqual(200 + 1e-6);
      }
      await vi.advanceTimersByTimeAsync(1600);   // the script rests: nothing more may move
      expect(moves(f, Date.now() - 1500)).toEqual([]);
    }
    expect(off / n).toBeGreaterThanOrEqual(0.6);
    expect(off / n).toBeLessThanOrEqual(0.85);
    expect(trace(f).some((e) => e[0] === "move_off_eager")).toBe(true);
  }, 120_000);

  it("stays lazy where the page says no", async () => {
    const f = await setup("ha-2");
    f.st.moveOffSafe = false;   // a field, a menu, a popup trigger
    for (let i = 0; i < 12; i++) {
      await drive(f.page.mouse.click(300 + i * 20, 300));
      const ups = f.log.filter((e) => e.op === "up");
      expect(moves(f, ups[ups.length - 1].t), "no drift inside the click").toEqual([]);
    }
    expect(trace(f).some((e) => e[0] === "move_off_eager")).toBe(false);
  }, 60_000);
});

describe("never on trial / held / hold", () => {
  it("a trial moves nothing and runs nothing after-action", async () => {
    const f = await setup("ha-1");
    f.page._clearcoteAttention = true;
    f.fire("load");
    const n = f.log.length;
    await drive(f.page.click("#t", { trial: true }));
    await drive(f.page.hover("#t", { trial: true }));
    expect(f.log.slice(n)).toEqual([]);
    expect(trace(f)).toEqual([]);
  }, 60_000);

  it("no move-off while a button is held", async () => {
    const f = await setup("ha-1");
    for (let i = 0; i < 6; i++) {
      await drive(f.page.mouse.click(400, 400));
      await vi.advanceTimersByTimeAsync(800);
      await drive(f.page.mouse.down());
      await vi.advanceTimersByTimeAsync(800);
      await drive(f.page.mouse.move(600, 420));
      await drive(f.page.mouse.up());
    }
    expect(trace(f).filter((e) => e[0] === "move_off")).toEqual([]);
    expect(await drive(settle(f))).toBeFalsy();
  }, 120_000);

  it("press-and-hold: nothing moves mid-hold and no move-off follows", async () => {
    const f = await setup("ha-1");
    f.page._clearcoteAttention = true;
    await drive(f.page.pressAndHold("#t", { duration: 1500, tremor: false }));
    const d = f.log.findIndex((e) => e.op === "down"), u = f.log.findIndex((e) => e.op === "up");
    expect(d).toBeGreaterThanOrEqual(0);
    expect(f.log.slice(d + 1, u).filter((e) => e.op === "move")).toEqual([]);
    expect(await drive(settle(f))).toBeFalsy();
  }, 60_000);
});

describe("attention", () => {
  const firstPress = async (seed: string, textLen: number) => {
    const f = await setup(seed);
    f.page._clearcoteAttention = true;
    f.st.textLen = textLen;
    f.fire("load");
    const t = Date.now();
    await drive(f.page.click("#t"));
    return { f, ms: f.log.find((e) => e.op === "down")!.t - t };
  };
  it.each(SEEDS)("%s: the read pause scales with the page's text", async (seed) => {
    const s = await firstPress(seed, 120);
    const l = await firstPress(seed, 20000);
    expect(s.ms).toBeGreaterThanOrEqual(800);
    expect(l.ms - s.ms).toBeGreaterThanOrEqual(1500);
    expect(trace(l.f).some((e) => e[0] === "drift")).toBe(true);
  }, 120_000);

  it("is on by default; attention: false turns it off; the page flag overrides both ways", async () => {
    // SC 299: measured on r54 / 0.67.1 with the model off, the first input came 0.21-0.31 s after the load.
    const f = await setup("ha-1");
    f.st.textLen = 20000;
    f.fire("load");
    let t = Date.now();
    await drive(f.page.click("#t"));
    expect(trace(f).some((e) => e[0] === "read"), "on by default").toBe(true);
    expect(Date.now() - t).toBeGreaterThanOrEqual(800);
    const f2 = await setup("ha-1", { attention: false });
    f2.st.textLen = 20000;
    f2.fire("load");
    t = Date.now();
    await drive(f2.page.click("#t"));
    expect(Date.now() - t).toBeLessThan(3000);
    expect(trace(f2).filter((e) => e[0] === "read" || e[0] === "drift")).toEqual([]);
    const f3 = await setup("ha-1");
    f3.page._clearcoteAttention = false;   // the page flag overrides both ways
    f3.st.textLen = 20000;
    f3.fire("load");
    await drive(f3.page.click("#t"));
    expect(trace(f3).filter((e) => e[0] === "read" || e[0] === "drift")).toEqual([]);
    const f4 = await setup("ha-1", { attention: false });
    f4.page._clearcoteAttention = true;
    f4.st.textLen = 20000;
    f4.fire("load");
    await drive(f4.page.click("#t"));
    expect(trace(f4).some((e) => e[0] === "read")).toBe(true);
  }, 60_000);

  it("idle drifts after a gap > 1.5 s only", async () => {
    const f = await setup("ha-4");
    f.page._clearcoteAttention = true;
    await drive(f.page.mouse.click(300, 300));
    await vi.advanceTimersByTimeAsync(1000);
    await drive(f.page.mouse.click(500, 300));
    expect(trace(f).filter((e) => e[0] === "drift")).toEqual([]);
    await vi.advanceTimersByTimeAsync(2000);
    await drive(f.page.mouse.click(700, 300));
    const dr = trace(f).filter((e) => e[0] === "drift");
    expect(dr.length).toBeGreaterThanOrEqual(1);
    expect(dr.length).toBeLessThanOrEqual(2);
    for (const e of dr) { expect(e[1]).toBeGreaterThanOrEqual(300); expect(e[1]).toBeLessThanOrEqual(900); }
  }, 60_000);
});

describe("ambient option", () => {
  it("load = the per-load burst, idle = the attention model, the hidden flag still works", async () => {
    const f = await setup("ha-1");
    f.fire("load");
    await vi.advanceTimersByTimeAsync(3000);
    expect(moves(f)).toEqual([]);
    // Through the real options object (optionsOf(page)); the flag the options also set is cleared, so the
    // burst can only come from the option itself.
    const f2 = await setup("ha-1", { ambient: "load" });
    f2.page._clearcoteAutoAmbient = false;
    f2.fire("load");
    await vi.advanceTimersByTimeAsync(3000);
    expect(moves(f2).length).toBeGreaterThan(20);
    const g = await setup("ha-1", { ambient: "idle" });
    g.fire("load");
    await drive(g.page.click("#t"));
    expect(trace(g).some((e) => e[0] === "read")).toBe(true);
    const h = await setup("ha-1");
    h.page._clearcoteAutoAmbient = true;
    h.fire("load");
    await vi.advanceTimersByTimeAsync(3000);
    expect(moves(h).length).toBeGreaterThan(20);
  }, 60_000);
});

describe("randomness", () => {
  it("the seed fixes traits only: two fresh personas of one seed draw different per-action samples", async () => {
    const m: any = M;
    expect(m.afterTraits(M.makePersona("same-1"))).toEqual(m.afterTraits(M.makePersona("same-1")));
    const seqs: number[][] = [];
    for (let k = 0; k < 2; k++) {
      const f = await setup("same-1");
      for (let i = 0; i < 5; i++) await drive(f.page.hover("#t"));
      seqs.push(trace(f).filter((e) => e[0] === "dwell").map((e) => e[1] as number));
    }
    expect(seqs[0].length).toBe(5);
    expect(seqs[0]).not.toEqual(seqs[1]);
  }, 60_000);
});

// ------------------------------------------------------------------------------- shared vectors
const VEC = JSON.parse(readFileSync(fileURLToPath(new URL("../../testdata/humanize_after_vectors.json", import.meta.url)), "utf8"));
const near = (a: number, b: number) => expect(Math.abs(a - b)).toBeLessThan(1e-6);
const pick = (steps: M.Step[], c: any) => {
  expect(steps.length).toBe(c.n);
  near(steps.reduce((s, x) => s + x.sleepMs, 0), c.total_ms);
  for (const [i, x, y, sl] of c.samples) { near(steps[i].x, x); near(steps[i].y, y); near(steps[i].sleepMs, sl); }
};

describe("shared after-action vectors (Python, Node and .NET)", () => {
  const m: any = M, a: any = A;
  it.each((VEC.after_traits as any[]).map((t) => [t.persona, t]))("afterTraits %s", (_, t: any) => {
    const r = m.afterTraits(M.makePersona(t.persona));
    near(r.dwellMs, t.dwell_ms); near(r.moveOffRate, t.move_off_rate);
    near(r.moveOffDelayMs, t.move_off_delay_ms); near(r.readTempo, t.read_tempo);
  });
  it.each((VEC.ambient as any[]).map((c, i) => [i, c]))("planAmbient #%i", (_, c: any) => {
    pick(M.planAmbient({ x: c.from[0], y: c.from[1] }, { width: c.viewport[0], height: c.viewport[1] },
      M.makePersona(c.persona), c.ms, M.mulberry32(c.rng)), c);
  });
  it.each((VEC.dwell as any[]).map((c, i) => [i, c]))("planDwell #%i", (_, c: any) => {
    const box = c.box ? { x: c.box[0], y: c.box[1], width: c.box[2], height: c.box[3] } : null;
    pick(m.planDwell({ x: c.at[0], y: c.at[1] }, box, M.makePersona(c.persona), c.ms, M.mulberry32(c.rng)), c);
  });
  it.each((VEC.streams as any[]).map((c) => [c.persona, c]))("after-action stream %s", (_, c: any) => {
    const p = M.makePersona(c.persona);
    const r = m.afterRng(p);
    near(m.dwellMs(p, r), c.dwell_ms);
    let hits = 0, k = 0;
    for (let i = 0; i < 40; i++) {
      const hit = m.moveOffRoll(p, r, hits, i);
      expect(hit ? 1 : 0).toBe(c.rolls[i]);
      if (hit) {
        hits++;
        near(m.moveOffDelayMs(p, r), c.delays[k]);
        const t = m.planMoveOff({ x: 200 + (i * 137) % 800, y: 150 + (i * 89) % 600 }, { width: 1280, height: 800 }, p, r);
        near(t.x, c.targets[k][0]); near(t.y, c.targets[k][1]);
        k++;
      }
    }
    [0, 120, 3000, 20000, 50000].forEach((n, i) => near(a.readPauseMs(n, p, r), c.reads[i]));
    for (const want of c.notches) expect(a.exploreNotches(r)).toBe(want);
    [1.0, 1.6, 5.0].forEach((g, i) => {
      const got: number[] = a.idleDrifts(g, r);
      expect(got.length).toBe(c.idle[i].length);
      got.forEach((v, j) => near(v, c.idle[i][j]));
    });
  });
});

describe("per-call humanize: false", () => {
  it("goes straight to the stock call and skips the after-action step (lanes A + C)", async () => {
    const f = await setup("ha-1");
    f.page._clearcoteAttention = true;
    f.fire("load");
    await drive(f.page.click("#t", { humanize: false }));
    expect(trace(f).some((e) => e[0] === "read" || e[0] === "drift" || e[0] === "move_off")).toBe(false);
    await drive(f.page.click("#t"));
    expect(trace(f).some((e) => e[0] === "read")).toBe(true);
  }, 60_000);
});
