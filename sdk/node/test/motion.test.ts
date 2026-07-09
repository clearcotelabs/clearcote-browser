import { describe, it, expect } from "vitest";
import {
  makePersona, planMove, dragDwell, clickPoint, planAmbient, mulberry32, minJerk,
} from "../src/motion.js";
import { scoreMotion, stepsToSamples, extractFeatures } from "../src/motionscore.js";

// A faithful reproduction of the OLD single-bezier glide (smoothstep easing, uniform rand(7,20) dt,
// flat white-gauss jitter) so we can score old-vs-new on the same detector-emulator.
function oldGlide(from: { x: number; y: number }, to: { x: number; y: number }, rng: () => number) {
  const gauss = () => { let u = 0, v = 0; while (!u) u = rng(); while (!v) v = rng(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); };
  const x0 = from.x, y0 = from.y, dx = to.x - from.x, dy = to.y - from.y;
  const dist = Math.hypot(dx, dy);
  const steps = Math.floor(Math.max(10, Math.min(38, dist / 14)));
  const nx = -dy / dist, ny = dx / dist;
  const bow = (rng() * 0.22 - 0.11) * dist;
  const c1x = x0 + dx * 0.33 + nx * bow, c1y = y0 + dy * 0.33 + ny * bow;
  const c2x = x0 + dx * 0.66 + nx * bow, c2y = y0 + dy * 0.66 + ny * bow;
  const out: { x: number; y: number; sleepMs: number }[] = [];
  for (let i = 1; i <= steps; i++) {
    const t = i / steps, e = t * t * (3 - 2 * t), mt = 1 - e;
    const bx = mt * mt * mt * x0 + 3 * mt * mt * e * c1x + 3 * mt * e * e * c2x + e * e * e * to.x;
    const by = mt * mt * mt * y0 + 3 * mt * mt * e * c1y + 3 * mt * e * e * c2y + e * e * e * to.y;
    out.push({ x: bx + gauss() * 0.6, y: by + gauss() * 0.6, sleepMs: 7 + rng() * 13 });
  }
  out.push({ x: to.x, y: to.y, sleepMs: 12 });
  return out;
}

describe("persona", () => {
  it("is deterministic per seed and varies across seeds", () => {
    const a = makePersona("id-alpha"), b = makePersona("id-alpha"), c = makePersona("id-beta");
    expect(a).toEqual(b);
    expect(a.seed).not.toEqual(c.seed);
    expect(a.tremorHz).toBeGreaterThanOrEqual(8);
    expect(a.tremorHz).toBeLessThanOrEqual(12);
    expect(a.deviceHz).toBeGreaterThan(100);
  });
});

describe("minJerk", () => {
  it("is monotone 0->1 and peaks in speed near the middle", () => {
    expect(minJerk(0)).toBeCloseTo(0);
    expect(minJerk(1)).toBeCloseTo(1);
    // discrete speed of min-jerk should peak near tau=0.5
    let peak = 0, peakTau = 0;
    for (let i = 1; i <= 100; i++) { const v = minJerk(i / 100) - minJerk((i - 1) / 100); if (v > peak) { peak = v; peakTau = i / 100; } }
    expect(peakTau).toBeGreaterThan(0.4);
    expect(peakTau).toBeLessThan(0.6);
  });
});

describe("planMove vs old glide (detector-emulator)", () => {
  it("scores strictly more human than the old single-bezier glide, averaged over runs", () => {
    const p = makePersona("scorer-seed");
    let newTot = 0, oldTot = 0;
    const N = 40;
    for (let k = 0; k < N; k++) {
      const rngNew = mulberry32(1000 + k), rngOld = mulberry32(1000 + k);
      const from = { x: 200, y: 300 }, to = { x: 720, y: 470 };
      const nnew = scoreMotion(stepsToSamples(planMove(from, to, p, { targetW: 30, rng: rngNew })));
      const nold = scoreMotion(stepsToSamples(oldGlide(from, to, rngOld)));
      newTot += nnew.score; oldTot += nold.score;
    }
    const newAvg = newTot / N, oldAvg = oldTot / N;
    expect(newAvg).toBeGreaterThan(oldAvg);
    expect(newAvg).toBeGreaterThan(0.75);
  });

  it("fixes the specific tells: >=1 submovement, non-uniform dt, colored noise, right-skewed velocity", () => {
    const p = makePersona("tells");
    // average features over runs (single runs are noisy)
    let sub = 0, skew = 0, ac = 0, uniform = 0, straight = 0; const R = 30;
    for (let k = 0; k < R; k++) {
      const f = extractFeatures(stepsToSamples(planMove({ x: 150, y: 150 }, { x: 780, y: 520 }, p, { targetW: 28, rng: mulberry32(k) })));
      sub += f.submovements; skew += f.velSkew; ac += f.jitterAutocorr;
      uniform += f.dtUniform ? 1 : 0; straight += f.straightness;
    }
    expect(sub / R).toBeGreaterThanOrEqual(1);   // has corrective submovements
    expect(sub / R).toBeLessThan(3.5);           // but not an implausible number of them
    expect(skew / R).toBeGreaterThan(0.1);       // right-skewed (homing tail), not a symmetric bell
    expect(ac / R).toBeGreaterThan(0.1);         // colored jitter, not white
    expect(uniform).toBe(0);                      // never looks i.i.d.-uniform
    expect(straight / R).toBeLessThan(0.999);
  });

  it("duration follows Fitts (small target => longer than a big target at equal distance)", () => {
    const p = makePersona("fitts");
    const dur = (w: number) => {
      let tot = 0; const R = 20;
      for (let k = 0; k < R; k++) { const s = stepsToSamples(planMove({ x: 100, y: 100 }, { x: 600, y: 100 }, p, { targetW: w, rng: mulberry32(k) })); tot += s[s.length - 1].t; }
      return tot / R;
    };
    expect(dur(8)).toBeGreaterThan(dur(120));
  });
});

describe("drag endpoints", () => {
  it("dragDwell produces human grab/release windows", () => {
    const p = makePersona("drag");
    for (let k = 0; k < 50; k++) {
      const d = dragDwell(p, mulberry32(k));
      expect(d.grabMs).toBeGreaterThanOrEqual(p.grabMinMs);
      expect(d.grabMs).toBeLessThanOrEqual(p.grabMaxMs);
      expect(d.releaseMs).toBeGreaterThanOrEqual(p.releaseMinMs);
    }
  });

  it("a full drag sample stream passes the drag scorer (grab+release not too short)", () => {
    const p = makePersona("drag2");
    const rng = mulberry32(3);
    const handle = { x: 300, y: 402 }, target = { x: 560, y: 402 };
    const approach = planMove({ x: 100, y: 400 }, handle, p, { rng });   // button UP
    const { grabMs, releaseMs } = dragDwell(p, rng);
    const drag = planMove(handle, target, p, { settle: true, rng });     // button HELD
    drag[drag.length - 1].sleepMs = releaseMs;                            // pre-release settle dwell
    // sequence: approach(up) → down@handle(dwell grabMs) → held drag → up@target
    const down = { x: handle.x, y: handle.y, sleepMs: grabMs };
    const up = { x: target.x, y: target.y, sleepMs: 0 };
    const steps = [...approach, down, ...drag, up];
    const start = approach.length, end = approach.length + 1 + drag.length; // held = down + drag (not up)
    const res = scoreMotion(stepsToSamples(steps, { start, end }), { drag: true });
    expect(res.flags.some((f) => f.startsWith("grab-hesitation-too-short"))).toBe(false);
    expect(res.flags.some((f) => f.startsWith("release-delay-too-short"))).toBe(false);
    expect(res.features.grabMs).toBeGreaterThan(60);
    expect(res.features.releaseMs).toBeGreaterThan(40);
  });
});

describe("clickPoint", () => {
  it("stays inside the box and biases toward the approach side", () => {
    const p = makePersona("cp");
    const box = { x: 100, y: 100, width: 200, height: 60 };
    for (let k = 0; k < 100; k++) {
      const pt = clickPoint(box, { x: 0, y: 130 }, p, mulberry32(k));
      expect(pt.x).toBeGreaterThanOrEqual(box.x + 2);
      expect(pt.x).toBeLessThanOrEqual(box.x + box.width - 2);
      expect(pt.y).toBeGreaterThanOrEqual(box.y + 2);
    }
  });
});

describe("planAmbient", () => {
  it("produces non-trivial pre-challenge entropy within the viewport", () => {
    const p = makePersona("amb");
    const steps = planAmbient({ x: 400, y: 300 }, { width: 1280, height: 800 }, p, 1500, mulberry32(1));
    const total = steps.reduce((a, s) => a + s.sleepMs, 0);
    expect(total).toBeGreaterThan(1200);
    for (const s of steps) { expect(s.x).toBeGreaterThan(0); expect(s.x).toBeLessThan(1280); }
    // distinct positions (real entropy, not a frozen cursor)
    const distinct = new Set(steps.map((s) => `${Math.round(s.x)},${Math.round(s.y)}`));
    expect(distinct.size).toBeGreaterThan(20);
  });
});
