// Human motion core — the trajectory/timing model shared by every humanized input path.
//
// PURE (no Playwright, no I/O): given a start point, a target, and a seeded `Persona`, it returns a
// list of `Step`s (absolute cursor coordinates + a post-step sleep). The wrapper (humanize.ts)
// walks the steps with the engine's NATIVE mouse.move + an off-protocol sleep, so the whole path is
// trusted input. Keeping the math here (a) unifies Node/Python so they can't drift, (b) makes it
// unit-testable offline, and (c) lets motionscore.ts score generated paths without a browser.
//
// Model — why these choices (mapped to what behavioral collectors actually measure):
//   • Minimum-jerk submovements. A human reach is a primary ballistic submovement plus 1–2 corrective
//     ones (Meyer's optimized-submovement model), each following the minimum-jerk profile
//     s(τ)=10τ³−15τ⁴+6τ⁵ (speed 30τ²(1−τ)², peaking at τ=0.5). Composing them yields the mid-path
//     velocity peak, the decelerating Fitts homing phase, and natural overshoot→correction reversals —
//     none of which a single ease curve produces. (A single bezier peaks at the wrong place and has
//     exactly one submovement — a structural tell.)
//   • Fitts's law duration. Movement time = a + b·log2(D/W+1), not ∝ distance, so duration is
//     statistically human across short-big vs long-small targets (a distribution detectors check).
//   • Colored noise. Sub-pixel jitter = slow Ornstein–Uhlenbeck drift + a band-limited 8–12 Hz
//     physiological tremor, not flat white gauss (whose flat spectrum is itself detectable).
//   • Seeded persona. One motor signature (mean speed, tremor, overshoot, handedness bias, cadence)
//     is drawn per identity from the fingerprint seed, so behavior is consistent WITHIN a session and
//     unlinkable across seeds — the same coherence model as the rest of the persona.
//   • Endpoint dwells. Grab hesitation after mouse.down and a pre-release settle before mouse.up (the
//     two worst slider tells: real ~130–360 ms grab / ~90–230 ms release vs a scripted few ms).
//
// NOTE on the 2/3 power law: it governs continuous CURVED drawing, not discrete point-to-point
// reaches, so imposing it on a reach would corrupt the min-jerk profile. We deliberately do NOT apply
// it during generation; motionscore.ts reports power-law residual only as a diagnostic.

/** A deterministic 0..1 RNG (mulberry32). Same seed ⇒ same stream, so a persona/trajectory is
 * reproducible in tests; production paths pass a fresh unseeded RNG for live micro-variation. */
export type Rng = () => number;

export function mulberry32(seedInt: number): Rng {
  let a = seedInt >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** FNV-1a hash of an arbitrary seed (string or number) to a 32-bit int for the persona RNG. */
export function hashSeed(seed: string | number | undefined): number {
  if (seed === undefined || seed === null) return (Math.random() * 0xffffffff) >>> 0;
  if (typeof seed === "number" && Number.isFinite(seed)) return (Math.abs(seed) * 2654435761) >>> 0;
  const s = String(seed);
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** Standard-normal sample (Box–Muller) from a given RNG. */
export function gaussFrom(rng: Rng): number {
  let u = 0, v = 0;
  while (u === 0) u = rng();
  while (v === 0) v = rng();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

/** The per-identity motor signature. All ranges are chosen to sit inside measured human envelopes. */
export interface Persona {
  seed: number;
  /** Cursor sampling cadence (Hz) → step spacing ≈ 1000/deviceHz ms (real mice: 125–1000 Hz). */
  deviceHz: number;
  /** Physiological tremor frequency (Hz), band 8–12. */
  tremorHz: number;
  /** Tremor amplitude (px). */
  tremorAmp: number;
  /** OU drift amplitude (px) — slow low-frequency wander. */
  driftAmp: number;
  /** OU drift correlation time (ms). */
  driftTau: number;
  /** Base per-sample gaussian jitter (px). */
  jitter: number;
  /** Fitts intercept a (ms) and slope b (ms/bit). */
  fittsA: number;
  fittsB: number;
  /** Fraction of the gap the primary ballistic submovement covers (0.84–0.94). */
  primaryFrac: number;
  /** Overshoot scale as a fraction of distance for the primary landing. */
  overshoot: number;
  /** Max corrective submovements after the primary (1–2, occasionally 3). */
  maxCorrections: number;
  /** Handedness/approach curvature bias (signed, radians-ish) applied to path bow. */
  approachBias: number;
  /** Grab hesitation window after mouse.down (ms). */
  grabMinMs: number;
  grabMaxMs: number;
  /** Pre-release settle window before mouse.up (ms). */
  releaseMinMs: number;
  releaseMaxMs: number;
  /** Click press-hold window — mousedown→mouseup (ms). Human ~60–150; a scripted click is ~2 ms. */
  clickHoldMinMs: number;
  clickHoldMaxMs: number;
  /** Per-key dwell window — keydown→keyup (ms). Human ~45–120 (right-skewed); scripted typing ~2–3 ms. */
  keyDwellMinMs: number;
  keyDwellMaxMs: number;
}

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

/** Derive a stable motor persona from a fingerprint seed (or random when unseeded). */
export function makePersona(seed?: string | number): Persona {
  const seedInt = hashSeed(seed);
  const r = mulberry32(seedInt);
  return {
    seed: seedInt,
    deviceHz: Math.round(lerp(110, 155, r())),
    tremorHz: lerp(8, 12, r()),
    tremorAmp: lerp(0.12, 0.5, r()),
    driftAmp: lerp(0.1, 0.4, r()),
    driftTau: lerp(60, 160, r()),
    jitter: lerp(0.25, 0.7, r()),
    fittsA: lerp(90, 150, r()),
    fittsB: lerp(120, 190, r()),
    primaryFrac: lerp(0.84, 0.94, r()),
    overshoot: lerp(0.03, 0.08, r()),
    maxCorrections: r() < 0.15 ? 3 : r() < 0.6 ? 2 : 1,
    approachBias: (r() - 0.5) * 0.5,
    grabMinMs: 130,
    grabMaxMs: 360,
    releaseMinMs: 90,
    releaseMaxMs: 230,
    clickHoldMinMs: 60,
    clickHoldMaxMs: 150,
    keyDwellMinMs: 45,
    keyDwellMaxMs: 120,
  };
}

/** A human mousedown→mouseup click hold (ms), right-skewed (most clicks brief, some linger). Fixes
 * the ~2 ms instant-press tell shared by clicks. */
export function clickHold(p: Persona, rng: Rng = Math.random): number {
  return p.clickHoldMinMs + Math.min(1, Math.abs(gaussFrom(rng)) * 0.5) * (p.clickHoldMaxMs - p.clickHoldMinMs);
}

/** A human per-key keydown→keyup dwell (ms), right-skewed (a floor + occasional longer holds — the
 * empirical human dwell shape). Fixes the ~2–3 ms instant-keystroke tell. */
export function keyDwell(p: Persona, rng: Rng = Math.random): number {
  return p.keyDwellMinMs + Math.min(1, Math.abs(gaussFrom(rng)) * 0.45) * (p.keyDwellMaxMs - p.keyDwellMinMs);
}

export interface Point { x: number; y: number; }
/** One dispatched sample: move the cursor to (x,y), then sleep `sleepMs` before the next. */
export interface Step { x: number; y: number; sleepMs: number; }

export interface PlanOpts {
  /** Target width/height (px) for the Fitts duration + click-point spread. Defaults ~24. */
  targetW?: number;
  targetH?: number;
  /** Append a tiny 1–2 px settling jiggle at the end (used for slider/drag seating). */
  settle?: boolean;
  /** RNG for live micro-variation. Defaults to Math.random (non-reproducible). Pass a seeded Rng in tests. */
  rng?: Rng;
}

const log2 = (x: number) => Math.log(x) / Math.LN2;
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** Cubic bezier point at parameter e (0..1). */
function bez(p0: number, c1: number, c2: number, p1: number, e: number): number {
  const m = 1 - e;
  return m * m * m * p0 + 3 * m * m * e * c1 + 3 * m * e * e * c2 + e * e * e * p1;
}

/** Minimum-jerk easing s(τ)=10τ³−15τ⁴+6τ⁵ (position fraction; speed 30τ²(1−τ)²). */
export function minJerk(tau: number): number {
  const t = clamp(tau, 0, 1);
  return t * t * t * (10 + t * (-15 + 6 * t));
}

/** Sample ONE min-jerk submovement a→b along a slightly bowed path, adding colored noise. Appends to
 * `out`. `t0` is the absolute clock (ms) at the start (for tremor phase continuity). Returns the
 * absolute clock at the end. `land` = put the final sample exactly on b with no noise. */
function sampleSubmove(
  out: Step[], a: Point, b: Point, durMs: number, p: Persona, rng: Rng,
  t0: number, drift: { x: number; y: number }, jit: { x: number; y: number }, tremorPhase: number, land: boolean
): number {
  const dx = b.x - a.x, dy = b.y - a.y;
  const dist = Math.hypot(dx, dy) || 1e-6;
  const stepMs = 1000 / p.deviceHz;
  // Cap the sample count so a very long move can't emit thousands of steps (defends the Fitts cap).
  const n = Math.min(180, Math.max(4, Math.round(durMs / stepMs)));
  // perpendicular unit for the path bow; sign follows the persona's handedness bias
  const nx = -dy / dist, ny = dx / dist;
  const bowSign = p.approachBias >= 0 ? 1 : -1;
  const bow = bowSign * (0.04 + Math.abs(gaussFrom(rng)) * 0.05) * Math.min(dist, 260) * (0.5 + Math.abs(p.approachBias));
  const c1x = a.x + dx * 0.33 + nx * bow, c1y = a.y + dy * 0.33 + ny * bow;
  const c2x = a.x + dx * 0.66 + nx * bow, c2y = a.y + dy * 0.66 + ny * bow;
  let t = t0;
  for (let i = 1; i <= n; i++) {
    const e = minJerk(i / n);
    let px = bez(a.x, c1x, c2x, b.x, e);
    let py = bez(a.y, c1y, c2y, b.y, e);
    const last = i === n;
    if (!(last && land)) {
      // Ornstein–Uhlenbeck drift (colored, low-frequency)
      const k = stepMs / p.driftTau;
      drift.x += -k * drift.x + Math.sqrt(2 * k) * p.driftAmp * gaussFrom(rng);
      drift.y += -k * drift.y + Math.sqrt(2 * k) * p.driftAmp * gaussFrom(rng);
      // 8–12 Hz physiological tremor: absolute-clock phase (continuous across submovements), a slow
      // amplitude "breathing", and a slightly DETUNED/phase-offset y axis — real tremor is not a
      // fixed-amplitude tone locked in exact 90° quadrature.
      const tAmp = p.tremorAmp * (0.7 + 0.3 * Math.sin((2 * Math.PI * t) / 850 + tremorPhase));
      const trX = tAmp * Math.sin(tremorPhase + (2 * Math.PI * p.tremorHz * t) / 1000);
      const trY = tAmp * Math.sin(tremorPhase + 1.0 + (2 * Math.PI * (p.tremorHz * 0.93) * t) / 1000);
      // Sub-pixel jitter as a COLORED AR(1) process, not i.i.d. white noise — white noise adds a fresh
      // speed reversal every sample, manufacturing spurious "submovements" and a flat noise spectrum.
      jit.x = 0.7 * jit.x + gaussFrom(rng) * p.jitter * 0.22;
      jit.y = 0.7 * jit.y + gaussFrom(rng) * p.jitter * 0.22;
      px += drift.x + trX + jit.x;
      py += drift.y + trY + jit.y;
    } else {
      px = b.x; py = b.y;
    }
    // device sampling is quite regular (a sharp cadence mode); keep per-sample jitter small so the
    // dt distribution has a mode, unlike a rand(a,b) band. Pauses come from the inter-submovement gap.
    const sleepMs = stepMs * lerp(0.88, 1.12, rng());
    out.push({ x: px, y: py, sleepMs });
    t += sleepMs;
  }
  return t;
}

/**
 * Plan a free cursor move `from`→`to` as a sum of minimum-jerk submovements (primary + corrections)
 * with Fitts-scaled duration and colored noise. Returns the samples to dispatch.
 */
export function planMove(from: Point, to: Point, p: Persona, opts: PlanOpts = {}): Step[] {
  const rng = opts.rng ?? Math.random;
  const W = Math.max(6, opts.targetW ?? 24);
  const D = Math.hypot(to.x - from.x, to.y - from.y);
  const out: Step[] = [];
  if (D < 1.5) { out.push({ x: to.x, y: to.y, sleepMs: 1000 / p.deviceHz }); return out; }

  // Fitts movement time (ms) + a little log-normal-ish noise; short hops get a small floor. The floor
  // is itself capped at the 1700 ms ceiling so a very long move can't invert the clamp (lo > hi) and
  // silently blow past the cap.
  const id = log2(D / W + 1);
  const mtLo = Math.min(1700, Math.max(70, 40 + D * 0.6));
  const mt = clamp((p.fittsA + p.fittsB * id) * (0.85 + 0.3 * rng()), mtLo, 1700);

  // Submovement targets: primary lands near `to` with a small over/undershoot; corrections close the
  // residual error, each covering ~55–80% of what's left.
  const targets: Point[] = [];
  if (D >= 40) {
    const f = p.primaryFrac;
    const spread = Math.min(16, D * p.overshoot);
    targets.push({
      x: from.x + (to.x - from.x) * f + gaussFrom(rng) * spread,
      y: from.y + (to.y - from.y) * f + gaussFrom(rng) * spread,
    });
    // 1 correction usually, occasionally 2 (a 3rd is rare) — keeps the corrective-submovement count
    // in the human 1–3 band rather than over-generating homing bumps.
    const nCorr = rng() < 0.08 * p.maxCorrections ? 2 : 1;
    let cur = targets[0];
    for (let i = 0; i < nCorr; i++) {
      const close = lerp(0.55, 0.8, rng());
      const nx = { x: cur.x + (to.x - cur.x) * close, y: cur.y + (to.y - cur.y) * close };
      // tiny lateral error so corrections aren't perfectly collinear
      nx.x += gaussFrom(rng) * 1.2; nx.y += gaussFrom(rng) * 1.2;
      targets.push(nx); cur = nx;
    }
  }
  targets.push({ x: to.x, y: to.y });

  // Allocate MT: primary ~62%, remaining split across corrections (each shorter).
  const segDur: number[] = [];
  if (targets.length === 1) {
    segDur.push(mt);
  } else {
    // Primary gets ~78% of the time (min-jerk peaks at its own mid-point ⇒ global peak lands ~0.35–0.4,
    // the human sweet spot slightly before the whole-gesture midpoint), corrections share the rest.
    segDur.push(mt * 0.78);
    const rest = mt * 0.22;
    const nc = targets.length - 1;
    for (let i = 0; i < nc; i++) segDur.push((rest / nc) * lerp(0.8, 1.2, rng()));
  }

  const drift = { x: 0, y: 0 };
  const jit = { x: 0, y: 0 };
  const tremorPhase = rng() * 2 * Math.PI;
  let a = from, t = 0;
  for (let s = 0; s < targets.length; s++) {
    const b = targets[s];
    const last = s === targets.length - 1;
    t = sampleSubmove(out, a, b, Math.max(20, segDur[s]), p, rng, t, drift, jit, tremorPhase, last && !opts.settle);
    if (!last && out.length) out[out.length - 1].sleepMs += lerp(40, 120, rng()); // motor re-planning gap
    a = b;
  }

  if (opts.settle) {
    // A couple of tiny sub-pixel seating jiggles, then land exactly (slider handle "clunk" into place).
    const jig = 1 + Math.floor(rng() * 2);
    for (let i = 0; i < jig; i++) {
      out.push({ x: to.x + gaussFrom(rng) * 1.4, y: to.y + gaussFrom(rng) * 1.4, sleepMs: lerp(30, 90, rng()) });
    }
    out.push({ x: to.x, y: to.y, sleepMs: 1000 / p.deviceHz });
  }
  return out;
}

/** Grab/release dwell times (ms) for a held-button drag, drawn from the persona window. */
export function dragDwell(p: Persona, rng: Rng = Math.random): { grabMs: number; releaseMs: number } {
  return {
    grabMs: lerp(p.grabMinMs, p.grabMaxMs, rng()),
    releaseMs: lerp(p.releaseMinMs, p.releaseMaxMs, rng()),
  };
}

export interface Box { x: number; y: number; width: number; height: number; }

/** A human click point inside `box`: a 2D gaussian around center, nudged toward the approach side
 * (people undershoot toward where the cursor came from), clamped a couple px inside the edges. */
export function clickPoint(box: Box, from: Point, p: Persona, rng: Rng = Math.random): Point {
  const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
  const dx = cx - from.x, dy = cy - from.y;
  const d = Math.hypot(dx, dy) || 1;
  // undershoot bias: pull ~12% of the half-width back toward the approach direction
  const ux = -dx / d, uy = -dy / d;
  const bx = ux * box.width * 0.12, by = uy * box.height * 0.12;
  const x = cx + bx + gaussFrom(rng) * box.width * 0.16;
  const y = cy + by + gaussFrom(rng) * box.height * 0.16;
  // For a box narrower/shorter than the 4 px inset the clamp bounds would invert (lo > hi) and return
  // a point OUTSIDE the element — just use the center for such tiny targets.
  return {
    x: box.width >= 4 ? clamp(x, box.x + 2, box.x + box.width - 2) : cx,
    y: box.height >= 4 ? clamp(y, box.y + 2, box.y + box.height - 2) : cy,
  };
}

export interface Viewport { width: number; height: number; }

/**
 * Plan ambient / pre-challenge cursor activity: idle drift + a few non-goal moves within the viewport,
 * so a behavioral collector sees non-zero pointer entropy BEFORE the first goal action (the single
 * biggest reason a challenge/slider is shown to headless automation). Returns ~`ms` of samples.
 */
export function planAmbient(from: Point, vp: Viewport, p: Persona, ms: number, rng: Rng = Math.random): Step[] {
  const out: Step[] = [];
  let cur = from;
  let elapsed = 0;
  const budget = Math.max(200, ms);
  // keep motion inside a comfortable central band (people don't fling the cursor to the extremes)
  const bx0 = vp.width * 0.12, bx1 = vp.width * 0.88;
  const by0 = vp.height * 0.14, by1 = vp.height * 0.8;
  while (elapsed < budget && out.length < 4000) {
    const target = { x: lerp(bx0, bx1, rng()), y: lerp(by0, by1, rng()) };
    const seg = planMove(cur, target, p, { targetW: 80, rng });
    for (const s of seg) { out.push(s); elapsed += s.sleepMs; }
    // rest with a little drift between excursions (hand idling on the mouse)
    const restMs = lerp(120, 600, rng());
    const drift = { x: 0, y: 0 };
    const stepMs = 1000 / p.deviceHz;
    let held = 0;
    const tphase = rng() * 2 * Math.PI;
    while (held < restMs) {
      const k = stepMs / p.driftTau;
      drift.x += -k * drift.x + Math.sqrt(2 * k) * p.driftAmp * gaussFrom(rng);
      drift.y += -k * drift.y + Math.sqrt(2 * k) * p.driftAmp * gaussFrom(rng);
      const ph = tphase + (2 * Math.PI * p.tremorHz * held) / 1000;
      out.push({
        x: target.x + drift.x + p.tremorAmp * Math.sin(ph),
        y: target.y + drift.y + p.tremorAmp * Math.sin(ph + Math.PI / 2),
        sleepMs: stepMs * lerp(0.9, 1.4, rng()),
      });
      held += stepMs; elapsed += stepMs;
    }
    cur = target;
  }
  return out;
}
