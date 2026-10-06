// `profile: "auto"` — resolve a persona from the best source available.
//
// ORDER: the licensed clearcote service first, a local imported directory as backup.
//
// The service is preferred because its corpus is fresher, larger and curated. But it can be
// unreachable for reasons that are none of the caller's fault — no license, an exhausted daily
// budget, a rate limit, a network blip, an outage — and in every one of those cases a local
// profile is enormously better than no persona. So the fallback is by design, not a patch.
//
// THE FALLBACK IS ALWAYS ANNOUNCED. A silent downgrade would leave a caller believing they are
// on a fresh service profile while they are on a stale local one, and the whole product is about
// knowing exactly what identity you are presenting. `source` is returned on every result and a
// one-line reason is written to stderr unless `quiet`.

import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { PROFILE_DIR } from "./profile.js";
import { importDirectory, loadImportedProfile } from "./profileimport.js";
import { selectProfile, type HostFacts, type ProfileIndexEntry, type Selection } from "./profilelib.js";
import { fetchIndex, fetchProfile, type ProfileSourceOptions } from "./profilesource.js";
import { ensureLocalProfiles, hasLocalProfiles } from "./profilefetch.js";

/** Where an imported (e.g. converted chrome-fingerprints) directory is looked for by default. */
export const DEFAULT_LOCAL_DIR =
  process.env.CLEARCOTE_PROFILE_SOURCE ?? join(homedir(), ".clearcote", "profiles", "imported");

export type ProfileOrigin = "service" | "local";

/**
 * First engine major that implements `--fingerprint-profile`.
 *
 * BACKWARDS COMPATIBILITY, AND WHY THIS IS A HARD GATE. The free 149 build ships a 15-patch
 * stack with no persona-profile patch at all, so it does not merely apply the profile badly —
 * it ignores the switch entirely. Chromium discards unknown switches silently, so a 149 user
 * who asked for a profile would launch with NO persona while believing they had one. That is
 * strictly worse than the seed farbling 149 does support, because the failure is invisible.
 *
 * So a profile is never sent to an engine that cannot read it. Callers on such an engine fall
 * back to the seed path, which is what 149 was built for.
 */
export const MIN_PROFILE_ENGINE_MAJOR = 150;

/** Whether this engine can actually apply an imported profile. */
export function engineSupportsProfiles(browserMajor: number): boolean {
  return Number.isFinite(browserMajor) && browserMajor >= MIN_PROFILE_ENGINE_MAJOR;
}

export interface AutoOptions extends ProfileSourceOptions {
  /** Directory of clearcote-profile JSON used when the service is unavailable. */
  localDir?: string;
  /** Skip the service entirely and use the local directory. */
  preferLocal?: boolean;
  /** Set `false` to never bootstrap a local library on the fallback path. Default: allowed. */
  autoDownload?: boolean;
  /** How many profiles the first-run bootstrap converts (default 500). */
  autoDownloadCount?: number;
  /** Path to convert_dataset.py, for the bootstrap. Defaults to this checkout's copy, if any. */
  converter?: string;
  /** Python executable used by the bootstrap. */
  python?: string;
  quiet?: boolean;
}

export interface AutoResult {
  profile: Record<string, unknown>;
  selection: Selection;
  source: ProfileOrigin;
  /** Why the service was not used, when `source` is "local". */
  fallbackReason?: string;
}

// --- local index cache ------------------------------------------------------
// Indexing a directory means reading every file (10k profiles is a few seconds of I/O), which is
// far too slow to repeat per launch. The cache is keyed by directory and invalidated by its
// mtime, so adding or converting more profiles is picked up without a manual step.

interface CachedLocalIndex { dir: string; mtimeMs: number; index: ProfileIndexEntry[]; }

function localCachePath(): string {
  const d = join(PROFILE_DIR, "library");
  if (!existsSync(d)) mkdirSync(d, { recursive: true });
  return join(d, "local-index.json");
}

export function loadLocalIndex(dir: string): ProfileIndexEntry[] {
  const mtimeMs = statSync(dir).mtimeMs;
  const path = localCachePath();
  if (existsSync(path)) {
    try {
      const c = JSON.parse(readFileSync(path, "utf8")) as CachedLocalIndex;
      if (c.dir === dir && c.mtimeMs === mtimeMs) return c.index;
    } catch {
      /* fall through and re-index */
    }
  }
  const { index } = importDirectory(dir);
  try {
    writeFileSync(path, JSON.stringify({ dir, mtimeMs, index } satisfies CachedLocalIndex));
  } catch {
    /* a cache write failure must never fail a launch */
  }
  return index;
}

function note(quiet: boolean | undefined, msg: string): void {
  if (!quiet) process.stderr.write(`[clearcote] [profile] ${msg}\n`);
}

/** The optional-backup tip is worth saying once, not on every launch. */
let hintedThisProcess = false;

/** Test seam: reset the once-per-process tip. */
export function _resetHint(): void {
  hintedThisProcess = false;
}

/**
 * How to populate a local backup directory. Printed when the service is unavailable and no local
 * directory exists — the moment the caller actually needs it.
 *
 * OPTIONAL, AND NOT SOMETHING WE SHIP. Clearcote redistributes no third-party fingerprints; the
 * dataset is fetched by the user from its own repository under its own terms, and we only read
 * the converted format. Any directory of clearcote-profile JSON works just as well, which is
 * why this is phrased as one example rather than the required path.
 */
export function localSetupHint(dir: string): string {
  return [
    `A local profile directory is optional, and gives "auto" a backup when the service is`,
    `unavailable. Any folder of clearcote-profile JSON works — your own captures, or a`,
    `third-party dataset you convert yourself. For example, using the open chrome-fingerprints`,
    `dataset (fetched by you, from its own repo, under its own licence):`,
    ``,
    `    pip install chrome-fingerprints`,
    `    python tools/fingerprint-collect/convert_dataset.py --out "${dir}" --count 500`,
    ``,
    `Or capture your own with tools/fingerprint-collect (collect.html), or point`,
    `CLEARCOTE_PROFILE_SOURCE at a directory you already have.`,
  ].join("\n");
}

/** Resolve a persona from the local imported directory. */
export function resolveLocal(host: HostFacts, opts: AutoOptions = {}): AutoResult {
  const dir = opts.localDir ?? DEFAULT_LOCAL_DIR;
  if (!existsSync(dir)) {
    throw new Error(`no local profile directory at ${dir}\n\n${localSetupHint(dir)}`);
  }
  const index = loadLocalIndex(dir);
  if (index.length === 0) {
    throw new Error(`local profile directory ${dir} contains no usable clearcote-profile JSON`);
  }
  const selection = selectProfile(index, host, opts);
  return { profile: loadImportedProfile(dir, selection.entry.id), selection, source: "local" };
}

/**
 * Resolve `profile: "auto"`: service first, local directory as backup.
 *
 * Any service failure falls back — including 401 (no license) and 429 (rate limited or daily
 * distinct budget exhausted), because in all of those a local persona is better than none and
 * far better than a hard launch failure.
 */
export async function resolveAuto(host: HostFacts, opts: AutoOptions = {}): Promise<AutoResult> {
  if (opts.preferLocal) {
    const r = resolveLocal(host, opts);
    note(opts.quiet, `using local profile ${r.selection.entry.id} (preferLocal)`);
    return r;
  }

  try {
    const index = await fetchIndex(host, opts);
    if (index.length === 0) throw new Error("service returned no candidates for this host");
    const selection = selectProfile(index, host, opts);
    const profile = await fetchProfile(selection.entry.id, opts);
    note(
      opts.quiet,
      `using service profile ${selection.entry.id} ` +
        `(score ${selection.score.toFixed(2)}, pool ${selection.poolSize}, mode ${selection.mode})`,
    );
    // Mention the optional backup ONCE per process, and only while there is none configured.
    // The useful moment to learn about a fallback is before you need it — but repeating it on
    // every launch would be noise, and nagging someone who already set one up would be wrong.
    if (!hintedThisProcess && !existsSync(opts.localDir ?? DEFAULT_LOCAL_DIR)) {
      hintedThisProcess = true;
      note(opts.quiet, `tip: no local backup directory configured. If the service is ever rate limited or
unreachable, "auto" fails rather than falling back. Setting one up is optional:
${localSetupHint(opts.localDir ?? DEFAULT_LOCAL_DIR)}`);
    }
    return { profile, selection, source: "service" };
  } catch (serviceErr) {
    const reason = String(serviceErr instanceof Error ? serviceErr.message : serviceErr).slice(0, 200);
    // First-run bootstrap: if there is no local library yet, try to build one before giving up.
    // Only on the fallback path — a working service must never pay for this. Never throws: a
    // bootstrap failure has to degrade to "no local backup", not take down the launch.
    if (opts.autoDownload !== false) {
      const dir = opts.localDir ?? DEFAULT_LOCAL_DIR;
      if (!hasLocalProfiles(dir)) {
        await ensureLocalProfiles(dir, join(PROFILE_DIR, "library"), {
          quiet: opts.quiet,
          count: opts.autoDownloadCount,
          converter: opts.converter,
          python: opts.python,
        }).catch(() => false);
      }
    }
    try {
      const r = resolveLocal(host, opts);
      // Both halves of the story: what failed, and what is being used instead.
      note(opts.quiet, `service unavailable (${reason}) — falling back to local profile ${r.selection.entry.id}`);
      return { ...r, fallbackReason: reason };
    } catch (localErr) {
      const dir = opts.localDir ?? DEFAULT_LOCAL_DIR;
      const noLocalDir = !existsSync(dir);
      const lr = String(localErr instanceof Error ? localErr.message : localErr);
      // Report BOTH failures. Surfacing only the local one would send the caller to go and fix
      // a directory when the real problem was an expired license.
      throw new Error(
        `profile: "auto" could not resolve a persona.\n` +
          `  service: ${reason}\n` +
          `  local:   ${noLocalDir ? `no directory at ${dir}` : lr}\n\n` +
          (noLocalDir ? localSetupHint(dir) : ""),
      );
    }
  }
}

/** Families every Windows 11 install has (Microsoft's Windows 11 font list marks them "Added in
 * Windows 11"). The capture collector never probed them, so no corpus donor carries them; a
 * Windows 11 donor would otherwise hide fonts no Windows 11 machine lacks. 87% of donors DO list
 * HoloLens MDL2 Assets, and CreepJS reads that set without Segoe Fluent Icons as Windows 10, which
 * it flags against a Windows 11 platformVersion. Cascadia Code/Mono are deliberately absent: they
 * ship per user with Windows Terminal. So is the bare "Segoe UI Variable": genuine Chrome 154 on
 * Windows 11 reports that family absent (only the Display/Small/Text faces resolve). Listing a family
 * never over-claims on a Windows host (host ∩ list). */
export const WINDOWS11_FACES = [
  "Segoe Fluent Icons", "Segoe UI Variable Display", "Segoe UI Variable Small", "Segoe UI Variable Text",
] as const;

/** True when a UA-CH platformVersion claims Windows 11 (major >= 13). */
export function claimsWindows11(platformVersion: unknown): boolean {
  const head = String(platformVersion ?? "").trim().split(".")[0];
  return /^\d+$/.test(head) && Number(head) >= 13;
}

/** A captured profile whose fonts gain {@link WINDOWS11_FACES} when it claims Windows 11. For
 * profiles the SDK picked itself (`profile: "auto"`); returns the input unchanged otherwise, and a
 * copy when it changes anything, so a cached corpus entry is never mutated. */
export function profileWithWindows11Faces<T>(profile: T): T {
  if (!profile || typeof profile !== "object") return profile;
  const p = profile as Record<string, any>;
  const nav = p.navigator && typeof p.navigator === "object" ? p.navigator : {};
  const uadata = nav.uadata && typeof nav.uadata === "object" ? nav.uadata : {};
  const hints = uadata.high_entropy && typeof uadata.high_entropy === "object" ? uadata.high_entropy : {};
  const platform = String(hints.platform || uadata.platform || nav.platform || "");
  const fonts = p.fonts && typeof p.fonts === "object" ? p.fonts : undefined;
  const detected = fonts?.detected;
  if (!platform.toLowerCase().startsWith("win") || !Array.isArray(detected) || !claimsWindows11(hints.platformVersion)) {
    return profile;
  }
  const have = new Set(detected.map((f: unknown) => String(f).toLowerCase()));
  const added = WINDOWS11_FACES.filter((f) => !have.has(f.toLowerCase()));
  return { ...p, fonts: { ...fonts, detected: [...detected, ...added] } } as T;
}
