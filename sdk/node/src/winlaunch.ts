/**
 * Windows launch helpers: one reusable recovered copy per build, and removal of the temp
 * directories a launch that never started leaves behind.
 *
 * Why a cached build can refuse to start in place ("spawn UNKNOWN", Windows error 14001: "the
 * side-by-side configuration is incorrect"): chrome.exe's manifest names a private assembly, the
 * `<version>.manifest` beside it, and Windows resolves that assembly against the file system as the
 * system sees it. A process running inside an MSIX-packaged app (and everything it starts) has its
 * writes to %LOCALAPPDATA% redirected into that package's private store, so a build it downloaded
 * into the cache is visible to the process but not to that check, and every launch from the cache
 * fails. %TEMP% and the home directory are not redirected. Real-time antivirus still scanning a
 * freshly extracted chrome_elf.dll can produce the same error for a while.
 *
 * What used to happen: three in-place launches, then a fresh ~400 MB copy of the browser in
 * %TEMP%\clearcote-recover-<random> that nothing deleted. Each failed Playwright launch also leaked
 * its two temp directories (`playwright_chromiumdev_profile-*`, `playwright-artifacts-*`): Node's
 * `spawn` throws synchronously on that error, before Playwright registers their cleanup.
 *
 * Now: the copy lives at `~/.clearcote/recovered/<build>-<hash>/browser` and is reused by every later
 * launch of the same build (the Python SDK computes the same key, so both share it), and a failed
 * attempt has exactly its own temp directories removed. `clearcote clear-cache` deletes the
 * recovered copies too.
 */
import { createHash } from "node:crypto";
import {
  cpSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmdirSync, rmSync,
  statSync, writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";

export const RECOVERED_MARKER = ".clearcote-recovered.json";
const STALE_PARTIAL_MS = 3_600_000; // an unfinished copy older than this was abandoned (a killed launch)
const PROFILE_IN_LOG = /--user-data-dir=(.*?playwright_chromiumdev_profile-[A-Za-z0-9]{6})/;

export function recoverRoot(): string {
  return join(homedir(), ".clearcote", "recovered");
}

/**
 * `<build>-<hash>`: the cache build directory's name, and a hash of the source directory and the
 * exe's size and modification time, so a rebuilt or re-downloaded build gets a fresh copy. Must
 * match `recovery_key` in the Python SDK (_winlaunch.py) and `RecoveryKey` in the .NET SDK
 * (WinLaunch.cs). Throws if the exe is gone.
 */
export function recoveryKey(exe: string): string {
  const src = dirname(resolve(exe));
  const st = statSync(exe);
  const parent = basename(src).toLowerCase() === "browser" ? dirname(src) : src;
  const name = basename(parent).replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 60) || "browser";
  // os.path.normcase on Windows: lower case, backslashes (resolve() already gives backslashes there)
  const norm = process.platform === "win32" ? src.toLowerCase() : src;
  const ident = `${norm}|${st.size}|${Math.floor(st.mtimeMs)}`;
  return `${name}-${createHash("sha256").update(ident, "utf8").digest("hex").slice(0, 12)}`;
}

/** The exe inside this build's recovered copy when one is complete, else undefined. */
export function recoveredExe(exe: string): string | undefined {
  let d: string;
  try {
    d = join(recoverRoot(), recoveryKey(exe));
  } catch {
    return undefined;
  }
  const cand = join(d, "browser", basename(exe));
  return existsSync(join(d, RECOVERED_MARKER)) && existsSync(cand) ? cand : undefined;
}

/**
 * Delete a browser copy unless a browser is running from it. Returns true once it is gone.
 *
 * Windows refuses to delete an exe while any process runs from it, so the main exe goes first: if
 * that fails the copy is in use and is left alone; if it succeeds nothing can start from the copy
 * any more and the rest is removed. (Renaming the directory is no test: Windows allows it while a
 * program inside is running.)
 */
export function discardCopy(path: string): boolean {
  const browser = join(path, "browser");
  let names: string[] = [];
  try {
    names = readdirSync(browser).filter((n) => n.toLowerCase().endsWith(".exe"));
  } catch { /* no browser dir */ }
  names.sort((a, b) => Number(a.toLowerCase() !== "chrome.exe") - Number(b.toLowerCase() !== "chrome.exe"));
  for (const n of names) {
    try {
      rmSync(join(browser, n));
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== "ENOENT") return false;
    }
  }
  try {
    rmSync(path, { recursive: true, force: true, maxRetries: 2 });
  } catch { /* reported by the existence check */ }
  return !existsSync(path);
}

function ageMs(path: string): number {
  try {
    return Date.now() - statSync(path).mtimeMs;
  } catch {
    return 0;
  }
}

/**
 * Remove recovered copies nothing can use any more: their source build is gone or has changed (its
 * key no longer matches), plus copies abandoned half-way. Copies in use are skipped.
 */
export function sweepRecovered(root: string, keep?: string): void {
  let names: string[];
  try {
    names = readdirSync(root);
  } catch {
    return;
  }
  for (const name of names) {
    if (name === keep) continue;
    const path = join(root, name);
    try {
      if (!statSync(path).isDirectory()) continue;
    } catch {
      continue;
    }
    let stale: boolean;
    let sourceExe: string | undefined;
    try {
      const meta = JSON.parse(readFileSync(join(path, RECOVERED_MARKER), "utf8"));
      if (typeof meta.source === "string" && typeof meta.exe === "string") sourceExe = join(meta.source, meta.exe);
    } catch { /* no readable marker */ }
    if (sourceExe === undefined) {
      // a copy still being made, or abandoned when its launch was killed
      stale = ageMs(path) > STALE_PARTIAL_MS;
    } else {
      try {
        stale = recoveryKey(sourceExe) !== name;
      } catch {
        stale = true; // the build it was copied from is gone (clear-cache, or deleted by hand)
      }
    }
    if (stale) discardCopy(path);
  }
}

/**
 * Remove `clearcote-recover-*` copies older SDK versions left in the temp directory (one ~400 MB
 * browser per launch, never deleted). Recent ones and ones in use are left alone. Set
 * `CLEARCOTE_KEEP_RECOVER=1` to keep them for debugging.
 */
export function sweepTempRecoverDirs(keepMs = 60_000): void {
  if (process.env.CLEARCOTE_KEEP_RECOVER) return;
  const tmp = tmpdir();
  let names: string[];
  try {
    names = readdirSync(tmp);
  } catch {
    return;
  }
  for (const name of names) {
    if (!name.startsWith("clearcote-recover-")) continue;
    const path = join(tmp, name);
    try {
      if (statSync(path).isDirectory() && ageMs(path) > keepMs) discardCopy(path);
    } catch { /* vanished */ }
  }
}

/** `clearcote clear-cache`: delete every recovered copy. */
export function clearRecovered(): { removed: number; bytes: number; inUse: number } {
  const root = recoverRoot();
  const result = { removed: 0, bytes: 0, inUse: 0 };
  let names: string[];
  try {
    names = readdirSync(root).sort();
  } catch {
    return result;
  }
  for (const name of names) {
    const path = join(root, name);
    try {
      if (!statSync(path).isDirectory()) continue;
    } catch {
      continue;
    }
    const n = dirBytes(path);
    if (discardCopy(path)) {
      result.removed++;
      result.bytes += n;
    } else {
      result.inUse++;
    }
  }
  try {
    rmdirSync(root); // only when empty
  } catch { /* still holds something */ }
  return result;
}

function dirBytes(p: string): number {
  let total = 0;
  for (const e of readdirSync(p, { withFileTypes: true })) {
    const full = join(p, e.name);
    try {
      total += e.isDirectory() ? dirBytes(full) : statSync(full).size;
    } catch { /* vanished */ }
  }
  return total;
}

/**
 * Copy the build into `recoverRoot()` once and return the copy's exe; later launches reuse it.
 * Concurrent launches each copy into a `.partial-` directory and the first to finish wins the name;
 * the others discard theirs.
 */
export function makeRecoveredCopy(exe: string, warm?: (dir: string) => void): string {
  const src = dirname(resolve(exe));
  const root = recoverRoot();
  mkdirSync(root, { recursive: true });
  const key = recoveryKey(exe);
  sweepRecovered(root, key);
  sweepTempRecoverDirs();
  const ready = recoveredExe(exe);
  if (ready) return ready;
  const final = join(root, key);
  const part = mkdtempSync(join(root, `${key}.partial-`));
  let keepPart = false;
  try {
    cpSync(src, join(part, "browser"), { recursive: true });
    warm?.(join(part, "browser"));
    const st = statSync(exe);
    writeFileSync(join(part, RECOVERED_MARKER),
      JSON.stringify({ source: src, exe: basename(exe), size: st.size, mtimeMs: Math.floor(st.mtimeMs) }));
    try {
      renameSync(part, final);
    } catch {
      const other = recoveredExe(exe); // another launch finished its copy first
      if (other) return other;
      if (existsSync(final) && discardCopy(final)) { // an unmarked leftover held the name
        try {
          renameSync(part, final);
        } catch { /* still blocked */ }
      }
      if (existsSync(part)) {
        keepPart = true; // still blocked: launch from our own complete copy
        return join(part, "browser", basename(exe));
      }
    }
    return join(final, "browser", basename(exe));
  } finally {
    if (!keepPart && existsSync(part)) rmSync(part, { recursive: true, force: true, maxRetries: 2 });
  }
}

function birthMs(path: string): number {
  return statSync(path).birthtimeMs;
}

/**
 * Remove the two temp directories a Playwright launch leaves when its process never started.
 *
 * Node's spawn throws synchronously on "spawn UNKNOWN", before Playwright sets up the cleanup of the
 * directories it has just made, so every failed attempt leaked a `playwright-artifacts-*` and (for
 * launch()) a `playwright_chromiumdev_profile-*`. The profile is named in the error's call log. The
 * artifacts directory is not: it is the empty one made by this attempt (after `startedMs`) just
 * before that profile, or, for a persistent launch, the only one made by this attempt. Only empty
 * directories are removed, and nothing when the choice is ambiguous.
 */
export function removeFailedLaunchDirs(err: unknown, startedMs: number): void {
  const msg = String((err as Error)?.message ?? err);
  if (!msg.includes("<launching>")) return; // Playwright never got as far as starting the process
  let tmp: string | undefined;
  let anchor: number | undefined;
  const m = PROFILE_IN_LOG.exec(msg);
  if (m) {
    const prof = m[1];
    tmp = dirname(prof);
    try {
      anchor = birthMs(prof);
      rmdirSync(prof); // only succeeds while empty: the browser never wrote to it
    } catch { /* gone already, or not empty */ }
  }
  tmp ??= tmpdir();
  const now = Date.now();
  let found: Array<[number, string]> = [];
  let names: string[];
  try {
    names = readdirSync(tmp);
  } catch {
    return;
  }
  for (const name of names) {
    if (!name.startsWith("playwright-artifacts-")) continue;
    const path = join(tmp, name);
    try {
      const born = birthMs(path);
      if (born >= startedMs - 50 && born <= now + 50 && readdirSync(path).length === 0) found.push([born, path]);
    } catch { /* vanished */ }
  }
  let pick: string | undefined;
  if (anchor !== undefined) {
    found = found.filter(([born]) => born <= anchor! + 1).sort((a, b) => a[0] - b[0]);
    pick = found.length ? found[found.length - 1][1] : undefined;
  } else if (found.length === 1) {
    pick = found[0][1];
  }
  if (pick) {
    try {
      rmdirSync(pick);
    } catch { /* not empty any more */ }
  }
}
