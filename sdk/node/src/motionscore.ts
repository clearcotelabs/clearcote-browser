// Detector-emulator — extracts the feature vector a behavioral collector (DataDome/PerimeterX-class)
// would compute from a pointer stream and scores how human it looks. This is a VALIDATION tool: it
// lets us prove, offline and deterministically, that the motion core (motion.ts) improves the features
// that matter, instead of eyeballing trajectories or burning scarce clean-IP end-to-end runs.
//
// It is intentionally an APPROXIMATION of what real collectors do (their exact models are private);
// treat the score as a relative signal (old vs new), not an absolute pass/fail.

export interface Sample { x: number; y: number; t: number; buttons?: number; }

export interface MotionFeatures {
  n: number;
  durationMs: number;
  /** Fraction of the movement at which peak speed occurs. Human ≈ 0.3–0.45 (fast ballistic rise, slow
   * homing). A single analytic ease curve is SYMMETRIC (peak ≈ 0.5); pure ease-out peaks ≈ 0.15. */
  peakVelFrac: number;
  /** Time-weighted skewness of the speed profile. Human > 0 (long decelerating homing tail); a
   * symmetric single-curve motion ≈ 0. */
  velSkew: number;
  /** Corrective submovements = prominent interior speed minima (human 1–3; a single curve = 0). */
  submovements: number;
  /** Net displacement / path length (1 = perfectly straight). */
  straightness: number;
  /** Diagnostic only (NOT scored): residual of the whole speed profile vs a SINGLE min-jerk bell.
   * A real multi-submovement reach does not fit one bell, so this is high for human motion too. */
  minJerkResidual: number;
  /** Inter-sample dt mean / std (ms) and a uniformity flag (i.i.d.-uniform dt is a tell). */
  dtMeanMs: number;
  dtStdMs: number;
  dtUniform: boolean;
  /** Lag-1 autocorrelation of the sub-pixel jitter residual. White noise ≈ 0; colored motor noise
   * (1/f drift + 8–12 Hz tremor) ≈ 0.2–0.8. */
  jitterAutocorr: number;
  /** 2/3 power-law fit quality (diagnostic only — not generated): correlation of log speed vs
   * −1/3·log curvature. ~0 for straight paths (undefined curvature); informative on curved ones. */
  powerLawR: number;
  /** Drag endpoint timing (only when a held-button segment is present), else null. */
  grabMs: number | null;
  releaseMs: number | null;
}

function speeds(s: Sample[]): { v: number[]; tMid: number[] } {
  const v: number[] = [], tMid: number[] = [];
  for (let i = 1; i < s.length; i++) {
    const dt = Math.max(1e-3, s[i].t - s[i - 1].t);
    v.push(Math.hypot(s[i].x - s[i - 1].x, s[i].y - s[i - 1].y) / dt);
    tMid.push((s[i].t + s[i - 1].t) / 2);
  }
  return { v, tMid };
}

/** Smooth a series with a small moving average (window w). */
function smooth(a: number[], w = 3): number[] {
  const out: number[] = [];
  for (let i = 0; i < a.length; i++) {
    let s = 0, c = 0;
    for (let j = Math.max(0, i - w); j <= Math.min(a.length - 1, i + w); j++) { s += a[j]; c++; }
    out.push(s / c);
  }
  return out;
}

function mean(a: number[]): number { return a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0; }
function std(a: number[]): number {
  if (a.length < 2) return 0;
  const m = mean(a);
  return Math.sqrt(mean(a.map((x) => (x - m) ** 2)));
}

export function extractFeatures(s: Sample[]): MotionFeatures {
  const n = s.length;
  const durationMs = n > 1 ? s[n - 1].t - s[0].t : 0;
  const { v, tMid } = speeds(s);
  const sv = smooth(v, 2);

  // peak velocity position
  let peakIdx = 0;
  for (let i = 1; i < sv.length; i++) if (sv[i] > sv[peakIdx]) peakIdx = i;
  const peakVelFrac = durationMs > 0 ? (tMid[peakIdx] - s[0].t) / durationMs : 0;

  // velocity skewness (positive for a human's fast-rise/slow-decay profile; ~0 for a symmetric curve)
  const vmean = mean(sv), vstd = std(sv) || 1e-9;
  const velSkew = mean(sv.map((x) => ((x - vmean) / vstd) ** 3));

  // corrective submovements: prominent interior local minima (a real dip that recovers), not jitter.
  // Require the min to sit below 0.5·vmax and be a true valley between two higher shoulders, with a
  // minimum index separation so a single correction isn't counted several times.
  const vmax = Math.max(...sv, 1e-9);
  let submovements = 0, lastMin = -10;
  for (let i = 2; i < sv.length - 2; i++) {
    const valley = sv[i] < sv[i - 1] && sv[i] <= sv[i + 1] && sv[i] < sv[i - 2] && sv[i] < sv[i + 2];
    if (valley && sv[i] < 0.5 * vmax && i - lastMin >= 3) { submovements++; lastMin = i; }
  }

  // straightness
  let pathLen = 0;
  for (let i = 1; i < n; i++) pathLen += Math.hypot(s[i].x - s[i - 1].x, s[i].y - s[i - 1].y);
  const netDisp = n > 1 ? Math.hypot(s[n - 1].x - s[0].x, s[n - 1].y - s[0].y) : 0;
  const straightness = pathLen > 1e-6 ? netDisp / pathLen : 1;

  // min-jerk residual: compare the normalized speed profile to 30τ²(1−τ)²
  let minJerkResidual = 1;
  if (sv.length >= 4 && durationMs > 0) {
    const area = sv.reduce((acc, val, i) => acc + val * (i === 0 ? 0 : tMid[i] - tMid[i - 1]), 0) || 1;
    let res = 0, norm = 0;
    for (let i = 0; i < sv.length; i++) {
      const tau = (tMid[i] - s[0].t) / durationMs;
      const ideal = 30 * tau * tau * (1 - tau) * (1 - tau); // ∫ over τ = 1
      const obs = (sv[i] * durationMs) / area; // normalize so ∫ obs dτ ≈ 1
      res += (obs - ideal) ** 2; norm += ideal ** 2;
    }
    minJerkResidual = norm > 0 ? Math.sqrt(res / norm) : 1;
  }

  // dt stats + uniformity (uniform-i.i.d. dt has std/mean near that of U(a,b) and low autocorrelation)
  const dts: number[] = [];
  for (let i = 1; i < n; i++) dts.push(s[i].t - s[i - 1].t);
  const dtMeanMs = mean(dts), dtStdMs = std(dts);
  // A real device stream has a sharp cadence MODE (most samples near the median) with a heavy tail of
  // pauses; a rand(a,b) band has NO mode (roughly flat), so few samples cluster near the median.
  // Measure the fraction within ±20% of the median: high ⇒ has a mode; low ⇒ looks uniform/flat.
  const sorted = [...dts].sort((a, b) => a - b);
  const median = sorted.length ? sorted[Math.floor(sorted.length / 2)] : 0;
  const coreFrac = median > 0 ? dts.filter((d) => d >= 0.8 * median && d <= 1.2 * median).length / dts.length : 0;
  const dtUniform = coreFrac < 0.5;

  // jitter color: lag-1 autocorrelation of the sub-pixel residual (position minus a smoothed trend).
  // White gauss ≈ 0; colored motor noise (slow drift + band-limited tremor) ≈ 0.2–0.8.
  let jitterAutocorr = 0;
  if (n >= 8) {
    const xs = s.map((p) => p.x), ys = s.map((p) => p.y);
    const sx = smooth(xs, 3), sy = smooth(ys, 3);
    const rx = xs.map((x, i) => x - sx[i]), ry = ys.map((y, i) => y - sy[i]);
    const ac = (r: number[]) => {
      let num = 0, den = 0;
      for (let i = 1; i < r.length; i++) num += r[i] * r[i - 1];
      for (let i = 0; i < r.length; i++) den += r[i] * r[i];
      return den > 1e-9 ? num / den : 0;
    };
    jitterAutocorr = (ac(rx) + ac(ry)) / 2;
  }

  // 2/3 power law (diagnostic): correlation between log speed and −(1/3) log curvature
  let powerLawR = 0;
  if (n >= 6) {
    const lv: number[] = [], lc: number[] = [];
    for (let i = 1; i < n - 1; i++) {
      const ax = s[i].x - s[i - 1].x, ay = s[i].y - s[i - 1].y;
      const bx = s[i + 1].x - s[i].x, by = s[i + 1].y - s[i].y;
      const cross = Math.abs(ax * by - ay * bx);
      const segA = Math.hypot(ax, ay), segB = Math.hypot(bx, by);
      const curv = cross / (Math.pow(segA * segB * Math.hypot(bx + ax, by + ay), 1) + 1e-9);
      const sp = (segA + segB) / 2 / Math.max(1e-3, (s[i + 1].t - s[i - 1].t) / 2);
      if (curv > 1e-6 && sp > 1e-6) { lv.push(Math.log(sp)); lc.push(Math.log(curv)); }
    }
    if (lv.length >= 4) {
      const mv = mean(lv), mc = mean(lc);
      let num = 0, dv = 0, dc = 0;
      for (let i = 0; i < lv.length; i++) { num += (lv[i] - mv) * (lc[i] - mc); dv += (lv[i] - mv) ** 2; dc += (lc[i] - mc) ** 2; }
      powerLawR = dv > 0 && dc > 0 ? num / Math.sqrt(dv * dc) : 0;
    }
  }

  // drag endpoint timing: within the buttons==1 (held) segment. The `up` is the first sample AFTER
  // the held span (buttons back to 0); grab = down→first-movement, release = last-movement→up.
  let grabMs: number | null = null, releaseMs: number | null = null;
  const heldIdx: number[] = [];
  for (let i = 0; i < n; i++) if ((s[i].buttons ?? 0) & 1) heldIdx.push(i);
  if (heldIdx.length) {
    const d0 = heldIdx[0];
    let firstMoveT = s[d0].t;
    for (const i of heldIdx) {
      if (Math.hypot(s[i].x - s[d0].x, s[i].y - s[d0].y) > 1) { firstMoveT = s[i].t; break; }
    }
    grabMs = firstMoveT - s[d0].t;
    let lastMoveIdx = d0;
    for (let k = 1; k < heldIdx.length; k++) {
      const i = heldIdx[k], j = heldIdx[k - 1];
      if (Math.hypot(s[i].x - s[j].x, s[i].y - s[j].y) > 1) lastMoveIdx = i;
    }
    const lastHeld = heldIdx[heldIdx.length - 1];
    const upT = lastHeld + 1 < n ? s[lastHeld + 1].t : s[lastHeld].t;
    releaseMs = upT - s[lastMoveIdx].t;
  }

  return {
    n, durationMs, peakVelFrac, velSkew, submovements, straightness, minJerkResidual,
    dtMeanMs, dtStdMs, dtUniform, jitterAutocorr, powerLawR, grabMs, releaseMs,
  };
}

export interface MotionScore { score: number; flags: string[]; features: MotionFeatures; }

/** Score 0..1 (higher = more human) with human-readable flags for the tells that fire. */
export function scoreMotion(s: Sample[], opts: { drag?: boolean } = {}): MotionScore {
  const f = extractFeatures(s);
  const flags: string[] = [];
  let score = 1;
  const penalize = (cond: boolean, amt: number, flag: string) => { if (cond) { score -= amt; flags.push(flag); } };

  // peak velocity should be asymmetric-human: penalize pure ease-out (very early) AND a too-symmetric
  // single-curve peak sitting right at the midpoint.
  penalize(f.peakVelFrac < 0.15, 0.15, `peak-velocity-ease-out(${f.peakVelFrac.toFixed(2)})`);
  penalize(f.peakVelFrac > 0.48, 0.12, `peak-velocity-too-symmetric(${f.peakVelFrac.toFixed(2)})`);
  // velocity profile should have the human right-skew (long homing tail), not a symmetric bell
  penalize(f.velSkew < 0.1, 0.12, `velocity-symmetric(skew=${f.velSkew.toFixed(2)})`);
  // at least one corrective submovement (Fitts homing); a single analytic curve has none
  penalize(f.submovements < 1, 0.2, "no-corrective-submovement");
  // …but not TOO many: an over-corrected, velocity-reversal-heavy reach (many interior speed minima)
  // is as non-human as a single smooth curve — real homing settles in 1–3 corrections.
  penalize(f.submovements > 4, 0.12, `too-many-submovements(${f.submovements})`);
  // dt should NOT look i.i.d.-uniform (real device sampling has a cadence mode)
  penalize(f.dtUniform, 0.15, "uniform-dt-cadence");
  // colored (not white) sub-pixel noise
  penalize(f.jitterAutocorr < 0.1, 0.1, `white-noise-jitter(${f.jitterAutocorr.toFixed(2)})`);
  // path shouldn't be a ruler-straight line
  penalize(f.straightness > 0.9995, 0.08, "perfectly-straight-path");

  if (opts.drag) {
    penalize(f.grabMs != null && f.grabMs < 60, 0.2, `grab-hesitation-too-short(${f.grabMs}ms)`);
    penalize(f.releaseMs != null && f.releaseMs < 40, 0.2, `release-delay-too-short(${f.releaseMs}ms)`);
  }

  return { score: Math.max(0, score), flags, features: f };
}

/** Turn a Step list (absolute coords + post-step sleep) into timestamped Samples for scoring.
 * `buttonsDuring` marks the held-button (drag) span [startIdx, endIdx) with buttons=1. */
export function stepsToSamples(
  steps: { x: number; y: number; sleepMs: number }[],
  buttonsDuring?: { start: number; end: number }
): Sample[] {
  const out: Sample[] = [];
  let t = 0;
  for (let i = 0; i < steps.length; i++) {
    const held = buttonsDuring && i >= buttonsDuring.start && i < buttonsDuring.end ? 1 : 0;
    out.push({ x: steps[i].x, y: steps[i].y, t, buttons: held });
    t += steps[i].sleepMs;
  }
  return out;
}
