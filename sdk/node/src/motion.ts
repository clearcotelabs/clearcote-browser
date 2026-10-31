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
  /** The HUMAN BANDS every timing draw is clamped to (ms); the per-identity medians inside them come from
   * the salted timing stream ({@link timingTraits}). Grab hesitation after mouse.down: */
  grabMinMs: number;
  grabMaxMs: number;
  /** Pre-release settle before mouse.up (ms). */
  releaseMinMs: number;
  releaseMaxMs: number;
  /** Click press-hold band — mousedown→mouseup (ms), 55–230; a scripted click is ~2 ms. */
  clickHoldMinMs: number;
  clickHoldMaxMs: number;
  /** Per-key dwell band — keydown→keyup (ms), 45–120; scripted typing ~2–3 ms. */
  keyDwellMinMs: number;
  keyDwellMaxMs: number;
}

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const clampV = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

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
    clickHoldMinMs: 55,
    clickHoldMaxMs: 230,
    keyDwellMinMs: 45,
    keyDwellMaxMs: 120,
  };
}

// Salt for the timing stream. Like the rollover and shape salts, a separate mulberry32(seed ^ salt) stream
// keeps makePersona's draw ORDER (and so every existing seed's motor identity) unchanged.
const TIMING_SALT = 0xc2b2ae35;

/** Per-identity timing medians (ms) and tempo, stable per seed (see timingTraits). */
export interface TimingTraits {
  /** Median mousedown→mouseup hold, 70–140. */
  clickHoldMs: number;
  /** Median keydown→keyup dwell, 60–95. */
  keyDwellMs: number;
  /** Median grab hesitation after a drag's mousedown, 180–280. */
  grabMs: number;
  /** Median settle before a drag's mouseup, 120–180. */
  releaseMs: number;
  /** Median gap between the two presses of a double click, 90–140. */
  dblclickGapMs: number;
  /** Scale on deliberation pauses (select popup, datetime segments), 0.8–1.25. */
  pauseScale: number;
  /** Per-character fat-finger rate, 0.005–0.04. */
  typoRate: number;
}

/** The persona's timing traits, drawn from a salted stream of its seed. Before this every identity shared
 * ONE fixed window per timing (click hold 60–150, key dwell 45–120, grab 130–360, release 90–230,
 * double-click gap 70–170, select-popup pauses, datetime gaps, the 2 % typo rate). Each value is the
 * identity's MEDIAN; samples around it are log-normal and clamped to the persona's human band. Same values
 * as Python `_motion.timing_traits` and .NET `Motion.TimingTraitsOf`. */
export function timingTraits(p: Persona): TimingTraits {
  const r = mulberry32((p.seed ^ TIMING_SALT) >>> 0);
  const clickHoldMs = lerp(70, 140, r());
  const keyDwellMs = lerp(60, 95, r());
  const grabMs = lerp(180, 280, r());
  const releaseMs = lerp(120, 180, r());
  const dblclickGapMs = lerp(90, 140, r());
  const pauseScale = lerp(0.8, 1.25, r());
  const typoRate = lerp(0.005, 0.04, r());
  return { clickHoldMs, keyDwellMs, grabMs, releaseMs, dblclickGapMs, pauseScale, typoRate };
}

/** `median * exp(sigma * N(0,1))` clamped to [lo, hi]: a right-skewed human timing around a per-identity median. */
export function lognormalMs(median: number, sigma: number, lo: number, hi: number, rng: Rng): number {
  return clampV(median * Math.exp(sigma * gaussFrom(rng)), lo, hi);
}

/** The preset's tempo on `p` (HumanizeOptions: careful 1.4 / default 1 / quick 0.7, set by applyToPersona after
 * makePersona; 1 on a bare persona, so every vector is unchanged). */
export function tempoOf(p: Persona | null | undefined): number {
  const t = (p as any)?.tempo;
  return typeof t === "number" && t > 0 ? t : 1;
}

/** A human mousedown→mouseup click hold (ms): log-normal (σ 0.25) around the persona's own median
 * (70–140 ms), clamped to its band (55–230 ms). */
export function clickHold(p: Persona, rng: Rng = Math.random): number {
  return lognormalMs(timingTraits(p).clickHoldMs * tempoOf(p), 0.25, p.clickHoldMinMs, p.clickHoldMaxMs, rng);
}

/** A human per-key keydown→keyup dwell (ms): log-normal (σ 0.22) around the persona's own median
 * (60–95 ms), clamped to its band (45–120 ms). */
export function keyDwell(p: Persona, rng: Rng = Math.random): number {
  return lognormalMs(timingTraits(p).keyDwellMs * tempoOf(p), 0.22, p.keyDwellMinMs, p.keyDwellMaxMs, rng);
}

/** The gap between the two presses of a double click (ms): log-normal (σ 0.15) around the persona's
 * median (90–140), clamped to 70–170. */
export function dblclickGap(p: Persona, rng: Rng = Math.random): number {
  const t = tempoOf(p);
  return lognormalMs(timingTraits(p).dblclickGapMs * t, 0.15, 70 * t, 170 * t, rng);
}

/** A deliberation pause (ms) from [lo, hi], scaled by the persona's tempo (pauseScale 0.8–1.25).
 * Without a persona: the plain window. */
export function pauseMs(p: Persona | null | undefined, lo: number, hi: number, rng: Rng = Math.random): number {
  const v = lerp(lo, hi, rng());
  return p ? v * timingTraits(p).pauseScale * tempoOf(p) : v;
}

// Salt for the rollover stream. Drawing from mulberry32(seed ^ salt) instead of adding a field to
// makePersona keeps the persona draw ORDER (and so every existing seed's motor identity) unchanged.
const ROLLOVER_SALT = 0x9e3779b9;

/** Share of eligible key pairs this persona types with ROLLOVER: the next key goes down before the
 * previous one comes up, the way people type fast common pairs. Stable per seed, 0.12–0.38.
 *
 * Measured on r32/r33 before this existed: 0 overlapping keydowns in 30–32 typed keys, because every
 * character was one press() (down, dwell, up) before the next began. Same value as Python
 * `_motion.rollover_rate` and .NET `Motion.RolloverRate`. */
export function rolloverRate(p: Persona): number {
  return lerp(0.12, 0.38, mulberry32((p.seed ^ ROLLOVER_SALT) >>> 0)());
}

/** The platforms a wheel notch size is known for (the keys of WHEEL_NOTCH_PX). */
export type WheelPlatform = "windows" | "linux" | "macos" | "android";

/** Pixels one wheel notch scrolls, by the platform the persona claims. A wheel event sent over CDP
 * always carries wheelDelta ±120 (input_handler.cc sets wheel_ticks to ±1 whatever the delta), and a
 * real notch is the only event whose distance goes with a 120 wheelDelta:
 *   windows 100 = 3 lines × 100/3 px (web_input_event_builders_win.cc, default 3 lines per notch)
 *   linux   120 = ui::MouseWheelEvent::kWheelDelta (event.cc); measured 120/−120 per X11 notch
 *   macos    40 = kScrollbarPixelsPerCocoaTick (cocoa_event_utils.h)
 * android is not measured; it gets the windows value. */
export const WHEEL_NOTCH_PX: Readonly<Record<WheelPlatform, number>> = { windows: 100, linux: 120, macos: 40, android: 100 };

/** Whole notches for a requested scroll distance: the nearest count, at least one when the caller
 * asked for any scroll at all. A wheel cannot move 39 px; it moves notches. floor(x + 0.5), not
 * Math.round, so the count is the same expression as Python's and .NET's (half rounds up). */
export function wheelNotches(delta: number, notchPx: number): number {
  if (!delta) return 0;
  return Math.max(1, Math.floor(Math.abs(delta) / notchPx + 0.5));
}

/** Map navigator.userAgentData.platform / navigator.platform to a WHEEL_NOTCH_PX key (null = unknown). */
export function platformFromNavigator(value: string | null | undefined): WheelPlatform | null {
  const v = (value || "").toLowerCase();
  if (!v) return null;
  if (v.includes("android")) return "android"; // before linux: Android's navigator.platform is "Linux armv8l"
  if (v.includes("win")) return "windows";
  if (v.includes("mac")) return "macos";
  if (v.includes("linux") || v.includes("x11") || v.includes("cros")) return "linux";
  return null;
}

export interface Point { x: number; y: number; }
/** One dispatched sample: move the cursor to (x,y), then sleep `sleepMs` before the next. */
export interface Step { x: number; y: number; sleepMs: number; }

export interface PlanOpts {
  /** Target width/height (px) for the Fitts duration + click-point spread. Defaults ~24. */
  targetW?: number;
  targetH?: number;
  /** A drag that seats a handle: planned by planDrag, ending in a couple of sub-pixel seating jiggles. */
  settle?: boolean;
  /** The button is held (a drag leg): planned by planDrag, landing exactly on `to`. */
  held?: boolean;
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

// Salt for the path-shape stream. Like the rollover salt, a separate mulberry32(seed ^ salt) stream
// keeps makePersona's draw ORDER (and so every existing seed's motor identity) unchanged.
const SHAPE_SALT = 0x85ebca6b;

/** Per-identity path-shape traits, stable per seed (see shapeTraits). */
export interface ShapeTraits {
  /** Share of aimed moves that pass the target and come back, 0.15–0.38. */
  overshootRate: number;
  /** Scale on the primary submovement's bow, 0.75–1.3. */
  curve: number;
  /** Scale on held-drag duration, 0.85–1.2. */
  dragTempo: number;
}

/** The persona's path-shape traits, drawn from a salted stream of its seed.
 *
 * Measured on r38 / SDK 0.48.0 (SC 209): 1 of 36 planned approaches passed the target, against 15–40 %
 * for real reaches and 14/36 for the engine's recorded trajectories, because the primary submovement
 * always aimed at 84–94 % of the distance. Same values as Python `_motion.shape_traits` and .NET
 * `Motion.ShapeTraitsOf`. */
export function shapeTraits(p: Persona): ShapeTraits {
  const r = mulberry32((p.seed ^ SHAPE_SALT) >>> 0);
  const overshootRate = lerp(0.15, 0.38, r());
  const curve = lerp(0.75, 1.3, r());
  const dragTempo = lerp(0.85, 1.2, r());
  return { overshootRate, curve, dragTempo };
}

// Sub-pixel noise scale. The colored noise (OU drift + 8–12 Hz tremor + AR(1) jitter) stays, but smaller
// and carried mostly while the hand is moving: a mouse resting on the pad reports no counts, so a slow
// tail full of ±0.5 px wobble is what a quantizing page turns into 1 px zigzags.
const NOISE = 0.6;

/** floor(x + 0.5): the same rounding as Python `_js_round` and .NET `JsRound`. */
const jsRound = (x: number) => Math.floor(x + 0.5);

/** Time warp 1−(1−τ)^skew: skew > 1 moves the min-jerk speed peak before the midpoint. */
function skewed(tau: number, skew: number): number {
  const t = clamp(tau, 0, 1);
  return skew !== 1 ? 1 - Math.pow(1 - t, skew) : t;
}

/** Sample ONE min-jerk submovement a→b along a bowed path, adding colored noise. Appends to `out`.
 * `t0` is the absolute clock (ms) at the start (for tremor phase continuity). Returns the absolute
 * clock at the end. `land` = put the final sample exactly on b with no noise. `bowFrac` = signed peak
 * sideways deviation as a share of the submovement's length; `skew` = time warp (1 = symmetric). */
function sampleSubmove(
  out: Step[], a: Point, b: Point, durMs: number, p: Persona, rng: Rng,
  t0: number, drift: { x: number; y: number }, jit: { x: number; y: number }, tremorPhase: number, land: boolean,
  bowFrac: number, skew: number
): number {
  const dx = b.x - a.x, dy = b.y - a.y;
  const dist = Math.hypot(dx, dy) || 1e-6;
  const stepMs = 1000 / p.deviceHz;
  // Cap the sample count so a very long move can't emit thousands of steps (defends the Fitts cap).
  const n = Math.min(180, Math.max(4, jsRound(durMs / stepMs)));
  const nx = -dy / dist, ny = dx / dist;
  // A cubic with both control points offset by c peaks at 0.75 c; the first control point carries more
  // of it, so the widest point of the arc comes before the middle, as in a reach.
  const c = bowFrac * dist / 0.75;
  const c1x = a.x + dx * 0.3 + nx * c * 1.15, c1y = a.y + dy * 0.3 + ny * c * 1.15;
  const c2x = a.x + dx * 0.7 + nx * c * 0.85, c2y = a.y + dy * 0.7 + ny * c * 0.85;
  let t = t0;
  // Each sample's POSITION is taken at its own report time (SC 299): the report intervals jitter
  // (stepMs x 0.88-1.12, drawn at the end of each iteration as before), and a position sampled at the
  // nominal i/n while its time jittered made the speed a page computes swing +-12 % sample to sample --
  // 5 smoothed velocity peaks per planned glide (SC 298). `el` is the time of sample i since the
  // submovement started; the hand has arrived once el reaches the nominal duration.
  const total = n * stepMs;
  let el = stepMs;
  for (let i = 1; i <= n; i++) {
    const tau = i === n ? 1 : Math.min(1, el / total);
    const e = minJerk(skewed(tau, skew));
    let px = bez(a.x, c1x, c2x, b.x, e);
    let py = bez(a.y, c1y, c2y, b.y, e);
    const last = i === n;
    if (!(last && land)) {
      // noise follows the speed envelope (min-jerk speed 30τ²(1−τ)² peaks at 1.875)
      const v = 30 * tau * tau * (1 - tau) * (1 - tau) / 1.875;
      const g = NOISE * (0.35 + 0.65 * v);
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
      px += (drift.x + trX + jit.x) * g;
      py += (drift.y + trY + jit.y) * g;
    } else {
      px = b.x; py = b.y;
    }
    // device sampling is quite regular (a sharp cadence mode); keep per-sample jitter small so the
    // dt distribution has a mode, unlike a rand(a,b) band. Pauses come from the inter-submovement gap.
    const sleepMs = stepMs * lerp(0.88, 1.12, rng());
    out.push({ x: px, y: py, sleepMs });
    t += sleepMs;
    el += sleepMs;
  }
  return t;
}

/**
 * Plan a free cursor move `from`→`to` as a sum of minimum-jerk submovements (primary + corrections)
 * with Fitts-scaled duration and colored noise. Returns the samples to dispatch.
 *
 * A move with a button held (`held`) or one that seats a handle (`settle`) is a drag, planned by
 * {@link planDrag}.
 *
 * Shape (SC 209/210): the primary submovement arcs (peak sideways deviation ~6–18 % of the distance,
 * per-persona curvature and handedness), reaches its speed peak before the middle of the move, and
 * either stops short of the target (a short corrective submovement closes the gap) or, in
 * `shapeTraits(p).overshootRate` of moves, passes it by 3–28 px and comes back. Straightness (distance /
 * path length) of the planned points lands around 0.85–0.95, the band of real reaches.
 */
export function planMove(from: Point, to: Point, p: Persona, opts: PlanOpts = {}): Step[] {
  if (opts.settle || opts.held) return planDrag(from, to, p, { settle: opts.settle, rng: opts.rng });
  const rng = opts.rng ?? Math.random;
  const W = Math.max(6, opts.targetW ?? 24);
  const D = Math.hypot(to.x - from.x, to.y - from.y);
  const out: Step[] = [];
  if (D < 1.5) { out.push({ x: to.x, y: to.y, sleepMs: 1000 / p.deviceHz }); return out; }

  // Fitts movement time (ms) + a little log-normal-ish noise; short hops get a small floor. The floor
  // (an average-speed bound) is itself capped at the 1700 ms ceiling so a very long move can't invert the
  // clamp (lo > hi) and silently blow past the cap. 0.4 ms/px (was 0.6, SC 299): at 0.6 the floor overrode
  // Fitts' law whenever a far target was also wide, so planned time tracked DISTANCE instead of difficulty --
  // on SC 298's six targets (ID 2.5-4.6) the planned MT/ID correlation was -0.14 median over 8 personas;
  // 0.58 at 0.4, with the fastest far reach (3300 px) still under 8.3 px/ms at its peak.
  const id = log2(D / W + 1);
  const mtLo = Math.min(1700, Math.max(70, 40 + D * 0.4));
  const mt = clamp((p.fittsA + p.fittsB * id) * (0.85 + 0.3 * rng()), mtLo, 1700);
  const tr = shapeTraits(p);
  const ux = (to.x - from.x) / D, uy = (to.y - from.y) / D;

  // Submovement targets: the primary stops a little short of `to` or, in the persona's overshoot rate,
  // passes it; one correction closes the residual (occasionally an intermediate one first).
  const targets: Point[] = [];
  let over = false;
  if (D >= 40) {
    over = rng() < tr.overshootRate;
    const along = over
      ? D + clamp(D * lerp(0.03, 0.08, rng()), 3, 28)
      : D * clamp(p.primaryFrac + 0.05 + gaussFrom(rng) * 0.015, 0.88, 0.985);
    const lat = gaussFrom(rng) * Math.min(6, D * 0.02);
    targets.push({ x: from.x + ux * along - uy * lat, y: from.y + uy * along + ux * lat });
    if (rng() < 0.08 * p.maxCorrections) {
      const cur = targets[0];
      const close = lerp(0.55, 0.8, rng());
      const mx = cur.x + (to.x - cur.x) * close + gaussFrom(rng) * 1.0;
      const my = cur.y + (to.y - cur.y) * close + gaussFrom(rng) * 1.0;
      targets.push({ x: mx, y: my });
    }
  }
  targets.push({ x: to.x, y: to.y });

  const segDur: number[] = [];
  if (targets.length === 1) {
    segDur.push(mt);
  } else {
    // Primary gets ~78% of the time; with its skewed min-jerk profile the global speed peak lands
    // ~0.3 of the way through the move, before the midpoint as in a reach. Corrections share the rest.
    segDur.push(mt * 0.78);
    const rest = mt * 0.22;
    const nc = targets.length - 1;
    for (let i = 0; i < nc; i++) segDur.push((rest / nc) * lerp(0.8, 1.2, rng()));
  }

  // The primary's arc: handedness sign from the persona (1 move in 5 bends the other way), size from
  // the persona's curvature trait. Corrections are short and nearly straight.
  let sign = p.approachBias >= 0 ? 1 : -1;
  if (rng() < 0.2) sign = -sign;
  let bow = sign * Math.min(0.25, (0.08 + Math.abs(gaussFrom(rng)) * 0.11) * tr.curve * (0.75 + Math.abs(p.approachBias)));
  if (D * Math.abs(bow) > 110) bow = bow < 0 ? -110 / D : 110 / D;

  const drift = { x: 0, y: 0 };
  const jit = { x: 0, y: 0 };
  const tremorPhase = rng() * 2 * Math.PI;
  let a = from, t = 0;
  for (let s = 0; s < targets.length; s++) {
    const b = targets[s];
    const last = s === targets.length - 1;
    const first = s === 0;
    t = sampleSubmove(out, a, b, Math.max(20, segDur[s]), p, rng, t, drift, jit, tremorPhase, last,
      first ? bow : bow * 0.15, first ? 1.25 : 1.0);
    // motor re-planning gap; a reversal after passing the target comes back quicker
    if (!last && out.length) out[out.length - 1].sleepMs += over && first ? lerp(25, 90, rng()) : lerp(40, 120, rng());
    a = b;
  }
  return out;
}

/**
 * Plan a held-button drag `from`→`to` (slider handle, drag-and-drop, range thumb).
 *
 * Measured on r38 / SDK 0.48.0 (SC 209): the held leg reused the aimed-move planner, whose arc put the
 * hand up to 47 px off the line between grab and drop — off a 40 px slider track. A drag is a slower,
 * flatter movement: one skewed min-jerk leg along the line with at most ~5 px of arc and ±6 px of total
 * sideways error, 0.5–2 s for 300–400 px (`shapeTraits(p).dragTempo`), and now and then (about 0.6× the
 * persona's overshoot rate) a 2.5–10 px overshoot and pull-back. `settle` adds a couple of sub-pixel
 * seating jiggles before landing exactly on `to`.
 */
export function planDrag(from: Point, to: Point, p: Persona, opts: { settle?: boolean; rng?: Rng } = {}): Step[] {
  const rng = opts.rng ?? Math.random;
  const settle = !!opts.settle;
  const D = Math.hypot(to.x - from.x, to.y - from.y);
  const out: Step[] = [];
  const stepMs = 1000 / p.deviceHz;
  if (D < 1.5) { out.push({ x: to.x, y: to.y, sleepMs: stepMs }); return out; }
  const tr = shapeTraits(p);
  const ux = (to.x - from.x) / D, uy = (to.y - from.y) / D;
  const mt = clamp((260 + 1.7 * D) * tr.dragTempo * lerp(0.9, 1.1, rng()), Math.min(500, 150 + 1.5 * D), 2000);
  const over = D >= 60 && rng() < tr.overshootRate * 0.6;
  const o = over ? clamp(D * lerp(0.01, 0.03, rng()), 2.5, 10) : 0;
  const sign = p.approachBias >= 0 ? 1 : -1;
  const h = sign * Math.min(5, D * 0.015) * lerp(0.3, 1.0, rng());
  const tremorPhase = rng() * 2 * Math.PI;

  const legs: Array<[number, number, number, number]> = [[0, D + o, mt * (over ? 0.86 : 1.0), h]];
  if (over) legs.push([D + o, D, mt * 0.14, 0]);
  let drift = 0, jit = 0, t = 0;
  for (let li = 0; li < legs.length; li++) {
    const [s0, s1, dur, hh] = legs[li];
    const n = Math.min(360, Math.max(4, jsRound(dur / stepMs)));
    const dt = dur / n;
    const lastLeg = li === legs.length - 1;
    for (let i = 1; i <= n; i++) {
      const tau = i / n;
      const e = minJerk(skewed(tau, 1.2));
      let along = s0 + (s1 - s0) * e;
      const v = 30 * tau * tau * (1 - tau) * (1 - tau) / 1.875;
      const k = dt / p.driftTau;
      drift += -k * drift + Math.sqrt(2 * k) * p.driftAmp * 0.8 * gaussFrom(rng);
      jit = 0.7 * jit + gaussFrom(rng) * p.jitter * 0.12;
      const tAmp = p.tremorAmp * (0.7 + 0.3 * Math.sin((2 * Math.PI * t) / 850 + tremorPhase));
      const tremor = tAmp * Math.sin(tremorPhase + (2 * Math.PI * p.tremorHz * t) / 1000);
      along += tAmp * 0.6 * Math.sin(tremorPhase + 1.0 + (2 * Math.PI * (p.tremorHz * 0.93) * t) / 1000) * v;
      let side = hh * Math.sin(Math.PI * e) + (drift + tremor + jit) * (0.6 + 0.4 * v);
      side = clamp(side, -6, 6);
      let px: number, py: number;
      if (lastLeg && i === n && !settle) { px = to.x; py = to.y; }
      else { px = from.x + ux * along - uy * side; py = from.y + uy * along + ux * side; }
      const sl = dt * lerp(0.88, 1.12, rng());
      out.push({ x: px, y: py, sleepMs: sl });
      t += sl;
    }
    if (!lastLeg && out.length) out[out.length - 1].sleepMs += lerp(30, 80, rng()); // notices, stops, pulls back
  }

  if (settle) {
    // A couple of tiny sub-pixel seating jiggles, then land exactly (slider handle "clunk" into place).
    const jig = 1 + Math.floor(rng() * 2);
    for (let i = 0; i < jig; i++) {
      const jx = to.x + clamp(gaussFrom(rng) * 0.8, -1.5, 1.5);
      const jy = to.y + clamp(gaussFrom(rng) * 0.8, -1.5, 1.5);
      out.push({ x: jx, y: jy, sleepMs: lerp(30, 90, rng()) });
    }
    out.push({ x: to.x, y: to.y, sleepMs: stepMs });
  }
  return out;
}

// Salt for the drag-start hesitation traits (Python _HESITATION_SALT, .NET HesitationSalt): a salted
// stream keeps makePersona's draw ORDER, and planDrag itself is untouched.
const HESITATION_SALT = 0xfd7046c5; // own stream (was the timing salt: hesitation moved with the click hold)
/** The drag-start hesitation window (ms): a hand that has just closed on a handle holds 100-300 ms. */
export const HESITATION_MS: readonly [number, number] = [100, 300];
/** ... creeping a few px, never this far, before the leg proper. */
export const HESITATION_MAX_PX = 8;

/** The still-but-not-frozen moment between the grab and the drag leg from->to: samples spanning
 * 100-300 ms (centred per persona; `durMs`, 40-300, overrides the length but the persona's draw is still
 * made), creeping < 8 px toward `to` with a little sideways wobble. Same numbers as Python
 * `_motion.drag_hesitation` and .NET `Motion.DragHesitation` (sdk/testdata/humanize_scroll_vectors.json).
 * Measured on r38 / SDK 0.51.0 (SC 232): every drag's first held move was already the full-speed leg. */
export function dragHesitation(from: Point, to: Point, p: Persona, rng: Rng = Math.random, durMs?: number | null): Step[] {
  const r = mulberry32((p.seed ^ HESITATION_SALT) >>> 0);
  const centre = lerp(130, 260, r());
  const amp = lerp(2, 6, r());
  const wobble = lerp(0.4, 1.2, r());
  const d = Math.hypot(to.x - from.x, to.y - from.y);
  const [ux, uy] = d >= 1e-6 ? [(to.x - from.x) / d, (to.y - from.y) / d] : [1, 0];
  let dur = clamp(centre * lerp(0.8, 1.2, rng()), HESITATION_MS[0], HESITATION_MS[1]);
  if (durMs !== undefined && durMs !== null) dur = clamp(durMs, 40, HESITATION_MS[1]);
  const reach = amp * lerp(0.6, 1.0, rng());
  const stepMs = 1000 / p.deviceHz;
  const n = Math.max(3, jsRound(dur / stepMs));
  const dt = dur / n;
  const phase = rng() * 2 * Math.PI;
  const out: Step[] = [];
  let side = 0;
  for (let i = 1; i <= n; i++) {
    const tau = i / n;
    const along = reach * minJerk(tau);
    side = 0.8 * side + gaussFrom(rng) * wobble * 0.25;
    const s = clamp(side + wobble * 0.5 * Math.sin(phase + 2 * Math.PI * tau), -1.5, 1.5);
    let px = from.x + ux * along - uy * s, py = from.y + uy * along + ux * s;
    if (Math.hypot(px - from.x, py - from.y) >= HESITATION_MAX_PX - 0.5) {
      px = from.x + ux * (HESITATION_MAX_PX - 1); py = from.y + uy * (HESITATION_MAX_PX - 1);
    }
    out.push({ x: px, y: py, sleepMs: dt * lerp(0.9, 1.1, rng()) });
  }
  return out;
}

/** When (ms from the start of `steps`) the walk first leaves the `radius` circle around `from`. */
export function legExitMs(from: Point, steps: Step[], radius = HESITATION_MAX_PX): number {
  let t = 0;
  for (const s of steps) {
    if (Math.hypot(s.x - from.x, s.y - from.y) >= radius) return t;
    t += s.sleepMs;
  }
  return t;
}

/** The held part of a drag: the hesitation creep, then the planDrag leg, planned together so the pointer
 * stays inside 8 px of the press for `windowMs` (clamped 100-300: the persona's grab dwell) and then goes.
 * Same as Python `hesitate_then_drag`. */
export function hesitateThenDrag(from: Point, to: Point, p: Persona, windowMs: number, settle = true,
  rng: Rng = Math.random): { steps: Step[]; creep: number } {
  const w = clamp(windowMs, HESITATION_MS[0], HESITATION_MS[1]);
  let creep = dragHesitation(from, to, p, rng, w);
  let leg = planDrag(creep[creep.length - 1], to, p, { settle, rng });
  const t8 = legExitMs(from, leg);
  const want = Math.max(40, w - t8);
  const have = creep.reduce((a, s) => a + s.sleepMs, 0);
  if (Math.abs(have - want) > 1) {
    const k = want / have, m = Math.min(1, k);
    creep = creep.map((s) => ({ x: from.x + (s.x - from.x) * m, y: from.y + (s.y - from.y) * m, sleepMs: s.sleepMs * k }));
    leg = planDrag(creep[creep.length - 1], to, p, { settle, rng });
  }
  return { steps: creep.concat(leg), creep: creep.length };
}

/** Where a reading hand parks the pointer before a wheel: x ~ 0.5w ± 0.10w clamped [0.12w, 0.88w], y ~ 0.34h
 * ± 0.09h clamped [0.10h, 0.62h] (two gaussians, x first). One model in all three SDKs (Python
 * `wheel_anchor_point`, .NET `Motion.WheelAnchorPoint`); before 0.52 Node used 0.48/0.36 and other clamps. */
export function wheelAnchorPoint(vw: number, vh: number, rng: Rng = Math.random): [number, number] {
  const x = clamp(vw * 0.5 + gaussFrom(rng) * vw * 0.10, vw * 0.12, vw * 0.88);
  const y = clamp(vh * 0.34 + gaussFrom(rng) * vh * 0.09, vh * 0.10, vh * 0.62);
  return [x, y];
}

/** Grab/release dwell times (ms) for a held-button drag: log-normal (σ 0.2) around the persona's medians,
 * inside its grab/release bands. */
export function dragDwell(p: Persona, rng: Rng = Math.random): { grabMs: number; releaseMs: number } {
  const tt = timingTraits(p);
  const t = tempoOf(p); // the preset scales the medians; applyToPersona already scaled the bands
  const grabMs = lognormalMs(tt.grabMs * t, 0.2, p.grabMinMs, p.grabMaxMs, rng);
  const releaseMs = lognormalMs(tt.releaseMs * t, 0.2, p.releaseMinMs, p.releaseMaxMs, rng);
  return { grabMs, releaseMs };
}

export interface Box { x: number; y: number; width: number; height: number; }

const PACE_SALT = 0x5bd1e995; // own stream: the pacer's hop cap (SC 299)

/** The longest hop this hand makes between two dispatched pointer points, 55..110 px (the first draw of its
 *  salted pace stream): what a quick reach covers in about one 60 Hz frame at a 3.5-7 px/ms peak. The
 *  wall-clock pacer (pacing.ts walk) never hops further; a late walk stretches in time instead.
 *
 *  Measured on r54 / SDK 0.67.1 (SC 298): a fixed 40 px cap put a wall at 40 px under every persona (0 of
 *  4,462 page-visible steps above 40.5 px), the same for every identity. Same value in _motion.py hop_cap_px
 *  and Motion.HopCapPx. */
export function hopCapPx(p: Persona): number {
  const r = mulberry32((p.seed ^ PACE_SALT) >>> 0);
  return lerp(55, 110, r());
}

// Salt for the aim traits (clickPoint): the persona's landing dispersion, undershoot and handedness.
const AIM_SALT = 0x27d4eb2f;

/** Per-identity aiming traits, stable per seed (see aimTraits). */
export interface AimTraits {
  /** Landing dispersion, sd as a share of the box, 0.10–0.20. */
  spread: number;
  /** Pull toward the side the cursor came from, share of the box, 0.06–0.16. */
  undershoot: number;
  /** Handedness: lateral offset as a share of the box width (approachBias × 0.4, −0.1–0.1). */
  sideBias: number;
  /** Share of visible checkbox / radio clicks that land on the label text, 0.25–0.55 (lane F's label aim). */
  labelShare: number;
}

/** The persona's aiming traits from a salted stream of its seed. Same values as Python `aim_traits` and
 * .NET `Motion.AimTraitsOf`. */
export function aimTraits(p: Persona): AimTraits {
  const r = mulberry32((p.seed ^ AIM_SALT) >>> 0);
  const spread = lerp(0.10, 0.20, r());
  const undershoot = lerp(0.06, 0.16, r());
  const labelShare = lerp(0.25, 0.55, r()); // SC 232 integration: a trait (was one 0.4 for every identity)
  return { spread, undershoot, sideBias: p.approachBias * 0.4, labelShare };
}

/** A human click point inside `box`: a 2D gaussian toward the centre with the persona's dispersion
 * (sd 0.10–0.20 of the box), nudged toward the approach side (undershoot 6–16 %) and sideways by its
 * handedness (approachBias), clamped a couple px inside the edges. The SEED fixes the persona's spread,
 * undershoot and handedness (it was one dispersion for everyone); each click's draw is unseeded (Math.random
 * unless `rng` is given), like every per-action draw, so no two visits of one identity replay the same
 * landing sequence. Without a persona: sd 0.15, 12 % undershoot, no handedness. */
export function clickPoint(box: Box, from: Point, p?: Persona | null, rng: Rng = Math.random): Point {
  let spread = 0.15, under = 0.12, side = 0;
  if (p) {
    const at = aimTraits(p);
    spread = at.spread; under = at.undershoot; side = at.sideBias;
  }
  const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
  const dx = cx - from.x, dy = cy - from.y;
  const d = Math.hypot(dx, dy) || 1;
  const ux = -dx / d, uy = -dy / d;
  const x = cx + ux * box.width * under + side * box.width + gaussFrom(rng) * box.width * spread;
  const y = cy + uy * box.height * under + gaussFrom(rng) * box.height * spread;
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

// ------------------------------------------------------------------ after-action (SC 232 lane C)
// Salt for the after-action stream (hover dwell, move-off after a click, the attention model). A separate
// mulberry32(seed ^ salt) stream keeps makePersona's draw ORDER (every existing seed's motor identity)
// unchanged. Same values in Python `_motion.py` and .NET `Motion.cs`.
const AFTER_SALT = 0x165667b1; // own stream (was the timing salt: dwell moved with the click hold)

/** The pointer keeps within this many px of where a hover arrived while it dwells there. */
export const DWELL_RADIUS_PX = 3;
/** A move-off after a click is kept this far inside the viewport. */
export const MOVE_OFF_MARGIN_PX = 8;

/** Per-identity after-action traits, stable per seed (see afterTraits). */
export interface AfterTraits {
  /** Centre of this hand's hover dwell, 250–700 ms (draws stay in 150–900). */
  dwellMs: number;
  /** Share of clicks followed by a move-off, 0.64–0.82 (a 40-click run stays in 60–85 %). */
  moveOffRate: number;
  /** Centre of the release → move-off gap, 300–600 ms (draws stay in 200–700). */
  moveOffDelayMs: number;
  /** Scale on the attention model's read pause, 0.85–1.15. */
  readTempo: number;
}

/** The persona's after-action traits: the first four draws of its salted after-action stream.
 *
 * Measured on r42 / SDK 0.51.0 (SC 232 review): a hover stopped dead on arrival (0 samples after the
 * glide's last one) and the pointer never left the pressed pixel after a click. Same values as Python
 * `after_traits` and .NET `Motion.AfterTraitsOf`. */
export function afterTraits(p: Persona): AfterTraits {
  const r = mulberry32((p.seed ^ AFTER_SALT) >>> 0);
  const dwellMs = lerp(250, 700, r());
  const moveOffRate = lerp(0.64, 0.82, r());
  const moveOffDelayMs = lerp(300, 600, r());
  const readTempo = lerp(0.85, 1.15, r());
  return { dwellMs, moveOffRate, moveOffDelayMs, readTempo };
}

/** The persona's after-action stream positioned AFTER its four trait draws. Used by the shared vectors only, to
 * compare the SDKs draw for draw: the humanizer's per-action draws are unseeded (Math.random), as in planMove --
 * the seed fixes traits only, or every session of an identity would replay one sequence. */
export function afterRng(p: Persona): Rng {
  const r = mulberry32((p.seed ^ AFTER_SALT) >>> 0);
  for (let i = 0; i < 4; i++) r();
  return r;
}

/** How long a hover dwells on arrival: the persona's centre × 0.6–1.4, kept in 150–800 ms (150–900 as the page
 * measures it, with the glide's slow tail). */
export function dwellMs(p: Persona, rng: Rng = Math.random): number {
  return clamp(afterTraits(p).dwellMs * lerp(0.6, 1.4, rng()), 150, 800);
}

/** Plan the pointer's rest on a hover target: OU drift + 8–12 Hz tremor around `at` for about `ms` (at
 * least 3 samples), every sample within DWELL_RADIUS_PX of `at` and inside `box` (1 px in; null: no box).
 * The hand is not braced on a hover the way it is before a press: drift ×2, tremor ×1.5 of a glide's. */
export function planDwell(at: Point, box: Box | null, p: Persona, ms: number, rng: Rng = Math.random): Step[] {
  const stepMs = 1000 / p.deviceHz;
  const n = Math.max(3, jsRound(Math.max(0, ms) / (stepMs * 1.15))); // rest gaps average 1.15 frames
  const out: Step[] = [];
  let driftX = 0, driftY = 0;
  const tphase = rng() * 2 * Math.PI;
  let t = 0;
  for (let i = 0; i < n; i++) {
    const k = stepMs / p.driftTau;
    driftX += -k * driftX + Math.sqrt(2 * k) * p.driftAmp * 2 * gaussFrom(rng);
    driftY += -k * driftY + Math.sqrt(2 * k) * p.driftAmp * 2 * gaussFrom(rng);
    const ph = tphase + (2 * Math.PI * p.tremorHz * t) / 1000;
    let dx = driftX + p.tremorAmp * 1.5 * Math.sin(ph);
    let dy = driftY + p.tremorAmp * 1.5 * Math.sin(ph + Math.PI / 2);
    const r = Math.hypot(dx, dy);
    if (r > DWELL_RADIUS_PX) { dx = (dx * DWELL_RADIUS_PX) / r; dy = (dy * DWELL_RADIUS_PX) / r; }
    let x = at.x + dx, y = at.y + dy;
    if (box) {
      x = box.width >= 2 ? clamp(x, box.x + 1, box.x + box.width - 1) : at.x;
      y = box.height >= 2 ? clamp(y, box.y + 1, box.y + box.height - 1) : at.y;
    }
    const sleepMs = stepMs * lerp(0.9, 1.4, rng());
    out.push({ x, y, sleepMs });
    t += sleepMs;
  }
  return out;
}

/** Does this click get a move-off? In `afterTraits(p).moveOffRate` of clicks: a draw against the rate,
 * nudged by how far the page's running share (`hits` of `n` earlier clicks) has drifted from it. */
export function moveOffRoll(p: Persona, rng: Rng = Math.random, hits = 0, n = 0): boolean {
  const rate = afterTraits(p).moveOffRate;
  return rng() < clamp(rate + 0.5 * (rate * n - hits), 0.05, 0.98);
}

/** The release → move-off gap: the persona's centre × 0.7–1.3, kept in 200–660 ms (200–700 as the page measures it). */
export function moveOffDelayMs(p: Persona, rng: Rng = Math.random): number {
  const t = tempoOf(p);
  return clamp(afterTraits(p).moveOffDelayMs * t * lerp(0.7, 1.3, rng()), 200 * t, 660 * t);
}

/** Where the hand drifts after a click: 40–200 px from `from` at an angle in the downward half-plane
 * (20–160°, y down: the gateway's moveOnTarget model), kept MOVE_OFF_MARGIN_PX inside `vp`; mirrored
 * upward when the clamp would leave it on the spot (a click near the bottom edge). */
export function planMoveOff(from: Point, vp: Viewport, p: Persona, rng: Rng = Math.random): Point {
  void p;
  const m = MOVE_OFF_MARGIN_PX;
  const ang = lerp(20, 160, rng()) * (Math.PI / 180);   // as Python math.radians rounds it
  const dist = lerp(40, 200, rng());
  const dx = Math.cos(ang) * dist, dy = Math.sin(ang) * dist;
  let x = clamp(from.x + dx, m, vp.width - m);
  let y = clamp(from.y + dy, m, vp.height - m);
  if (Math.hypot(x - from.x, y - from.y) < 12) {
    x = clamp(from.x + dx, m, vp.width - m);
    y = clamp(from.y - dy, m, vp.height - m);
  }
  return { x, y };
}


// ------------------------------------------------------------------ fresh-page entry (SC 239 / pro-r43-13)
// Where a hand comes from on a page the pointer has not been over yet: 30-120 px ABOVE the view, within +-30 % of the
// view width of the target (in from the tab strip / address bar, as pro-r43-13 does for the engine's own first path).
// The samples still above the view are not sent; the page's first event is where the path crosses the top edge
// (y 0-4). Port of Python _motion.entry_point / enter_from_top; sdk/testdata/humanize_motion_vectors.json "entry" pins both.
export const ENTRY_ABOVE_PX: readonly [number, number] = [30, 120];
export const ENTRY_SPREAD = 0.6;
export const ENTRY_CROSS_PX = 4;
export const ENTRY_EDGE_PX = 8;
export const ENTRY_DEFAULT_VIEW_W = 1000;
/** An ambient drift on a fresh page is planned from the entry point too (its first excursion comes in over the top
 *  edge, humanize.ts ambientPlan); the dropped lead-in is ~100-200 ms, so it is planned this much longer and then
 *  trimmed to the ask. */
export const ENTRY_LEAD_MS = 250;

/** The off-view point a fresh page's first glide starts from (x first, then y: two draws from `rng`). */
export function entryPoint(target: Point, viewW?: number | null, rng: Rng = Math.random): Point {
  const w = viewW && viewW > 16 ? viewW : ENTRY_DEFAULT_VIEW_W;
  const x = clamp(target.x + (rng() - 0.5) * ENTRY_SPREAD * w, ENTRY_EDGE_PX, w - ENTRY_EDGE_PX);
  const y = -(ENTRY_ABOVE_PX[0] + rng() * (ENTRY_ABOVE_PX[1] - ENTRY_ABOVE_PX[0]));
  return { x, y };
}

/** `steps` of a path planned from the off-view `start` with the samples still above the view dropped and the first
 *  one put where the path crosses the top edge (y 0-4; one draw from `rng` when a crossing sample is made). The
 *  crossing sample takes the timing of the sample it replaces (or of the first kept one). A path whose start is
 *  inside the view, or that never comes in, is returned as it is. */
export function enterFromTop<S extends Step>(steps: S[], start: Point, rng: Rng = Math.random): S[] {
  if (!steps.length || start.y >= 0) return steps;
  let i = 0;
  while (i < steps.length && steps[i].y < 0) i++;
  if (i >= steps.length) return steps;
  const a = i === 0 ? start : steps[i - 1];
  const b = steps[i];
  if (b.y <= ENTRY_CROSS_PX) return steps.slice(i);
  const yIn = rng() * ENTRY_CROSS_PX;
  const f = b.y > a.y ? (yIn - a.y) / (b.y - a.y) : 1;
  const cx = a.x + (b.x - a.x) * f;
  if (i === 0) return [{ ...steps[0], x: cx, y: yIn }, ...steps];
  return [{ ...steps[i - 1], x: cx, y: yIn }, ...steps.slice(i)];
}
