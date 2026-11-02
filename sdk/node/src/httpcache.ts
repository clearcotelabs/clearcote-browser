// A shared HTTP cache for runs that start from a fresh profile (`httpCacheDir`).
//
// A fresh profile starts with an empty disk cache, so every run downloads the site's scripts, styles and
// images again through the proxy. Pointing Chromium's --disk-cache-dir at a folder that outlives the run keeps
// those files while cookies and storage stay new. Measured on r56 (11-02, github.com + bbc.com/news through a
// counting proxy): about 8 MB of proxy traffic per run fresh, 0.36-0.44 MB once the cache was warm.
//
// Chromium locks its cache: of several browsers on one cache folder at once, only the first gets it, the
// others run on an empty in-memory cache (measured: 0.29 / 8.08 / 8.10 MB). So the folder is split into slots
// (<dir>/slot-0, slot-1, ...) and each launch takes the first one no other live launch holds, marked by
// <dir>/slot-N.lock holding the owner's pid. A lock whose process is gone is taken over. Python
// (_httpcache.py) and .NET (HttpCache.cs) use the same layout, so the three can share one folder.
//
// Only a launch with a profile has a disk cache: incognito keeps it in memory (measured: --disk-cache-dir on an
// incognito launch left Cache_Data empty and saved nothing), so a launch without a profile refuses the option.

import { closeSync, mkdirSync, openSync, readFileSync, rmSync, writeSync } from "node:fs";
import { join, resolve } from "node:path";
import { warnOnce } from "./warnings.js";

/** How many slots one folder offers: the most browsers that can share it at once. */
export const HTTP_CACHE_SLOTS = 64;

/** A slot held by this process: `dir` goes to --disk-cache-dir, `release()` frees it (idempotent). */
export interface HttpCacheSlot {
  dir: string;
  release(): void;
}

function alive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === "EPERM"; // exists, owned by someone else
  }
}

function tryLock(file: string): boolean {
  try {
    const fd = openSync(file, "wx");
    try { writeSync(fd, String(process.pid)); } finally { closeSync(fd); }
    return true;
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
    return false;
  }
}

/** Hold the first free slot of `root`, creating the folder. Throws when the folder cannot be created;
 *  returns null (with one warning) when every slot is held by a live process. */
export function acquireHttpCacheSlot(root: string, quiet?: boolean): HttpCacheSlot | null {
  const base = resolve(root);
  mkdirSync(base, { recursive: true });
  for (let n = 0; n < HTTP_CACHE_SLOTS; n++) {
    const lock = join(base, `slot-${n}.lock`);
    let held = tryLock(lock);
    if (!held) {
      let owner = NaN;
      try { owner = Number(readFileSync(lock, "utf8").trim()); } catch { /* released meanwhile */ }
      if (alive(owner)) continue;
      rmSync(lock, { force: true }); // a crashed run's lock
      held = tryLock(lock);
      if (!held) continue; // another launch took it over first
    }
    const dir = join(base, `slot-${n}`);
    mkdirSync(dir, { recursive: true });
    let done = false;
    const release = () => {
      if (done) return;
      done = true;
      process.removeListener("exit", release);
      try { if (readFileSync(lock, "utf8").trim() === String(process.pid)) rmSync(lock, { force: true }); } catch { /* gone */ }
    };
    process.once("exit", release);
    return { dir, release };
  }
  warnOnce("http-cache-full", `httpCacheDir: all ${HTTP_CACHE_SLOTS} slots of ${base} are in use; this launch runs without the shared cache`, quiet);
  return null;
}

/** A launch without a profile (incognito) keeps its HTTP cache in memory, so a cache folder would do nothing. */
export const HTTP_CACHE_NEEDS_PROFILE =
  "httpCacheDir needs a profile: a launch without one (ephemeralProfile: false) keeps its HTTP cache in memory. " +
  "Leave ephemeralProfile on (the default), or use launchPersistentContext() or serve()";

/** Validate `httpCacheDir` against the caller's own args (before anything starts). */
export function checkHttpCacheDir(httpCacheDir: unknown, args: readonly string[] | undefined): string | undefined {
  if (httpCacheDir === undefined || httpCacheDir === null) return undefined;
  if (typeof httpCacheDir !== "string" || !httpCacheDir.trim()) throw new TypeError("httpCacheDir must be a folder path");
  if ((args ?? []).some((a) => /^--disk-cache-dir(=|$)/.test(a))) {
    throw new TypeError("httpCacheDir and an explicit --disk-cache-dir in args cannot be combined; pass one of them");
  }
  return httpCacheDir;
}

/**
 * Take a slot for a launch whose options may carry `httpCacheDir`: the options come back without it and with
 * --disk-cache-dir=<slot> appended to `args`, plus the release to call once the browser has exited.
 */
export function withHttpCache<T extends { httpCacheDir?: string; args?: string[]; quiet?: boolean }>(
  options: T,
): { options: Omit<T, "httpCacheDir">; release: () => void } {
  const { httpCacheDir, ...rest } = options;
  const root = checkHttpCacheDir(httpCacheDir, rest.args);
  if (root === undefined) return { options: rest, release: () => {} };
  const slot = acquireHttpCacheSlot(root, rest.quiet);
  if (!slot) return { options: rest, release: () => {} };
  return { options: { ...rest, args: [...(rest.args ?? []), `--disk-cache-dir=${slot.dir}`] }, release: slot.release };
}
