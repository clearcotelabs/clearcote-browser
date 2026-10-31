/**
 * The attention model (SC 232 lane C): what a reader does BETWEEN the actions a script asks for. A port of
 * sdk/python/clearcote/_attention.py (same values; pinned by sdk/testdata/humanize_after_vectors.json).
 *
 * Pure planning, no Playwright: humanize.ts calls these at the start of the NEXT action -- never from a
 * timer -- and sleeps / dispatches what they return.
 *  - After a `load` the first action waits a READ PAUSE of 0.8–4 s, scaled by how much text the page
 *    shows (measured in the isolated world, capped at READ_CAP_CHARS), and in EXPLORE_RATE of loads rolls
 *    the wheel 1–3 notches first.
 *  - When more than IDLE_GAP_S pass between two actions, the hand idles: 1–2 ambient drifts of 300–900 ms.
 * ON by default since 0.68.0 (SC 299). Measured on r54 / SDK 0.67.1 (SC 298/299): with the model off, the first
 * input came 0.21-0.31 s after the load on every persona, as fast as a stock Playwright script. Off with the
 * option `attention: false`; the page flag `page._clearcoteAttention` (true / false) overrides both ways.
 */
import { optionsOf } from "./hzoptions.js";
import { afterTraits, tempoOf, type Persona, type Rng } from "./motion.js";

export const READ_MIN_MS = 800;
export const READ_MAX_MS = 4000;
export const READ_CAP_CHARS = 20000;
export const EXPLORE_RATE = 0.3;
export const IDLE_GAP_S = 1.5;
export const IDLE_DRIFT_MIN_MS = 300;
export const IDLE_DRIFT_MAX_MS = 900;

/** Visible text length of the document, capped (run in the isolated world). Same source as Python's. */
export const TEXT_LENGTH = "() => { const b = document.body; if (!b) return 0; let t = ''; try { t = b.innerText || ''; }"
  + ` catch (e) { t = b.textContent || ''; } return Math.min(${READ_CAP_CHARS}, t.length); }`;

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** The read pause before the first action on a freshly loaded page (ms): READ_MIN_MS for an empty page,
 * READ_MAX_MS from READ_CAP_CHARS of text (square-root growth between), × the persona's read tempo ×
 * 0.85–1.15, kept in the band. */
export function readPauseMs(textLen: unknown, p: Persona, rng: Rng = Math.random): number {
  let n = Number(textLen ?? 0);
  if (!Number.isFinite(n)) n = 0;
  const frac = Math.sqrt(clamp(n / READ_CAP_CHARS, 0, 1));
  const t = tempoOf(p); // the preset (careful / quick) scales the read pause and its band
  const ms = (READ_MIN_MS + (READ_MAX_MS - READ_MIN_MS) * frac) * afterTraits(p).readTempo * t * lerp(0.85, 1.15, rng());
  return clamp(ms, READ_MIN_MS * t, READ_MAX_MS * t);
}

/** 0 (most loads) or 1–3 exploratory wheel notches down, in EXPLORE_RATE of loads. */
export function exploreNotches(rng: Rng = Math.random): number {
  if (rng() >= EXPLORE_RATE) return 0;
  return 1 + Math.floor(rng() * 3);
}

/** The ambient drifts (ms each) the hand makes after `gapS` seconds without an action: none up to
 * IDLE_GAP_S, else 1–2 of IDLE_DRIFT_MIN_MS–IDLE_DRIFT_MAX_MS. */
export function idleDrifts(gapS: number | null | undefined, rng: Rng = Math.random): number[] {
  if (gapS === null || gapS === undefined || gapS <= IDLE_GAP_S) return [];
  const n = 1 + (rng() < 0.5 ? 1 : 0);
  const out: number[] = [];
  for (let i = 0; i < n; i++) out.push(lerp(IDLE_DRIFT_MIN_MS, IDLE_DRIFT_MAX_MS, rng()));
  return out;
}

/** A humanize option read off the page: the page's resolved HumanizeOptions (`optionsOf`), else a stand-in
 * `page._clearcoteOptions` object (the tests' seam), else `dflt`. Never throws. */
export function option(page: any, name: string, dflt: unknown = undefined): unknown {
  try {
    const o: any = optionsOf(page) ?? page?._clearcoteOptions;
    if (o === null || o === undefined || typeof o !== "object") return dflt;
    return name in o ? o[name] : dflt;
  } catch { return dflt; }
}

/** Is the attention model on for this page: the page flag `_clearcoteAttention` when it is true or false, else
 * on unless the `attention` option is false -- or when `ambient: "idle"` asks for it. Port of
 * _attention.attention_enabled. */
export function attentionEnabled(page: any): boolean {
  let flag: unknown;
  try { flag = page?._clearcoteAttention; } catch { flag = undefined; }
  if (flag === true || flag === false) return flag;
  if (ambientMode(page) === "idle") return true;
  return option(page, "attention", undefined) !== false;
}

/** The page's ambient mode: lane A's `ambient` option ("off" | "load" | "idle"), else "off". A truthy
 * `attention` option maps to "idle". */
export function ambientMode(page: any): "off" | "load" | "idle" {
  const v = option(page, "ambient");
  if (typeof v === "string") {
    const m = v.trim().toLowerCase();
    if (m === "off" || m === "load" || m === "idle") return m;
  }
  if (option(page, "attention", false)) return "idle";
  return "off";
}
