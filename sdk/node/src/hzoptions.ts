// `humanize:` options: the object form of the switch, the fallback report and strict mode.
//
// `humanize` accepts `true` / `false` (as before), `"strict"` (`{ strict: true }`), or a HumanizeOptions
// object (an unknown key throws). `humanize: true` is exactly `{}`: every default reproduces 0.51.0, and no
// option draws from the persona's RNG stream, so a seed keeps the same motor persona whatever is given.
//
// Every place where the humanized route hands an action to Playwright's own (stock, untimed) call reports
// it through reportFallback: by default `onFallback({ api, reason, selector })` is called; with
// `strict: true` a ClearcoteStrictError is thrown instead and the stock call does not run. Same options,
// same meaning, in the Python (`_hzoptions.py`) and .NET (`Humanize.cs`) SDKs.
import { AsyncLocalStorage } from "node:async_hooks";

export type HumanizePreset = "careful" | "default" | "quick";
export type HumanizeEngine = "auto" | "all" | "coords" | "off";
export type HumanizeAmbient = "off" | "load" | "idle";

/** One action handed to Playwright's own call. */
export interface HumanizeFallbackEvent {
  /** The API that was called ("page.click", "locator.fill", ...). */
  api: string;
  /** Why the humanized route did not apply. */
  reason: string;
  /** The selector, when there is one. */
  selector?: string;
}

/** The `humanize:` options (all optional; `{}` is `humanize: true`). */
export interface HumanizeOptions {
  /** Every planned duration (glides, presses, key dwell and gaps) x1.4 / x1.0 / x0.7, and the rate of
   * thinking pauses scaled the same way. Default "default". */
  preset?: HumanizePreset;
  /** Throw ClearcoteStrictError instead of falling back to a stock Playwright call. */
  strict?: boolean;
  /** Which clicks the engine's pointer path walks: "auto" (CLEARCOTE_HUMANIZE_ENGINE_CLICKS, else
   * "coords"), "all", "coords", "off". Replaces the environment variable when not "auto". */
  engine?: HumanizeEngine;
  /** "load": a short ambient burst on every page load (the old `page._clearcoteAutoAmbient`); "idle":
   * idle drifts between actions; "off" (default). */
  ambient?: HumanizeAmbient;
  /** Per-character slip rate 0..1; unset = CLEARCOTE_HUMANIZE_TYPO_RATE, else 0.02. */
  typoRate?: number;
  /** "left" | "right"; unset = the persona's. */
  handedness?: "left" | "right";
  /** The pointer's report rate (Hz) in place of the persona's. */
  deviceHz?: number;
  /** "mouse" | "trackpad"; unset = the persona's. */
  inputDevice?: "mouse" | "trackpad";
  /** Called each time an action is handed to Playwright's own call (not in strict mode). Throwing from it
   * stops the stock call. */
  onFallback?: (event: HumanizeFallbackEvent) => void;
}

/** What `humanize:` accepts. */
export type HumanizeSetting = boolean | "strict" | HumanizeOptions;

export const PRESET_TEMPO: Readonly<Record<HumanizePreset, number>> = { careful: 1.4, default: 1.0, quick: 0.7 };

/** A validated HumanizeOptions with its defaults filled in. */
export interface ResolvedHumanize {
  preset: HumanizePreset;
  strict: boolean;
  engine: HumanizeEngine;
  ambient?: HumanizeAmbient;
  typoRate?: number;
  handedness?: "left" | "right";
  deviceHz?: number;
  inputDevice?: "mouse" | "trackpad";
  onFallback?: (event: HumanizeFallbackEvent) => void;
  tempo: number;
}

const KEYS = ["preset", "strict", "engine", "ambient", "typoRate", "handedness", "deviceHz", "inputDevice", "onFallback"];
// snake_case spellings accepted too, so one options object works in every SDK and in the cloud API
const ALIASES: Record<string, string> = { typo_rate: "typoRate", device_hz: "deviceHz", input_device: "inputDevice", on_fallback: "onFallback" };
const RESOLVED = Symbol.for("clearcote.humanizeResolved");

/** Thrown in strict mode where the humanized route would have handed the action to Playwright's own call. */
export class ClearcoteStrictError extends Error {
  readonly api: string;
  readonly reason: string;
  readonly selector?: string;
  constructor(api: string, reason: string, selector?: string) {
    super(`humanize strict: ${selector ? JSON.stringify(selector) : "the action"} would fall back to a stock Playwright call (${reason})`);
    this.name = "ClearcoteStrictError";
    this.api = api;
    this.reason = reason;
    this.selector = selector;
    // worded by the caller's API like Playwright's own errors (humanize.ts asApiError)
    (this as any)[Symbol.for("clearcote.playwrightError")] = true;
  }
}

function oneOf<T extends string>(name: string, v: unknown, allowed: readonly T[]): T | undefined {
  if (v === undefined || v === null) return undefined;
  if (typeof v !== "string" || !allowed.includes(v as T)) {
    throw new TypeError(`humanize ${name} must be ${allowed.map((a) => `"${a}"`).join(", ")}, not ${JSON.stringify(v)}`);
  }
  return v as T;
}

/** `humanize:` as given -> the resolved options, or null for off. Throws on what it does not know. */
export function resolveHumanize(value: unknown): ResolvedHumanize | null {
  if (value === undefined || value === null || value === false) return null;
  if (value && typeof value === "object" && (value as any)[RESOLVED]) return value as ResolvedHumanize;
  let o: Record<string, unknown>;
  if (value === true) o = {};
  else if (value === "strict") o = { strict: true };
  else if (typeof value === "object" && !Array.isArray(value)) {
    o = {};
    for (const [k0, v] of Object.entries(value as Record<string, unknown>)) {
      const k = ALIASES[k0] ?? k0;
      if (!KEYS.includes(k)) throw new TypeError(`unknown humanize option "${k0}" (known: ${KEYS.join(", ")})`);
      o[k] = v;
    }
  } else {
    throw new TypeError(`humanize must be true, false, "strict" or an options object, not ${JSON.stringify(value)}`);
  }
  const preset = oneOf("preset", o.preset, ["careful", "default", "quick"] as const) ?? "default";
  if (o.strict !== undefined && typeof o.strict !== "boolean") throw new TypeError("humanize strict must be true or false");
  const r: ResolvedHumanize = {
    preset,
    strict: o.strict === true,
    engine: oneOf("engine", o.engine, ["auto", "all", "coords", "off"] as const) ?? "auto",
    tempo: PRESET_TEMPO[preset],
  };
  const ambient = oneOf("ambient", o.ambient, ["off", "load", "idle"] as const);
  if (ambient) r.ambient = ambient;
  if (o.typoRate !== undefined && o.typoRate !== null) {
    const t = o.typoRate;
    if (typeof t !== "number" || !(t >= 0 && t <= 1)) throw new TypeError("humanize typoRate must be a number from 0 to 1");
    r.typoRate = t;
  }
  const hand = oneOf("handedness", o.handedness, ["left", "right"] as const);
  if (hand) r.handedness = hand;
  if (o.deviceHz !== undefined && o.deviceHz !== null) {
    const h = o.deviceHz;
    if (typeof h !== "number" || !Number.isInteger(h) || h < 30 || h > 8000) throw new TypeError("humanize deviceHz must be a whole number of Hz from 30 to 8000");
    r.deviceHz = h;
  }
  const dev = oneOf("inputDevice", o.inputDevice, ["mouse", "trackpad"] as const);
  if (dev) r.inputDevice = dev;
  if (o.onFallback !== undefined && o.onFallback !== null) {
    if (typeof o.onFallback !== "function") throw new TypeError("humanize onFallback must be a function");
    r.onFallback = o.onFallback as (e: HumanizeFallbackEvent) => void;
  }
  Object.defineProperty(r, RESOLVED, { value: true, enumerable: false });
  return r;
}

/** The JSON-able options (no callback, unset values left out): what a cloud launch sends as humanizeOptions. */
export function humanizePayload(r: ResolvedHumanize): Record<string, unknown> {
  const out: Record<string, unknown> = { preset: r.preset, strict: r.strict, engine: r.engine };
  for (const k of ["ambient", "typoRate", "handedness", "deviceHz", "inputDevice"] as const) if (r[k] !== undefined) out[k] = r[k];
  return out;
}

// The public API a call came in through ("locator.click" when a locator routes through page.click).
const caller = new AsyncLocalStorage<string>();

/** Run `fn` with `api` as the outermost public entry point (kept when one is already set). */
export function withCallerApi<R>(api: string, fn: () => Promise<R>): Promise<R> {
  return caller.getStore() === undefined ? caller.run(api, fn) : fn();
}

/** Whether a humanized SDK method is running in this async context (its own Playwright calls, its native fallback). */
export function inHumanizedCall(): boolean {
  return caller.getStore() !== undefined;
}

/** The outermost public SDK entry point running in this async context ("page.check", "locator.click"), if any. */
export function callerApi(): string | undefined {
  return caller.getStore();
}

export function optionsOf(page: unknown): ResolvedHumanize | null {
  const o = (page as any)?._clearcoteHz;
  return o && o[RESOLVED] ? (o as ResolvedHumanize) : null;
}

/** One humanized action is about to be handed to Playwright's own call: tell onFallback (its error
 * propagates and stops the stock call), or, in strict mode, throw ClearcoteStrictError instead. */
export function reportFallback(page: unknown, api: string, reason: string, selector?: string): HumanizeFallbackEvent {
  const ev: HumanizeFallbackEvent = { api: caller.getStore() ?? api, reason, ...(selector !== undefined ? { selector } : {}) };
  // the newest event is kept on the page either way (a test and hook-less caller seam)
  try { (page as any)._clearcoteLastFallback = ev; } catch { /* a frozen or non-object page */ }
  const o = optionsOf(page);
  if (!o) return ev;
  if (o.strict) throw new ClearcoteStrictError(ev.api, reason, selector);
  if (o.onFallback) o.onFallback(ev);
  return ev;
}

/** The engine click mode: the page's option unless "auto", else the environment's. */
export function engineModeFor(page: unknown, envMode: () => "off" | "coords" | "all"): "off" | "coords" | "all" {
  const o = optionsOf(page);
  return o && o.engine !== "auto" ? o.engine : envMode();
}

export function typoRateFor(page: unknown, envRate: () => number): number {
  const o = optionsOf(page);
  return o && o.typoRate !== undefined ? o.typoRate : envRate();
}

/** The options a planner reads from the persona, set as plain fields after makePersona (no RNG draw, so the
 * seed -> persona map is unchanged). The timing windows are scaled by the preset here; other planners read
 * tempo / pauseScale / handedness / inputDevice defensively. */
export function applyToPersona<P extends object>(persona: P, r: ResolvedHumanize): P {
  const p: any = persona;
  const t = r.tempo;
  p.tempo = t;
  p.pauseScale = t;
  p.handedness = r.handedness;
  if ((r.handedness === "left" || r.handedness === "right") && typeof p.approachBias === "number") {
    // the persona's handedness is the sign of its approach bias (path bow side, click side bias): the option fixes
    // the sign and keeps the drawn magnitude ("right" = positive)
    p.approachBias = r.handedness === "right" ? Math.abs(p.approachBias) : -Math.abs(p.approachBias);
  }
  p.inputDevice = r.inputDevice;
  if (t !== 1) {
    for (const k of ["grabMinMs", "grabMaxMs", "releaseMinMs", "releaseMaxMs", "clickHoldMinMs", "clickHoldMaxMs", "keyDwellMinMs", "keyDwellMaxMs"]) {
      if (typeof p[k] === "number") p[k] *= t;
    }
  }
  if (r.deviceHz !== undefined) p.deviceHz = r.deviceHz;
  return persona;
}
