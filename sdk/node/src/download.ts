// Resolve the Clearcote browser binary: use an explicit path / env override if given, otherwise
// download a release, verify it, extract it to a per-version cache, and return chrome.exe.
//
// Two modes:
//   • pinned (default)     — download the exact release baked into this SDK and verify its zip +
//                            chrome.exe against the SHA-256 hashes shipped in release.ts. The hash
//                            IS the trust anchor: you audit it once, in the package you installed.
//   • autoUpdate (opt-in)  — resolve the NEWEST GitHub release at runtime, verify the zip against
//                            that release's published SHA256SUMS.txt, and — when a `gpg` binary is
//                            available — verify SHA256SUMS.txt.asc against the pinned signing-key
//                            fingerprint. Lets you stay current without bumping the SDK. Falls back
//                            to the pinned release if GitHub is unreachable.
// A hash mismatch (either mode) is always a hard failure and the partial download is deleted.

import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmodSync, closeSync, createReadStream, createWriteStream, existsSync, mkdirSync, mkdtempSync, openSync, readSync, readdirSync, renameSync, writeFileSync } from "node:fs";
import { rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import extract from "extract-zip";
import { RELEASE, REPO, SIGNING_KEY_FPR, platformRelease, type ReleaseInfo } from "./release.js";

/**
 * Read every file under `dir` so on-access antivirus finishes scanning the freshly-extracted,
 * unsigned binaries BEFORE the browser is launched. Windows-only concern: launching a just-extracted
 * chrome.exe can race the real-time AV scan of chrome_elf.dll (the SxS assembly member the exe's
 * manifest depends on) — surfacing as "spawn UNKNOWN" / "side-by-side configuration is incorrect",
 * which Windows then caches against the path so every later launch from it keeps failing. Forcing a
 * sequential read here makes the scan happen up front and closes the race. Best-effort, safe anywhere.
 */
export function warmFiles(dir: string): void {
  const buf = Buffer.allocUnsafe(1 << 20);
  const walk = (d: string): void => {
    let entries;
    try {
      entries = readdirSync(d, { withFileTypes: true });
    } catch {
      return;
    }
    for (const ent of entries) {
      const p = path.join(d, ent.name);
      if (ent.isDirectory()) {
        walk(p);
        continue;
      }
      try {
        const fd = openSync(p, "r");
        try {
          while (readSync(fd, buf, 0, buf.length, null) > 0) {
            /* discard — the read forces the AV scan */
          }
        } finally {
          closeSync(fd);
        }
      } catch {
        /* best-effort */
      }
    }
  };
  walk(dir);
}

export interface DownloadOptions {
  /** Override the cache directory (default: per-OS user cache dir). */
  cacheDir?: string;
  /** Suppress progress logging. */
  quiet?: boolean;
  /**
   * Opt in to resolving and downloading the LATEST GitHub release instead of the version pinned
   * into this SDK, so you don't have to upgrade the package for every browser build. Verified
   * against the release's own SHA256SUMS.txt (+ GPG signature when `gpg` is installed). Falls back
   * to the pinned release if GitHub can't be reached. Default: false.
   * Also enabled by setting the env var `CLEARCOTE_AUTO_UPDATE=1`.
   */
  autoUpdate?: boolean;
}

/** A resolved release to fetch — either the pinned {@link RELEASE} or one discovered at runtime. */
interface ResolvedRelease extends ReleaseInfo {
  /** True when discovered via the GitHub API (un-pinned) rather than baked into the SDK. */
  unpinned: boolean;
  /** URL of SHA256SUMS.txt.asc (auto-update only) for optional GPG verification. */
  ascUrl?: string;
  /** URL of the public signing key (auto-update only). */
  keyUrl?: string;
}

function log(quiet: boolean | undefined, msg: string): void {
  if (!quiet) process.stderr.write(`[clearcote] ${msg}\n`);
}

function autoUpdateRequested(opt: boolean | undefined): boolean {
  if (opt !== undefined) return opt;
  const env = process.env.CLEARCOTE_AUTO_UPDATE;
  return env === "1" || env === "true";
}

function defaultCacheRoot(): string {
  if (process.env.CLEARCOTE_CACHE) return process.env.CLEARCOTE_CACHE;
  if (process.platform === "win32") {
    return path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "clearcote", "Cache");
  }
  if (process.platform === "darwin") {
    return path.join(os.homedir(), "Library", "Caches", "clearcote");
  }
  return path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "clearcote");
}

function findFile(dir: string, name: string): string | null {
  const stack = [dir];
  while (stack.length) {
    const cur = stack.pop() as string;
    let entries: import("node:fs").Dirent[];
    try {
      entries = readdirSync(cur, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const e of entries) {
      const full = path.join(cur, e.name);
      if (e.isDirectory()) stack.push(full);
      else if (e.name.toLowerCase() === name.toLowerCase()) return full;
    }
  }
  return null;
}

function sha256File(file: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const hash = createHash("sha256");
    createReadStream(file)
      .on("error", reject)
      .on("data", (d) => hash.update(d))
      .on("end", () => resolve(hash.digest("hex")));
  });
}

async function downloadTo(url: string, dest: string, expectedSize: number, quiet: boolean | undefined): Promise<void> {
  // Idle-timeout the stream: a 60s timer reset on every chunk aborts a stalled connection (no data
  // for 60s) without capping the total time of the large (~242 MB) binary download.
  const ctrl = new AbortController();
  const IDLE_MS = 60_000;
  let idle: ReturnType<typeof setTimeout> | undefined;
  const bump = () => {
    if (idle) clearTimeout(idle);
    idle = setTimeout(() => ctrl.abort(new Error("clearcote download stalled (no data for 60s)")), IDLE_MS);
  };
  bump();
  try {
    const res = await fetch(url, { redirect: "follow", signal: ctrl.signal });
    if (!res.ok || !res.body) {
      throw new Error(`Clearcote download failed: HTTP ${res.status} ${res.statusText} for ${url}`);
    }
    const total = Number(res.headers.get("content-length")) || expectedSize;
    let seen = 0;
    let lastPct = -1;
    const progress = new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        bump(); // data arrived -> reset the idle deadline
        seen += chunk.byteLength;
        const pct = total ? Math.floor((seen / total) * 100) : 0;
        if (!quiet && pct !== lastPct && pct % 5 === 0) {
          lastPct = pct;
          process.stderr.write(`\r[clearcote] downloading ${pct}% (${(seen / 1e6).toFixed(0)}/${(total / 1e6).toFixed(0)} MB)`);
        }
        controller.enqueue(chunk);
      },
    });
    await pipeline(Readable.fromWeb(res.body.pipeThrough(progress) as any), createWriteStream(dest));
    if (!quiet) process.stderr.write("\n");
  } finally {
    if (idle) clearTimeout(idle);
  }
}

async function fetchText(url: string): Promise<string> {
  // 30s total timeout for small API/text fetches so a stalled connection fails fast.
  const res = await fetch(url, { redirect: "follow", headers: { "User-Agent": "clearcote-sdk" }, signal: AbortSignal.timeout(30_000) });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return res.text();
}

async function fetchToFile(url: string, dest: string): Promise<void> {
  writeFileSync(dest, await fetchText(url));
}

/** Pull the archive + inner-binary hashes out of a SHA256SUMS.txt body. */
function parseSums(text: string, assetName: string, binary: string): { zip?: string; exe?: string } {
  const out: { zip?: string; exe?: string } = {};
  for (const raw of text.split(/\r?\n/)) {
    const m = raw.trim().match(/^([0-9a-fA-F]{64})\s+[*]?(.+)$/);
    if (!m) continue;
    const base = m[2].split(/[\\/]/).pop();
    if (base === assetName) out.zip = m[1].toLowerCase();
    else if (base === binary) out.exe = m[1].toLowerCase();
  }
  return out;
}

/** Escape a string for safe interpolation into a RegExp. */
function reEscape(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Resolve the newest non-draft GitHub release with THIS platform's asset + SHA256SUMS.txt. */
async function resolveLatest(quiet: boolean | undefined): Promise<ResolvedRelease | null> {
  const pin = platformRelease();
  if (!pin) return null; // unsupported OS -> nothing to auto-resolve
  const { assetGlob: glob, binary, os: pinOs, archive } = pin;
  const assetRe = new RegExp(`^clearcote-.*-${reEscape(glob)}\\.(?:zip|tar\\.xz)$`);
  const verRe = new RegExp(`^clearcote-(.+)-${reEscape(glob)}\\.(?:zip|tar\\.xz)$`);
  let list: any[];
  try {
    const res = await fetch(`https://api.github.com/repos/${REPO}/releases?per_page=30`, {
      redirect: "follow",
      headers: { "User-Agent": "clearcote-sdk", Accept: "application/vnd.github+json" },
      signal: AbortSignal.timeout(30_000),
    });
    if (!res.ok) throw new Error(`GitHub API HTTP ${res.status}`);
    list = (await res.json()) as any[];
  } catch (e) {
    log(quiet, `auto-update: couldn't reach GitHub (${(e as Error).message}); using pinned ${RELEASE.tag}`);
    return null;
  }
  const releases = list
    .filter((r) => r && !r.draft)
    .sort((a, b) => String(b.published_at || "").localeCompare(String(a.published_at || "")));
  for (const r of releases) {
    const assets: any[] = r.assets || [];
    const asset = assets.find((a) => assetRe.test(a.name));
    const sums = assets.find((a) => a.name === "SHA256SUMS.txt");
    if (!asset || !sums) continue;
    let parsed: { zip?: string; exe?: string };
    try {
      parsed = parseSums(await fetchText(sums.browser_download_url), asset.name, binary);
    } catch {
      continue;
    }
    if (!parsed.zip) continue;
    const m = asset.name.match(verRe);
    return {
      tag: r.tag_name,
      version: m ? m[1] : r.tag_name,
      asset: asset.name,
      url: asset.browser_download_url,
      sha256: parsed.zip,
      exeSha256: parsed.exe || "",
      size: asset.size || 0,
      os: pinOs,
      archive,
      binary,
      assetGlob: glob,
      unpinned: true,
      ascUrl: assets.find((a) => a.name === "SHA256SUMS.txt.asc")?.browser_download_url,
      keyUrl: assets.find((a) => a.name === "clearcote-signing-key.asc")?.browser_download_url,
    };
  }
  return null;
}

function hasGpg(): boolean {
  try {
    return spawnSync("gpg", ["--version"], { stdio: "ignore" }).status === 0;
  } catch {
    return false;
  }
}

/**
 * Best-effort GPG check for an auto-resolved release: import the published key into a throwaway
 * keyring, confirm its fingerprint equals the pinned one, then verify SHA256SUMS.txt.asc.
 * Returns "ok" | "skipped" (no gpg / no signature) | "failed". A "failed" result is treated as a
 * hard error by the caller; "skipped" proceeds (integrity is still guaranteed by the SHA-256 check).
 */
async function gpgVerifyAsync(
  rel: ResolvedRelease,
  sumsBody: string,
  tmp: string,
  quiet: boolean | undefined,
): Promise<"ok" | "skipped" | "failed"> {
  if (!rel.ascUrl || !rel.keyUrl) return "skipped";
  if (!hasGpg()) {
    log(quiet, "auto-update: gpg not found — skipping signature check (zip is still SHA-256-verified)");
    return "skipped";
  }
  const home = mkdtempSync(path.join(tmp, "ccgpg-"));
  const keyPath = path.join(home, "key.asc");
  const sumsPath = path.join(home, "SHA256SUMS.txt");
  const ascPath = path.join(home, "SHA256SUMS.txt.asc");
  const gpg = (args: string[]) => spawnSync("gpg", ["--homedir", home, "--batch", ...args], { encoding: "utf8" });
  try {
    writeFileSync(sumsPath, sumsBody);
    await fetchToFile(rel.keyUrl, keyPath);
    await fetchToFile(rel.ascUrl, ascPath);
    if (gpg(["--import", keyPath]).status !== 0) return "failed";
    const shown = gpg(["--with-colons", "--fingerprint"]);
    const fprs = (shown.stdout || "")
      .split(/\r?\n/)
      .filter((l) => l.startsWith("fpr:"))
      .map((l) => l.split(":")[9]);
    if (!fprs.includes(SIGNING_KEY_FPR)) {
      log(quiet, `auto-update: signing key fingerprint mismatch (expected ${SIGNING_KEY_FPR})`);
      return "failed";
    }
    const verified = gpg(["--verify", ascPath, sumsPath]);
    return verified.status === 0 ? "ok" : "failed";
  } catch {
    return "failed";
  } finally {
    await rm(home, { recursive: true, force: true });
  }
}

/** Download + verify a resolved release into `base`, returning the extracted browser-binary path. */
async function fetchAndVerify(rel: ResolvedRelease, base: string, opts: DownloadOptions): Promise<string> {
  const browserDir = path.join(base, "browser");
  mkdirSync(base, { recursive: true });
  const zipPath = path.join(base, rel.asset);

  log(opts.quiet, `fetching Clearcote ${rel.version} (${rel.tag}${rel.unpinned ? ", latest" : ""}, ~${(rel.size / 1e6).toFixed(0)} MB)`);
  await downloadTo(rel.url, zipPath, rel.size, opts.quiet);

  log(opts.quiet, "verifying SHA-256");
  const got = await sha256File(zipPath);
  if (got.toLowerCase() !== rel.sha256.toLowerCase()) {
    await rm(zipPath, { force: true });
    throw new Error(`Clearcote archive SHA-256 mismatch — refusing to use it.\n  expected ${rel.sha256}\n  got      ${got}`);
  }

  // For an auto-resolved (un-pinned) release, also confirm authenticity via the signed checksum file.
  if (rel.unpinned && rel.ascUrl) {
    const sumsBody = await fetchText(`https://github.com/${REPO}/releases/download/${rel.tag}/SHA256SUMS.txt`).catch(() => "");
    if (sumsBody) {
      const verdict = await gpgVerifyAsync(rel, sumsBody, base, opts.quiet);
      if (verdict === "failed") {
        await rm(zipPath, { force: true });
        throw new Error(
          `Clearcote ${rel.tag}: GPG signature verification FAILED against the pinned key ${SIGNING_KEY_FPR} — refusing to use it.`,
        );
      }
      if (verdict === "ok") log(opts.quiet, `auto-update: GPG signature OK (key ${SIGNING_KEY_FPR})`);
    }
  }

  log(opts.quiet, "extracting");
  await rm(browserDir, { recursive: true, force: true });
  // Extract to a sibling temp dir, then atomically move it into place so `browser/` only ever
  // appears once fully written (no partial tree a launch could race), and — on Windows — we can
  // pre-scan the finished tree before any launch (below).
  const incoming = path.join(base, ".incoming");
  await rm(incoming, { recursive: true, force: true });
  mkdirSync(incoming, { recursive: true });
  if (rel.asset.endsWith(".tar.xz") || rel.archive === "tar.xz") {
    // Node has no stdlib xz; the system `tar` (always present on Linux) auto-detects .xz.
    execFileSync("tar", ["-xf", zipPath, "-C", incoming]);
  } else {
    await extract(zipPath, { dir: incoming });
  }
  renameSync(incoming, browserDir);

  const binaryName = rel.binary || "chrome.exe";
  const exe = findFile(browserDir, binaryName);
  if (!exe) throw new Error(`Clearcote archive verified but ${binaryName} was not found inside it.`);
  if (rel.exeSha256) {
    const exeHash = await sha256File(exe);
    if (exeHash.toLowerCase() !== rel.exeSha256.toLowerCase()) {
      throw new Error(`Clearcote ${binaryName} SHA-256 mismatch — refusing to use it.\n  expected ${rel.exeSha256}\n  got      ${exeHash}`);
    }
  }

  if (process.platform !== "win32") {
    // Make the launcher executable (tar preserves 0755, but be defensive) + a best-effort setuid on
    // the sandbox helper. The setuid bit only takes effect if chrome-sandbox is root-owned; in
    // containers/non-root, pass --no-sandbox (see docs). We never require root here.
    try {
      chmodSync(exe, 0o755);
    } catch {
      /* best-effort */
    }
    const sandbox = path.join(path.dirname(exe), "chrome-sandbox");
    if (existsSync(sandbox)) {
      try {
        chmodSync(sandbox, 0o4755);
      } catch {
        /* best-effort — never require root */
      }
    }
  }

  if (process.platform === "win32") {
    // Pre-scan so real-time AV finishes with the freshly-extracted binaries before the first launch
    // — closes the chrome_elf.dll scan race that otherwise poisons the path (see warmFiles).
    warmFiles(browserDir);
  }
  writeFileSync(path.join(base, ".verified"), `${rel.sha256}\n`);
  await rm(zipPath, { force: true }); // reclaim ~250 MB; keep only the extracted tree
  log(opts.quiet, `ready: ${exe}`);
  return exe;
}

/** Options for {@link proEnsureBinary}. */
export interface ProDownloadOptions {
  /** License API base (default: `CLEARCOTE_LICENSE_API` env or clearcotelabs.com). */
  apiBase?: string;
  /** Override the cache directory (default: per-OS user cache dir). */
  cacheDir?: string;
  /** Suppress progress logging. */
  quiet?: boolean;
}

/**
 * Download + verify the PRO (license-gated) browser and return its chrome path.
 *
 * The PRO build is not on a public releases page: the SDK asks the site for it via
 * `GET /api/v1/download/pro` with the license key, gets back an unguessable, short-lived
 * blob URL + sha256, then reuses the SAME verify+extract path as the free binary
 * ({@link fetchAndVerify}, sha256-only — no GPG, exactly like the free pin). Cached per PRO
 * tag. Throws on any failure — a licensed caller must get the PRO build, never a silent free
 * fall-back (that would launch a binary the engine gate then refuses).
 */
export async function proEnsureBinary(licenseKey: string, opts: ProDownloadOptions = {}): Promise<string> {
  const baseUrl = (opts.apiBase || process.env.CLEARCOTE_LICENSE_API || "https://www.clearcotelabs.com").replace(/\/$/, "");
  const plat = process.platform === "win32" ? "windows" : process.platform === "linux" ? "linux" : null;
  if (!plat) throw new Error("Clearcote PRO ships Windows x64 and Linux x64 only.");

  const res = await fetch(`${baseUrl}/api/v1/download/pro?platform=${plat}`, {
    redirect: "follow",
    headers: { authorization: `Bearer ${licenseKey}`, "User-Agent": "clearcote-sdk" },
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) {
    const body = (await res.text().catch(() => "")).slice(0, 200);
    throw new Error(
      `Clearcote PRO download not authorized (HTTP ${res.status}): ${body}\n` +
        "Check your license key and that your plan is active.",
    );
  }
  const meta = (await res.json()) as {
    tag?: string; version?: string; asset?: string; archive?: string; binary?: string;
    url?: string; sha256?: string; exe_sha256?: string; size?: number;
  };
  if (!meta.url || !meta.sha256) {
    throw new Error(`Clearcote PRO build is not currently available for ${plat} (the server returned no download).`);
  }

  const rel: ResolvedRelease = {
    tag: meta.tag || `pro-${meta.version || ""}`,
    version: meta.version || "",
    asset: meta.asset || `clearcote-pro-${meta.version || ""}-${plat}-x64.${plat === "windows" ? "zip" : "tar.xz"}`,
    url: meta.url,
    sha256: meta.sha256,
    exeSha256: meta.exe_sha256 || "",
    size: meta.size || 0,
    os: process.platform,
    archive: (meta.archive as "zip" | "tar.xz") || (plat === "windows" ? "zip" : "tar.xz"),
    binary: meta.binary || (plat === "windows" ? "chrome.exe" : "chrome"),
    assetGlob: `${plat}-x64`,
    unpinned: false, // pinned -> sha256-only verify (no GPG), like the free pin
  };

  const base = path.join(opts.cacheDir || defaultCacheRoot(), rel.tag);
  if (existsSync(path.join(base, ".verified"))) {
    const cached = findFile(path.join(base, "browser"), rel.binary || "chrome.exe");
    if (cached) return cached;
  }
  return fetchAndVerify(rel, base, { cacheDir: opts.cacheDir, quiet: opts.quiet });
}

/**
 * Ensure the Clearcote binary is present and verified; return the path to chrome.exe.
 * Cached per release tag, so subsequent calls are instant.
 */
export async function ensureBinary(opts: DownloadOptions = {}): Promise<string> {
  const cacheRoot = opts.cacheDir || defaultCacheRoot();

  let rel: ResolvedRelease;
  if (autoUpdateRequested(opts.autoUpdate)) {
    const latest = await resolveLatest(opts.quiet);
    if (latest && latest.tag === RELEASE.tag) {
      // newest release IS the pinned one — prefer the baked-in hashes (the audited trust anchor).
      rel = { ...RELEASE, unpinned: false };
    } else {
      rel = latest ?? { ...RELEASE, unpinned: false };
    }
  } else {
    rel = { ...RELEASE, unpinned: false };
  }

  const base = path.join(cacheRoot, rel.tag);
  if (existsSync(path.join(base, ".verified"))) {
    const cached = findFile(path.join(base, "browser"), rel.binary || "chrome.exe");
    if (cached) return cached;
  }
  return fetchAndVerify(rel, base, opts);
}
