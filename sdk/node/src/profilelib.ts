// Profile library: index, host-coherence scoring, and selection.
//
// The persona path this serves is `--fingerprint-profile` WITHOUT `--fingerprint`. That
// distinction is load-bearing and is why this module exists: measured against a strict
// anti-bot device check, a seed persona failed 13/13 while a profile-only persona passed. With no
// seed the farbling machinery never engages, so canvas/WebGL readbacks come back
// byte-identical to an unmodified browser — the profile is static value substitution over a
// real, coherent capture rather than perturbation of this machine's.
//
// SELECTION IS ABOUT COHERENCE WITH *THIS HOST*, NOT ABOUT MATCHING AN OS STRING.
// A profile claiming an RTX 3080 on an Intel UHD host says NVIDIA in the strings and paints
// Intel pixels — a contradiction a detector reads in one call. So the GPU term dominates the
// score, and `browser_major` is a HARD filter: a Chrome-138 capture on a 150 engine reports
// UA 138 while the engine behaves like 150, which is self-contradictory in a way no scoring
// weight should be able to outvote.
//
// ROTATION IS PER-IDENTITY, NOT PER-LAUNCH. Always serving the single best profile makes every
// customer on similar hardware converge on one identity, and that shared fingerprint becomes its
// own signal. But re-rolling every launch is worse: an account whose device changes on every
// visit is a harder tell than a slightly-suboptimal stable one. So the default ("rotate") is
// sticky-per-key — same key always resolves to the same profile, different keys spread across
// the top-N.

import { createHash } from "node:crypto";

/** One row of the profile index. Deliberately slim: selection never touches a blob until it has
 *  picked a winner, so the index stays cheap to hold in memory and to ship. */
export interface ProfileIndexEntry {
  id: string;
  os_family: "windows" | "macos" | "linux" | "android" | (string & {});
  os_major?: string;
  /**
   * Chromium major of the capture, HARD-filtered against the engine's own major.
   *
   * `null`/absent means VERSION-AGNOSTIC and matches any engine. That is not a loophole — it is
   * the correct description of a hardware-only profile. Imports from the chrome-fingerprints
   * dataset deliberately drop the browser version (its records are Chrome ~114/115) and carry
   * only version-independent identity: GPU, screen, fonts, voices, audio, CPU/memory, keyboard.
   * Such a profile inherits the running engine's version, so it cannot contradict it.
   *
   * A profile that DOES pin a version and gets it wrong is a real contradiction — UA says 138
   * while the engine behaves like 150 — which is why a mismatched number is still rejected.
   */
  browser_major?: number | null;
  /** Coarse vendor class parsed from the renderer string: nvidia | amd | intel | apple | other. */
  gpu_vendor: string;
  /** Rough capability tier (0 = integrated, 1 = mainstream, 2 = high-end). */
  gpu_tier?: number;
  renderer?: string;
  screen_width?: number;
  screen_height?: number;
  device_pixel_ratio?: number;
  hardware_concurrency?: number;
  device_memory?: number;
  /** gzip+base64 length of the encoded profile — used to keep the launch under the argv limit. */
  encoded_size?: number;
  /** ISO date of capture; newer is mildly preferred. */
  captured_at?: string;
}

/** What we know about the machine actually doing the rendering. */
export interface HostFacts {
  os_family: string;
  browser_major: number;
  gpu_vendor?: string;
  gpu_tier?: number;
  screen_width?: number;
  screen_height?: number;
  device_pixel_ratio?: number;
  hardware_concurrency?: number;
  device_memory?: number;
}

export type SelectMode = "best" | "rotate" | "random";

export interface SelectOptions {
  /** `"rotate"` (default) sticky-per-key; `"best"` always top-scoring; `"random"` fresh each call. */
  mode?: SelectMode;
  /** Stickiness key for `"rotate"` — an account id, proxy session, or profile name. */
  key?: string;
  /** Candidate pool the mode picks from. Wide enough to avoid herding, narrow enough to stay coherent. */
  topN?: number;
  /** Override the target OS (defaults to the host's). */
  os?: string;
  /** Hard ceiling on encoded size; entries above it cannot be launched on Windows. */
  maxEncodedSize?: number;
}

/** Windows caps a process command line at 32767 chars. The profile rides on argv as
 *  `--fingerprint-profile=<gzip+base64>`, and the rest of the switches need room too, so keep a
 *  wide margin. Exceeding this is not a soft failure — Chromium refuses to spawn and Playwright
 *  surfaces it as an opaque `spawn UNKNOWN`, which is why this is enforced at selection time. */
export const DEFAULT_MAX_ENCODED = 24000;

const DEFAULT_TOP_N = 25;

/** Coarse GPU vendor class from a renderer string (works for ANGLE and raw GL strings). */
export function gpuVendorClass(renderer: string | undefined | null): string {
  const r = (renderer ?? "").toLowerCase();
  if (!r) return "unknown";
  if (r.includes("nvidia") || r.includes("geforce") || r.includes("quadro")) return "nvidia";
  if (r.includes("amd") || r.includes("radeon") || r.includes("ati ")) return "amd";
  if (r.includes("intel")) return "intel";
  if (r.includes("apple")) return "apple";
  if (r.includes("swiftshader") || r.includes("llvmpipe") || r.includes("software")) return "software";
  if (r.includes("mali") || r.includes("adreno") || r.includes("powervr")) return "mobile";
  return "other";
}

/** Ratio-based closeness in [0,1] — 1 when equal, decaying as the values diverge. */
function closeness(a: number | undefined, b: number | undefined): number {
  if (a === undefined || b === undefined || a <= 0 || b <= 0) return 0.5; // unknown: neutral
  const hi = Math.max(a, b), lo = Math.min(a, b);
  return lo / hi;
}

/**
 * Score a candidate against the host. Higher is better; the maximum is 1.
 *
 * Weights encode what a detector can actually catch, in descending order of how cheaply it can
 * be caught:
 *  - GPU class (0.45): string-vs-render mismatch is a single-call contradiction.
 *  - Screen (0.25): a persona whose screen cannot contain the real window is the documented
 *    "reliable block trigger"; a profile *larger* than the host display is the dangerous
 *    direction, so it is penalised harder than a smaller one.
 *  - DPR (0.10): interacts with screen and with CSS pixel math.
 *  - Cores/memory (0.10): weakly checkable against measured worker parallelism.
 *  - Freshness (0.10): a recent capture is likelier to match current Chrome behaviour.
 */
export function scoreProfile(entry: ProfileIndexEntry, host: HostFacts): number {
  let score = 0;

  // -- GPU: exact vendor match dominates. A software renderer on either side is disqualifying
  //    in practice (the pixels can never match a claimed discrete GPU), so it scores 0.
  const hv = host.gpu_vendor ?? "unknown";
  const ev = entry.gpu_vendor ?? "unknown";
  let gpu: number;
  if (ev === "software" || hv === "software") gpu = 0;
  else if (ev === hv) gpu = 1;
  else if (hv === "unknown" || ev === "unknown") gpu = 0.5;
  else gpu = 0.15; // different real vendor: strings and pixels disagree
  // tier proximity refines a same-vendor match
  if (gpu === 1 && entry.gpu_tier !== undefined && host.gpu_tier !== undefined) {
    gpu = 1 - Math.min(1, Math.abs(entry.gpu_tier - host.gpu_tier) / 3) * 0.4;
  }
  score += 0.45 * gpu;

  // -- Screen. Asymmetric on purpose: claiming a LARGER display than the host has is the
  //    contradiction that gets caught (the window cannot be contained), so penalise it more.
  const ew = entry.screen_width, eh = entry.screen_height;
  const hw = host.screen_width, hh = host.screen_height;
  let screen = 0.5;
  if (ew && eh && hw && hh) {
    const fits = ew <= hw && eh <= hh;
    const areaRatio = closeness(ew * eh, hw * hh);
    screen = fits ? 0.7 + 0.3 * areaRatio : 0.25 * areaRatio;
  }
  score += 0.25 * screen;

  score += 0.10 * closeness(entry.device_pixel_ratio, host.device_pixel_ratio);
  score += 0.05 * closeness(entry.hardware_concurrency, host.hardware_concurrency);
  score += 0.05 * closeness(entry.device_memory, host.device_memory);

  // -- Freshness: full credit inside 6 months, decaying to zero at ~2 years.
  let fresh = 0.5;
  if (entry.captured_at) {
    const days = (Date.now() - Date.parse(entry.captured_at)) / 86_400_000;
    fresh = Number.isFinite(days) ? Math.max(0, Math.min(1, 1 - (days - 180) / 550)) : 0.5;
  }
  score += 0.10 * fresh;

  return score;
}

/** Hard filters. A candidate failing any of these is not merely low-scoring — it is incoherent,
 *  and no weight should be able to promote it back into the pool. */
export function eligible(
  entry: ProfileIndexEntry,
  host: HostFacts,
  opts: SelectOptions = {},
): boolean {
  const os = (opts.os ?? host.os_family).toLowerCase();
  if (entry.os_family.toLowerCase() !== os) return false;
  // A version-agnostic profile (no browser_major) carries hardware identity only and inherits
  // the engine's version, so it cannot contradict it — those pass. A profile that PINS a
  // version and gets it wrong is a genuine contradiction: the UA would claim 138 while the
  // engine's observable behaviour is 150. Those are rejected however well they score.
  if (entry.browser_major != null && entry.browser_major !== host.browser_major) return false;
  const max = opts.maxEncodedSize ?? DEFAULT_MAX_ENCODED;
  if (entry.encoded_size !== undefined && entry.encoded_size > max) return false;
  return true;
}

/** Stable 32-bit hash — used to map a stickiness key onto the candidate pool. */
function keyHash(key: string): number {
  return parseInt(createHash("sha256").update(key).digest("hex").slice(0, 8), 16);
}

export interface Selection {
  entry: ProfileIndexEntry;
  score: number;
  /** How many candidates survived the hard filters — useful for spotting an over-narrow index. */
  poolSize: number;
  mode: SelectMode;
}

/**
 * Pick a profile for this host.
 *
 * Throws when nothing is eligible rather than silently falling back to an incoherent profile —
 * a wrong persona is worse than no persona, because it converts a clean browser into a
 * contradictory one.
 */
export function selectProfile(
  index: ProfileIndexEntry[],
  host: HostFacts,
  opts: SelectOptions = {},
): Selection {
  const mode: SelectMode = opts.mode ?? "rotate";
  const candidates = index.filter((e) => eligible(e, host, opts));
  if (candidates.length === 0) {
    const os = opts.os ?? host.os_family;
    throw new Error(
      `no profile matches host (os=${os}, chromium major=${host.browser_major}). ` +
        `The index has ${index.length} entries; none passed the os/major/size filters. ` +
        `Sync a newer index, or pass an explicit fingerprintProfile.`,
    );
  }

  const scored = candidates
    .map((entry) => ({ entry, score: scoreProfile(entry, host) }))
    // tie-break by id so the ordering is deterministic across runs and machines
    .sort((a, b) => b.score - a.score || (a.entry.id < b.entry.id ? -1 : 1));

  const poolSize = scored.length;
  const n = Math.max(1, Math.min(opts.topN ?? DEFAULT_TOP_N, poolSize));
  const pool = scored.slice(0, n);

  let chosen: { entry: ProfileIndexEntry; score: number };
  if (mode === "best") {
    chosen = pool[0];
  } else if (mode === "random") {
    chosen = pool[Math.floor(Math.random() * pool.length)];
  } else {
    // "rotate": sticky per key. With no key the caller still gets a STABLE profile rather than a
    // new one each launch — an identity whose device changes every visit is a worse tell than a
    // slightly-suboptimal fixed one. Callers wanting spread should pass a key.
    const key = opts.key ?? defaultStickyKey();
    chosen = pool[keyHash(key) % pool.length];
  }

  return { entry: chosen.entry, score: chosen.score, poolSize, mode };
}

/** A stable per-machine key, so keyless `"rotate"` is consistent rather than random. */
let cachedStickyKey: string | null = null;
export function defaultStickyKey(): string {
  if (cachedStickyKey) return cachedStickyKey;
  const bits = [process.env.CLEARCOTE_PROFILE_KEY, process.env.USERNAME ?? process.env.USER, process.platform];
  cachedStickyKey = createHash("sha256").update(bits.filter(Boolean).join("|")).digest("hex").slice(0, 16);
  return cachedStickyKey;
}
