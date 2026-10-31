// Wall-clock pacing for planned pointer paths (mirrors sdk/python/clearcote/_pacing.py).
//
// The motion planner (motion.ts planMove / planAmbient) returns samples {x, y, sleepMs} at the
// persona's device rate (~6-8 ms apart). Walking them as "dispatch, then sleep the planned gap" adds
// the dispatch round trip to EVERY sample: measured on PRO r38, one mouse.move costs 27 ms median, so a
// 1.05 s planned glide took 3.5-5.4 s and the page saw ~25 pointermove/s (SC 209).
//
// walk() schedules each sample at its planned offset from the start (the cumulative sum of the sleeps
// before it) and keeps that clock:
//   on time -> sleep until the sample's planned time, then dispatch it;
//   behind  -> send the point on the planned path at the time that has passed (between the two planned
//              samples around it), so the move keeps the plan's shape, its duration and its SPEED profile:
//              each hop is the plan's speed x the real time since the last dispatch, whatever the round trip;
//   never skipped: the final sample (the move lands exactly where it was aimed) and a sample marked
//              as changing button state (`pinned`) -- both go out as planned;
//   never a teleport: no hop longer than the persona's maxJump (motion.ts hopCapPx, 55-110 px: what a
//              quick reach covers in about one 60 Hz frame), and no more than IDLE_HOP_PX (20 px) after
//              IDLE_MS (40 ms) with nothing sent; a longer hop goes to the farthest point along the path
//              within the cap, so a late walk stretches in time rather than jumping (SC 210 lab v2: the
//              old cap, 3 px/ms x the time since the last dispatch, turned slow dispatches under load into
//              hops of 60-103 px a few ms apart; an r40 engine walks any hop >= 24 px after >= 40 ms idle).
//              SC 298 (r54, SDK 0.67.1, Windows): the old walk skipped to whole planned samples under a
//              FIXED 40 px cap. With an ~18 ms round trip it hopped 2 or 3 samples at a time -- a sawtooth
//              of 7-8 velocity peaks per glide -- and no page-visible step exceeded 40 px under any persona
//              (0 of 4,462), a wall shared by every identity;
//   a stall is a pause: a single dispatch slower than STALL_MS shifts the schedule by the excess
//              instead of being caught up by skipping.
//
// Sleeps are local timers, never page.waitForTimeout (a CDP round trip a detector can score).
// Nothing here changes what the planner returns; only WHEN each sample goes out, and when it runs late,
// which point between two of them goes out instead.

import { record as traceRecord, traceEnabled } from "./trace.js";

// The hop cap when the caller names no persona (the humanizer passes the persona's motion.ts hopCapPx).
export const MAX_JUMP_PX = 72;
// A single dispatch slower than this is a STALL (the page or the box was busy), not latency: nothing
// reached the page while it lasted, so the plan pauses with it instead of skipping ahead afterwards.
export const STALL_MS = 60;
// A fast hand: ~3 px/ms (a quick flick across a 1080p screen in ~0.6 s). No longer a dispatch cap (the
// hop cap is MAX_JUMP_PX / IDLE_HOP_PX since SC 210 round 2): kept as the speed bound the tests check.
export const FAST_PX_PER_MS = 3;
// After this long with nothing dispatched, a skip may hop at most IDLE_HOP_PX: what a hand at a moderate
// 2.5 px/ms covers in one 125 Hz report (8 ms), and below the 24 px an r40 engine walks.
export const IDLE_MS = 40;
export const IDLE_HOP_PX = 20;

export interface PacedStep { x: number; y: number; sleepMs: number; }

export interface WalkOpts<S extends PacedStep> {
  now?: () => number;                       // ms, monotonic
  sleep?: (ms: number) => Promise<unknown>;
  /** The persona's hop cap (motion.ts hopCapPx); MAX_JUMP_PX when omitted. */
  maxJump?: number;
  pinned?: (s: S, i: number) => boolean;    // a step that changes button state: never skipped
  /** The action kind for the score hook (trace.ts: glide | drag | ambient | hover). With CLEARCOTE_HUMANIZE_SCORE
   *  set, the dispatched samples and their actual dispatch times are scored after the walk. Read-only. */
  kind?: string;
  /** The button bitmask held through the walk (a drag leg: 1), carried on every recorded sample. */
  buttons?: number;
}

const realNow = (): number => performance.now();
const realSleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, Math.max(0, ms)));

/** Planned dispatch offsets (ms from the start) of every step, and the planned total. */
export function schedule(steps: readonly PacedStep[]): { due: number[]; total: number } {
  const due: number[] = [];
  let t = 0;
  for (const s of steps) { due.push(t); t += Math.max(0, s.sleepMs); }
  return { due, total: t };
}

/** Legacy helper (the pre-SC 299 walk): the planned sample to dispatch next when only whole samples may go
 *  out (step i is the first not yet sent, `elapsed` ms have passed). walk() itself sends points between
 *  samples (target); kept for callers outside it. */
export function nextIndex<S extends PacedStep>(
  steps: readonly S[], due: readonly number[], i: number, last: { x: number; y: number } | null,
  elapsed: number, maxJump = MAX_JUMP_PX, pinned: (s: S, i: number) => boolean = () => false,
): number {
  const n = steps.length;
  let j = i;
  while (j + 1 < n && due[j + 1] <= elapsed && !pinned(steps[j], j)) j++;
  if (last) {
    while (j > i && Math.hypot(steps[j].x - last.x, steps[j].y - last.y) > maxJump) j--;
  }
  return j;
}

/** The first index whose value is > t (Python's bisect.bisect_right on a sorted array). */
function bisectRight(a: readonly number[], t: number): number {
  let lo = 0, hi = a.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (t < a[mid]) hi = mid; else lo = mid + 1;
  }
  return lo;
}

/** The point of the planned path at plan time `t` (ms): linear between the two samples around it. Port of
 *  _pacing._pos_at. */
export function posAt(steps: readonly PacedStep[], due: readonly number[], t: number): { x: number; y: number } {
  const n = steps.length;
  if (t <= due[0]) return { x: steps[0].x, y: steps[0].y };
  if (t >= due[n - 1]) return { x: steps[n - 1].x, y: steps[n - 1].y };
  const k = bisectRight(due, t);        // due[k-1] <= t < due[k]
  const a = steps[k - 1], b = steps[k];
  const span = due[k] - due[k - 1];
  const f = span > 0 ? (t - due[k - 1]) / span : 1;
  return { x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f };
}

/** One dispatch: the point, its plan time and the index of the next planned sample not yet passed. */
export interface Target { x: number; y: number; tp: number; next: number; }

/** What goes out next when step `i` is the first planned sample not yet passed and `elapsed` ms of the plan
 *  have gone by: the point, its plan time `tp` and the next `i`.
 *
 *  On time: step i itself. Behind: the point of the path at `elapsed` -- or, when one is due, the first
 *  pinned step or the final step, exactly -- pulled back along the path until it lies within `cap` of the
 *  last dispatched point (the walk then stretches in time). Port of _pacing._target. */
export function target<S extends PacedStep>(
  steps: readonly S[], due: readonly number[], i: number, tPlanLast: number | null,
  last: { x: number; y: number } | null, elapsed: number, cap: number,
  pinned: (s: S, i: number) => boolean = () => false,
): Target {
  const n = steps.length;
  if (elapsed <= due[i]) return { x: steps[i].x, y: steps[i].y, tp: due[i], next: i + 1 };
  let j = i;
  while (j + 1 < n && due[j + 1] <= elapsed && !pinned(steps[j], j)) j++;
  let exact = j === n - 1 || pinned(steps[j], j);
  let x: number, y: number, tp: number;
  if (exact) {
    x = steps[j].x; y = steps[j].y; tp = due[j];
  } else {
    ({ x, y } = posAt(steps, due, elapsed));
    tp = elapsed;
  }
  if (last && Math.hypot(x - last.x, y - last.y) > cap) {
    let lo = tPlanLast ?? 0;
    let hi = tp;
    for (let k = 0; k < 24; k++) {   // the farthest plan time whose point is within the cap
      const mid = (lo + hi) / 2;
      const q = posAt(steps, due, mid);
      if (Math.hypot(q.x - last.x, q.y - last.y) <= cap) lo = mid; else hi = mid;
    }
    ({ x, y } = posAt(steps, due, lo));
    tp = lo;
    exact = false;
    if (tPlanLast !== null && tp <= tPlanLast + 1e-9) {
      // no progress within the cap (a zero-length planned gap jumps): send step i as planned
      return { x: steps[i].x, y: steps[i].y, tp: due[i], next: i + 1 };
    }
  }
  if (exact) return { x, y, tp, next: j + 1 };
  // the next planned sample still ahead of the point just sent (never one already passed)
  return { x, y, tp, next: Math.max(i, bisectRight(due, tp)) };
}

/** Dispatch `steps` through `move` against a wall clock (see the file header). Stops at the first move that
 *  throws. Resolves to the number of points dispatched. `o.maxJump`: the persona's hop cap (motion.ts
 *  hopCapPx). */
export async function walk<S extends PacedStep>(
  steps: readonly S[], move: (x: number, y: number) => Promise<unknown>, o: WalkOpts<S> = {},
): Promise<number> {
  const now = o.now ?? realNow;
  const sleep = o.sleep ?? realSleep;
  const maxJump = o.maxJump ?? MAX_JUMP_PX;
  const pinned = o.pinned ?? (() => false);
  if (!steps.length) return 0;
  const trace: { x: number; y: number; t: number; buttons?: number }[] | null = traceEnabled() ? [] : null;
  const kind = o.kind ?? "glide";
  const buttons = o.buttons ?? 0;
  const { due, total } = schedule(steps);
  let t0 = now();
  const tStart = t0;
  let tLast = t0;
  let i = 0, sent = 0;
  let last: { x: number; y: number } | null = null;
  let tPlanLast: number | null = null;
  while (i < steps.length) {
    let el = now() - t0;
    if (el < due[i]) { await sleep(due[i] - el); el = due[i]; }
    const g: Target = target(steps, due, i, tPlanLast, last, el, jumpCap(maxJump, now() - tLast), pinned);
    const tb = now();
    try { await move(g.x, g.y); } catch {
      if (trace?.length) traceRecord(kind, trace, { planned_n: steps.length, sent, aborted: true });
      return sent;
    }
    sent++;
    trace?.push(buttons ? { x: g.x, y: g.y, t: tb - tStart, buttons } : { x: g.x, y: g.y, t: tb - tStart });
    t0 += stallMs(now() - tb);
    last = { x: g.x, y: g.y };
    tLast = tb;
    tPlanLast = g.tp;
    i = g.next;
  }
  const el = now() - t0;
  if (el < total) await sleep(total - el);   // the last sample's own dwell (a settle hold, a frame)
  if (trace?.length) traceRecord(kind, trace, { planned_n: steps.length, planned_ms: total, sent });
  return sent;
}

/** How far inside the viewport a planned path is kept (SC 210 fix 2, verifier B: the planner's overshoot
 *  arcs left the window near its edges and the page saw no events while the pointer was outside). */
export const VIEW_MARGIN_PX = 2;

/** `steps` with every sample but the last moved inside the viewport `vw` x `vh` (`margin` px in). The last
 *  sample -- where the move was aimed -- is never changed, and a move aimed outside the viewport is
 *  returned as it is. Only coordinates change. Port of _pacing.keep_in_view. */
export function keepInView<S extends PacedStep>(steps: S[], vw: number, vh: number, margin = VIEW_MARGIN_PX): S[] {
  if (!steps.length || !vw || !vh) return steps;
  const t = steps[steps.length - 1];
  if (!(t.x >= 0 && t.x <= vw && t.y >= 0 && t.y <= vh)) return steps;
  const lx = Math.min(margin, t.x), hx = Math.max(vw - margin, t.x);
  const ly = Math.min(margin, t.y), hy = Math.max(vh - margin, t.y);
  return steps.map((s, k) => {
    if (k === steps.length - 1) return s;
    const x = Math.min(Math.max(s.x, lx), hx), y = Math.min(Math.max(s.y, ly), hy);
    return x === s.x && y === s.y ? s : { ...s, x, y };
  });
}

/** The largest hop between two dispatched points `dtMs` apart: maxJump, and IDLE_HOP_PX
 *  after an idle gap. Never more for a longer gap: a walk that fell behind stretches in time instead of
 *  catching up with a jump (the planner's own consecutive samples always go out). Port of
 *  _pacing._jump_cap. */
export const jumpCap = (maxJump: number, dtMs: number): number => (dtMs < IDLE_MS ? maxJump : Math.min(maxJump, IDLE_HOP_PX));

// Measured on PRO r38 under load: one move answered after 523 ms. Catching that up by skipping coarsened
// the rest of the glide to maximum hops, and across the fixture's iframe the page saw a 110 px jump. A
// single dispatch slower than STALL_MS is a stall: nothing reached the page while it lasted, so the plan
// pauses with it and only STALL_MS is caught up.
const stallMs = (took: number): number => (took > STALL_MS ? took - STALL_MS : 0);

/** px/ms arriving at sample k (0 for the first). */
export function speedAt(steps: readonly PacedStep[], k: number): number {
  if (k === 0) return 0;
  const a = steps[k - 1], b = steps[k];
  return Math.hypot(b.x - a.x, b.y - a.y) / Math.max(1e-3, a.sleepMs);
}

/**
 * Cut an ambient plan so it lasts about `ms`. planAmbient keeps adding whole excursions + rests until
 * its budget is spent and draws each rest sample's gap from 0.9-1.4x the frame time it counts, so its
 * plan runs well past what was asked. Of the samples whose end falls in [lo*ms, hi*ms], keep up to the
 * slowest one (inside a rest or the tail of a move). Only a tail is dropped; no sample is changed.
 *
 * The one exception to "about ms": when the whole window lies inside the first excursion (a short ask,
 * ~300 ms, against a ~0.5-1 s glide) every candidate is mid-flight, and a pointer that stops dead at
 * speed is a kinematic no hand produces. Then the cut moves forward to where that move has slowed down
 * (<= 10 % of the plan's peak speed), as long as that is within `stretch` x ms.
 */
export function trimTo<S extends PacedStep>(steps: S[], ms: number, lo = 0.85, hi = 1.10, stretch = 2): S[] {
  if (!steps.length || ms <= 0) return steps;
  const { due, total } = schedule(steps);
  if (total <= hi * ms) return steps;
  const end = steps.map((s, k) => due[k] + Math.max(0, s.sleepMs));
  let best = -1, bestV = Infinity;
  for (let k = 0; k < steps.length; k++) {
    if (end[k] < lo * ms) continue;
    if (end[k] > hi * ms) break;
    const v = speedAt(steps, k);
    if (v < bestV) { best = k; bestV = v; }
  }
  if (best < 0) {
    best = 0;
    for (let k = 0; k < steps.length; k++) if (end[k] <= hi * ms) best = k;
  }
  let peak = 0;
  for (let k = 0; k < steps.length; k++) peak = Math.max(peak, speedAt(steps, k));
  if (peak > 0 && speedAt(steps, best) > 0.3 * peak) {
    // First the rest of the +-20 % the caller was promised, then forward.
    let wk = -1, wv = Infinity;
    for (let k = 0; k < steps.length; k++) {
      if (end[k] < 0.8 * ms || end[k] > 1.15 * ms) continue;
      const v = speedAt(steps, k);
      if (v < wv) { wk = k; wv = v; }
    }
    if (wk >= 0 && wv <= 0.3 * peak) return steps.slice(0, wk + 1);
    for (let k = best + 1; k < steps.length; k++) {
      if (end[k] > stretch * ms) break;
      if (speedAt(steps, k) <= 0.1 * peak) { best = k; break; }
    }
  }
  return steps.slice(0, best + 1);
}
