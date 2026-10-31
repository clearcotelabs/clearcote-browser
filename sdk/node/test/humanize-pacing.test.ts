import { describe, it, expect, vi, afterEach } from "vitest";

// Wall-clock pacing of planned pointer paths (SC 210 lane P; mirrors tests/test_humanize_pacing.py).
//
// The planner returns ~130 samples ~6.6 ms apart for a ~1 s glide. The old dispatch loop awaited
// mouse.move for each sample and THEN slept the planned gap, so the dispatch round trip (27 ms median
// on PRO r38) was added to every sample: glides ran 3.5-5.4 s, ambientMotion(1500) 5.7x as long
// (SC 209). These tests drive the real attachHumanize wrappers with a fake mouse whose move costs 0 or
// 25 ms of FAKE time (vitest fake timers + performance), so they are exact and fast. On 0.48.0 the
// 25 ms duration checks and the ambient checks fail.

const spy = vi.hoisted(() => ({ plans: [] as { from: { x: number; y: number }; plan: any[] }[] }));
vi.mock("../src/motion.js", async (importOriginal) => {
  const m: any = await importOriginal();
  return {
    ...m,
    planMove: (from: any, to: any, p: any, o: any) => {
      const plan = m.planMove(from, to, p, o);
      spy.plans.push({ from: { x: from.x, y: from.y }, plan });
      return plan;
    },
    enterFromTop: (steps: any[], start: any, rng?: any) => {
      // A fresh page's first glide (SC 239): the samples above the view are not dispatched, so the walk the test
      // checks is the kept part, from its first (crossing) sample.
      const out = m.enterFromTop(steps, start, rng);
      const last = spy.plans[spy.plans.length - 1];
      if (last && last.plan === steps && out !== steps) { last.from = { x: out[0].x, y: out[0].y }; last.plan = out; }
      return out;
    },
  };
});

import { attachHumanize } from "../src/humanize.js";
import { ISO_PLATFORM, ISO_VIEWPORT } from "../src/isolated.js";
import { walk, trimTo, schedule, speedAt, STALL_MS, FAST_PX_PER_MS, MAX_JUMP_PX, jumpCap, nextIndex } from "../src/pacing.js";
import { hopCapPx, makePersona, mulberry32, planAmbient, planMove } from "../src/motion.js";

type Ev = { op: string; t: number; x?: number; y?: number };

const fakePage = (latMs: number) => {
  const log: Ev[] = [];
  const ev = async (op: string, x?: number, y?: number) => {
    await new Promise((r) => setTimeout(r, latMs));
    log.push({ op, t: performance.now(), x, y });
  };
  const noop = async () => undefined;
  const cdp = {
    send: async (method: string, params?: any): Promise<any> => {
      if (method === "Page.getFrameTree") return { frameTree: { frame: { id: "MAIN" } } };
      if (method === "Page.createIsolatedWorld") return { executionContextId: 1 };
      if (method === "Runtime.evaluate") {
        if (params.expression.startsWith(`(${ISO_PLATFORM})`)) return { result: { value: "Win32" } };
        if (params.expression.startsWith(`(${ISO_VIEWPORT})`)) return { result: { value: [1280, 800] } };
        return { result: { value: null } };
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
      move: (x: number, y: number) => ev("move", x, y),
      down: () => ev("down"), up: () => ev("up"),
      click: noop, wheel: noop, dblclick: noop,
    },
    keyboard: { type: noop, press: noop, down: noop, up: noop, insertText: noop },
    click: noop, hover: noop, dblclick: noop, fill: noop, press: noop, type: noop,
    focus: noop, on: () => undefined, mainFrame: () => ({}), waitForTimeout: noop,
    viewportSize: () => ({ width: 1280, height: 800 }),
    evaluate: () => { throw new Error("humanize must not evaluate in the page's world"); },
    context: () => ({ newCDPSession: async () => cdp }),
    locator: () => Object.create(locatorProto),
  };
  return { page: page as any, log };
};

// Run an async action to completion under fake timers, 1 ms of fake time per tick.
const drive = async <T>(p: Promise<T>): Promise<T> => {
  let done = false;
  const q = p.finally(() => { done = true; });
  for (let i = 0; !done && i < 120_000; i++) await vi.advanceTimersByTimeAsync(1);
  return q;
};

const maxGap = (pts: { x: number; y: number }[]) => {
  let m = 0;
  for (let i = 1; i < pts.length; i++) m = Math.max(m, Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y));
  return m;
};

// The largest persona hop cap (motion.ts hopCapPx draws 55-110 px).
const HOP_CAP_MAX = 110;

// No dispatched hop longer than a fast hand covers between the two events (3 px/ms, >= floor).
const hopsOk = (moves: Ev[], floor = HOP_CAP_MAX): boolean => {
  for (let i = 1; i < moves.length; i++) {
    const d = Math.hypot(moves[i].x! - moves[i - 1].x!, moves[i].y! - moves[i - 1].y!);
    if (d > Math.max(floor, FAST_PX_PER_MS * (moves[i].t - moves[i - 1].t)) + 1e-6) return false;
  }
  return true;
};

const checkWalk = (from: { x: number; y: number }, plan: any[], moves: Ev[], dur: number) => {
  const planned = plan.reduce((a, s) => a + s.sleepMs, 0);
  expect(dur, `glide took ${dur.toFixed(0)} ms, planned ${planned.toFixed(0)} ms`).toBeGreaterThanOrEqual(0.8 * planned - 1);
  expect(dur, `glide took ${dur.toFixed(0)} ms, planned ${planned.toFixed(0)} ms`).toBeLessThanOrEqual(1.2 * planned + 1);
  const lastM = moves[moves.length - 1], lastP = plan[plan.length - 1];
  expect([lastM.x, lastM.y]).toEqual([lastP.x, lastP.y]);
  const own = maxGap([from, ...plan]);
  expect(Math.hypot(moves[0].x! - from.x, moves[0].y! - from.y)).toBeLessThanOrEqual(Math.max(HOP_CAP_MAX, own) + 1e-6);
  expect(hopsOk(moves, Math.max(HOP_CAP_MAX, own)), "a skipped stretch teleported the cursor").toBe(true);
};

const setup = async (lat: number, seed: string) => {
  const f = fakePage(lat);
  await attachHumanize({} as never, f.page, { humanize: true, seed });
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "performance", "Date"] });
  return f;
};

afterEach(() => { vi.useRealTimers(); });

describe("humanize pacing: planned paths keep their planned duration", () => {
  for (const lat of [0, 25]) {
    it(`bare glides at ${lat} ms dispatch latency`, async () => {
      const f = await setup(lat, "pace-1");
      for (const [x, y] of [[900, 600], [150, 150], [640, 420], [1100, 120]]) {
        const n0 = spy.plans.length;
        const t0 = performance.now();
        await drive(f.page.mouse.move(x, y));
        const t1 = performance.now();
        expect(spy.plans.length).toBe(n0 + 1);
        const { from, plan } = spy.plans[spy.plans.length - 1];
        const mv = f.log.filter((e) => e.op === "move" && e.t > t0 && e.t <= t1);
        checkWalk(from, plan, mv, t1 - t0);
        expect([mv[mv.length - 1].x, mv[mv.length - 1].y]).toEqual([x, y]);
      }
    }, 60_000);

    it(`held drag at ${lat} ms: button order kept, lands on target, planned duration`, async () => {
      const f = await setup(lat, "pace-2");
      await drive(f.page.mouse.move(200, 300));
      await drive(f.page.mouse.down());
      const t0 = performance.now();
      await drive(f.page.mouse.move(560, 310));
      const t1 = performance.now();
      await drive(f.page.mouse.up());
      const ops = f.log.map((e) => e.op);
      expect(ops.filter((o) => o === "down").length).toBe(1);
      expect(ops.filter((o) => o === "up").length).toBe(1);
      const d = ops.indexOf("down"), u = ops.indexOf("up");
      const leg = f.log.slice(d + 1, u);
      expect(leg.length).toBeGreaterThan(0);
      expect(leg.every((e) => e.op === "move")).toBe(true);
      expect([leg[leg.length - 1].x, leg[leg.length - 1].y]).toEqual([560, 310]);
      const { from, plan } = spy.plans[spy.plans.length - 1];
      checkWalk(from, plan, leg, t1 - t0);
    }, 60_000);

    for (const ms of [600, 1500, 3000]) {
      it(`ambientMotion(${ms}) at ${lat} ms lasts ${ms} ms +-20 %`, async () => {
        const f = await setup(lat, "pace-3");
        const t0 = performance.now();
        await drive(f.page.ambientMotion(ms));
        const took = performance.now() - t0;
        const mv = f.log.filter((e) => e.op === "move");
        expect(mv.length).toBeGreaterThan(0);
        // a fresh page's drift comes in over the top edge, inside its budget (SC 239): the first event is at y 0-4
        expect(mv[0].y).toBeGreaterThanOrEqual(0); expect(mv[0].y).toBeLessThanOrEqual(4);
        expect(took, `ambientMotion(${ms}) took ${took.toFixed(0)} ms`).toBeGreaterThanOrEqual(0.8 * ms);
        // With 25 ms latency a late walk stretches instead of catching up with jumps (SC 210 fix 2, no hop
        // over the persona's hop cap): ambient's fast flicks run long (Python, 30 plans: worst 1.67x at 600 ms).
        expect(took, `ambientMotion(${ms}) took ${took.toFixed(0)} ms`).toBeLessThanOrEqual((lat === 0 ? 1.2 : 1.8) * ms);
        expect(hopsOk(mv)).toBe(true);
      }, 60_000);
    }
  }
});

describe("pacing.walk", () => {
  const fake = (lat: number) => {
    let t = 0;
    const sent: { x: number; y: number; t: number }[] = [];
    return {
      now: () => t,
      sleep: async (ms: number) => { t += Math.max(0, ms); },
      move: async (x: number, y: number) => { t += lat; sent.push({ x, y, t }); },
      sent, get t() { return t; },
    };
  };

  it("never drops a pinned (button-state) step or the last one", async () => {
    const steps = Array.from({ length: 200 }, (_, i) => ({ x: i, y: 0, sleepMs: 2, btn: [37, 38, 120].includes(i) }));
    const c = fake(25);
    await walk(steps, c.move, { now: c.now, sleep: c.sleep, pinned: (s) => s.btn });
    const xs = c.sent.map((s) => s.x);
    expect(xs.length).toBeLessThan(100);
    for (const k of [37, 38, 120, 199]) expect(xs).toContain(k);
    expect([...xs].sort((a, b) => a - b)).toEqual(xs);
  });

  it("on time: every sample at its planned time", async () => {
    const steps = Array.from({ length: 50 }, (_, i) => ({ x: i * 3, y: 0, sleepMs: 7 }));
    const c = fake(0);
    expect(await walk(steps, c.move, { now: c.now, sleep: c.sleep })).toBe(50);
    c.sent.forEach((s, k) => expect(s.t).toBeCloseTo(7 * k, 6));
    expect(c.t).toBeCloseTo(350, 6);
  });

  it("dispatches the in-between point instead of a long jump", async () => {
    const steps = Array.from({ length: 40 }, (_, i) => ({ x: i * 30, y: 0, sleepMs: 1 }));
    const c = fake(25);
    await walk(steps, c.move, { now: c.now, sleep: c.sleep });
    // 30 px/ms is ten times a fast hand: every hop stays within what one covers in a 25 ms dispatch.
    expect(maxGap([{ x: 0, y: 0 }, ...c.sent])).toBeLessThanOrEqual(FAST_PX_PER_MS * 25 + 1e-6);
    expect(c.sent.length).toBeGreaterThan(10);
    expect(c.sent[c.sent.length - 1].x).toBe(39 * 30);
  });

  for (const stall of [200, 350, 500]) {
    it(`after a ${stall} ms stall it stretches instead of jumping (SC 210 lab v2)`, async () => {
      // 35 hops of 60-103 px a few ms apart on r38 under load: the old cap (3 px/ms x the time since the
      // last dispatch) let a slow dispatch turn into one big catch-up hop. Now no hop is longer than the hop
      // cap (MAX_JUMP_PX without a persona), nor more than 20 px right after it: the walk takes longer.
      const steps = Array.from({ length: 300 }, (_, i) => ({ x: i * 3, y: 0, sleepMs: 2 }));
      let t = 0;
      const sent: { x: number; t: number }[] = [];
      await walk(steps, async (x) => { t += [3, 9, 15].includes(sent.length) ? stall : 25; sent.push({ x, t }); },
        { now: () => t, sleep: async (ms) => { t += Math.max(0, ms); } });
      const hops = sent.slice(1).map((s, k) => s.x - sent[k].x);
      expect(Math.max(...hops)).toBeLessThanOrEqual(MAX_JUMP_PX + 1e-6);
      for (const k of [3, 9, 15]) expect(hops[k], `the hop after stall ${k}`).toBeLessThanOrEqual(20 + 1e-6);
      expect(sent[sent.length - 1].x).toBe(299 * 3);
    });
  }

  it("pauses through a stall instead of skipping after it", async () => {
    const steps = Array.from({ length: 200 }, (_, i) => ({ x: i * 2, y: 0, sleepMs: 5 }));
    let t = 0;
    const sent: number[] = [];
    await walk(steps, async (x) => { t += sent.length === 10 ? 500 : 2; sent.push(x); },
      { now: () => t, sleep: async (ms) => { t += Math.max(0, ms); } });
    expect(sent.length).toBeGreaterThanOrEqual(185);
    expect(Math.abs(t - (1000 + 500 - STALL_MS))).toBeLessThan(30);
    expect(sent[sent.length - 1]).toBe(398);
  });

  it("a slow first sample is a stall too", async () => {
    const steps = Array.from({ length: 40 }, (_, i) => ({ x: i * 2, y: 0, sleepMs: 5 }));
    let t = 0;
    const sent: number[] = [];
    await walk(steps, async (x) => { t += sent.length ? 0 : 300; sent.push(x); },
      { now: () => t, sleep: async (ms) => { t += Math.max(0, ms); } });
    expect(sent.length).toBeGreaterThanOrEqual(40 - STALL_MS / 5 - 1);
  });

  it("stops at a failing move", async () => {
    let calls = 0;
    const c = fake(0);
    const n = await walk(Array.from({ length: 10 }, (_, i) => ({ x: i, y: 0, sleepMs: 5 })),
      async () => { if (++calls === 3) throw new Error("Target closed"); }, { now: c.now, sleep: c.sleep });
    expect(n).toBe(2);
    expect(calls).toBe(3);
  });

  it("trimTo cuts an ambient plan near the budget, dropping only a tail", () => {
    const p = makePersona("trim");
    for (const ms of [600, 1200, 1500, 4000]) {
      const plan = planAmbient({ x: 300, y: 300 }, { width: 1280, height: 800 }, p, ms);
      const cut = trimTo(plan, ms);
      const { total } = schedule(cut);
      expect(total).toBeGreaterThanOrEqual(0.85 * ms);
      expect(total).toBeLessThanOrEqual(1.1 * ms);
      expect(cut).toEqual(plan.slice(0, cut.length));
    }
  });

  it("trimTo on a short ask does not stop the pointer mid-flight", () => {
    for (let seed = 0; seed < 60; seed++) {
      const plan = planAmbient({ x: 300, y: 300 }, { width: 1280, height: 800 }, makePersona(seed), 300);
      const cut = trimTo(plan, 300);
      let peak = 0;
      for (let k = 0; k < plan.length; k++) peak = Math.max(peak, speedAt(plan, k));
      const { total } = schedule(cut);
      expect(cut).toEqual(plan.slice(0, cut.length));
      expect(total).toBeLessThanOrEqual(600 + 1e-6);
      if (total > 1.1 * 300) expect(speedAt(plan, cut.length - 1)).toBeLessThanOrEqual(0.3 * peak);
    }
  });
});

// ---------------------------------------------------------------- SC 299: interpolated points, persona hop cap
describe("pacing.walk: interpolated points and the persona hop cap (SC 299)", () => {
  type Sent = { x: number; y: number; t: number };
  /** Local maxima (above 40 % of the peak) of the 5-sample moving average of hop / dt. */
  const velocityPeaks = (sent: Sent[], smooth = 5): number => {
    let v: number[] = [];
    for (let k = 1; k < sent.length; k++) {
      const dt = sent[k].t - sent[k - 1].t;
      if (dt > 0) v.push(Math.hypot(sent[k].x - sent[k - 1].x, sent[k].y - sent[k - 1].y) / dt);
    }
    if (smooth > 1) {
      const h = Math.floor(smooth / 2);
      v = v.map((_, i) => { const w = v.slice(Math.max(0, i - h), i + h + 1); return w.reduce((a, b) => a + b, 0) / w.length; });
    }
    const top = v.length ? Math.max(...v) : 0;
    let n = 0;
    for (let i = 1; i < v.length - 1; i++) if (v[i] >= v[i - 1] && v[i] > v[i + 1] && v[i] > 0.4 * top) n++;
    return n;
  };
  /** A fake clock whose move costs lat +- spread ms (seeded), stamped when its round trip ends. */
  const jittery = (lat: number, spread: number, seed = 7) => {
    const r = mulberry32(seed);
    let t = 0;
    const sent: Sent[] = [];
    return {
      now: () => t,
      sleep: async (ms: number) => { t += Math.max(0, ms); },
      move: async (x: number, y: number) => { t += Math.max(1, lat - spread + 2 * spread * r()); sent.push({ x, y, t }); },
      sent,
    };
  };
  const median = (a: number[]) => { const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
  const hops = (sent: Sent[]) => sent.slice(1).map((s, k) => Math.hypot(s.x - sent[k].x, s.y - sent[k].y));

  /** The pre-SC 299 walk (whole planned samples only, nextIndex), on the same fake clock: the reference. */
  const legacyWalk = async (steps: { x: number; y: number; sleepMs: number }[], c: ReturnType<typeof jittery>, maxJump: number) => {
    const { due } = schedule(steps);
    let t0 = c.now(), tLast = t0, i = 0;
    let last: { x: number; y: number } | null = null;
    while (i < steps.length) {
      const el = c.now() - t0;
      if (el < due[i]) await c.sleep(due[i] - el);
      else i = nextIndex(steps, due, i, last, el, jumpCap(maxJump, c.now() - tLast));
      const tb = c.now();
      await c.move(steps[i].x, steps[i].y);
      const took = c.now() - tb;
      if (took > STALL_MS) t0 += took - STALL_MS;
      last = steps[i]; tLast = tb; i++;
    }
  };

  it("sends the point on the path when late and keeps its speed", async () => {
    // SC 298 on Windows: an ~18 ms round trip against ~7 ms planned samples. Skipping to whole samples hopped
    // 2 or 3 at a time (a sawtooth of 7-8 velocity peaks per glide). Sending the path's point at the elapsed
    // time keeps one smooth speed profile. The fake stamps each point when its round trip ends, so the jitter
    // itself adds some wobble. Python's version (random.Random latencies, unseeded plans) holds a median of
    // <= 3.5 smoothed peaks over 20 personas; the stream here is seeded (plans AND latencies: the same lists
    // come out of Python's walk for these inputs), median 3.0-4.0 across plan streams, so the band is <= 4, with
    // fewer peaks in all than the old walk makes on the very same plans and round trips.
    for (const planSalt of [0, 100, 200]) {
      const pk: number[] = [], old: number[] = [];
      for (let seed = 0; seed < 20; seed++) {
        const plan = planMove({ x: 120, y: 140 }, { x: 1020, y: 480 }, makePersona(String(seed)), { targetW: 40, rng: mulberry32(planSalt + seed) });
        const c = jittery(18, 6, seed + 50);
        await walk(plan, c.move, { now: c.now, sleep: c.sleep, maxJump: 200 });
        expect([c.sent[c.sent.length - 1].x, c.sent[c.sent.length - 1].y]).toEqual([plan[plan.length - 1].x, plan[plan.length - 1].y]);
        pk.push(velocityPeaks(c.sent));
        const c2 = jittery(18, 6, seed + 50);
        await legacyWalk(plan, c2, 200);
        old.push(velocityPeaks(c2.sent));
      }
      expect(median(pk), `${pk} (old walk ${old})`).toBeLessThanOrEqual(4);
      expect(Math.max(...pk), String(pk)).toBeLessThanOrEqual(8);
      const sum = (a: number[]) => a.reduce((x, y) => x + y, 0);
      expect(sum(pk), `${pk} (old walk ${old})`).toBeLessThan(sum(old));
    }
  });

  it("a late point lies on the planned path between the two samples around it", async () => {
    const steps = Array.from({ length: 60 }, (_, i) => ({ x: i * 5, y: (i % 2) * 3, sleepMs: 7 }));
    const c = jittery(18, 0);
    await walk(steps, c.move, { now: c.now, sleep: c.sleep });
    let between = 0;
    for (const s of c.sent) {
      const k = Math.floor(s.x / 5);
      if (s.x === k * 5) continue;   // a planned sample itself
      between++;
      const a = steps[k], b = steps[k + 1];
      const f = (s.x - a.x) / (b.x - a.x);
      expect(s.y).toBeCloseTo(a.y + (b.y - a.y) * f, 9);
    }
    expect(between, "points between planned samples").toBeGreaterThan(5);
    expect(c.sent[c.sent.length - 1].x).toBe(59 * 5);
  });

  it("planned glide speed follows its report times", async () => {
    // SC 299: a planned sample's position was taken at the nominal i/n while its report interval jittered
    // +-12 %, so the speed a page computes swung sample to sample (5 smoothed velocity peaks per planned glide,
    // SC 298). Walked on time, a single-submovement plan now shows one smooth speed bell. (Seeded plans here, so
    // the run is the same every time; Python's version draws them unseeded.)
    const pk: number[] = [];
    for (let seed = 0; seed < 30; seed++) {
      const plan = planMove({ x: 120, y: 140 }, { x: 1020, y: 480 }, makePersona(String(seed)), { targetW: 40, rng: mulberry32(seed) });
      const c = jittery(0, 0);
      await walk(plan, c.move, { now: c.now, sleep: c.sleep, maxJump: 200 });
      pk.push(velocityPeaks(c.sent));
    }
    expect(median(pk), String(pk)).toBeLessThanOrEqual(1.5);
    expect(Math.max(...pk), String(pk)).toBeLessThanOrEqual(3);
  });

  it("hops beyond 40 px when the plan is that fast", async () => {
    // The old fixed 40 px cap was a wall every persona shared (SC 298: 0 of 4,462 page steps above 40.5 px).
    // A fast reach at ~3 px/ms with an 18-25 ms round trip covers 55-75 px between two dispatches.
    const p = makePersona("3");
    const cap = hopCapPx(p);
    const plan = planMove({ x: 60, y: 100 }, { x: 1900, y: 900 }, p, { targetW: 60, rng: mulberry32(3) });
    const c = jittery(22, 3);
    await walk(plan, c.move, { now: c.now, sleep: c.sleep, maxJump: cap });
    const h = hops(c.sent);
    expect(Math.max(...h)).toBeGreaterThan(45);
    expect(Math.max(...h)).toBeLessThanOrEqual(cap + 1e-6);
  });

  it("the hop cap is a persona trait", () => {
    const caps = Array.from({ length: 200 }, (_, s) => hopCapPx(makePersona(String(s))));
    expect(caps.every((c) => c >= 55 && c <= 110)).toBe(true);
    expect(Math.max(...caps) - Math.min(...caps)).toBeGreaterThan(40);
    expect(hopCapPx(makePersona("same"))).toBe(hopCapPx(makePersona("same")));
  });

  it("never sends a point behind one already sent", async () => {
    // Interpolated points move forward along the plan: the plan time of every dispatched point grows.
    const steps = Array.from({ length: 160 }, (_, i) => ({ x: i * 4, y: (i % 7) * 1.5, sleepMs: 6.5 }));
    const c = jittery(20, 8, 3);
    await walk(steps, c.move, { now: c.now, sleep: c.sleep });
    const xs = c.sent.map((s) => s.x);
    expect([...xs].sort((a, b) => a - b)).toEqual(xs);
    expect(xs[xs.length - 1]).toBe(steps[steps.length - 1].x);
  });
});

describe("keepInView (SC 210 fix 2)", () => {
  it("moves samples inside but never the target", async () => {
    const { keepInView } = await import("../src/pacing.js");
    const steps = [{ x: 10, y: 5, sleepMs: 8 }, { x: -12, y: -3, sleepMs: 8 }, { x: 1290, y: 400, sleepMs: 8 }, { x: 1279, y: 799, sleepMs: 8 }];
    expect(keepInView(steps, 1280, 800).map((s) => [s.x, s.y])).toEqual([[10, 5], [2, 2], [1279, 400], [1279, 799]]);
    const aimedOut = [{ x: 10, y: 5, sleepMs: 8 }, { x: 1300, y: 900, sleepMs: 8 }];
    expect(keepInView(aimedOut, 1280, 800)).toBe(aimedOut);
  });
});
