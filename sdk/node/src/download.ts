// Resolve the Clearcote browser binary: use an explicit path / env override if given, otherwise
// download the pinned release, verify its SHA-256 against the value baked into the SDK, extract
// it to a per-version cache, and return the path to chrome.exe. The hash check is mandatory —
// a mismatch throws and the partial download is deleted.

import { createHash } from "node:crypto";
import { createReadStream, createWriteStream, existsSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import extract from "extract-zip";
import { RELEASE } from "./release.js";

export interface DownloadOptions {
  /** Override the cache directory (default: per-OS user cache dir). */
  cacheDir?: string;
  /** Suppress progress logging. */
  quiet?: boolean;
}

function log(quiet: boolean | undefined, msg: string): void {
  if (!quiet) process.stderr.write(`[clearcote] ${msg}\n`);
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

async function downloadTo(url: string, dest: string, quiet: boolean | undefined): Promise<void> {
  const res = await fetch(url, { redirect: "follow" });
  if (!res.ok || !res.body) {
    throw new Error(`Clearcote download failed: HTTP ${res.status} ${res.statusText} for ${url}`);
  }
  const total = Number(res.headers.get("content-length")) || RELEASE.size;
  let seen = 0;
  let lastPct = -1;
  const progress = new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      seen += chunk.byteLength;
      const pct = Math.floor((seen / total) * 100);
      if (!quiet && pct !== lastPct && pct % 5 === 0) {
        lastPct = pct;
        process.stderr.write(`\r[clearcote] downloading ${pct}% (${(seen / 1e6).toFixed(0)}/${(total / 1e6).toFixed(0)} MB)`);
      }
      controller.enqueue(chunk);
    },
  });
  await pipeline(Readable.fromWeb(res.body.pipeThrough(progress) as any), createWriteStream(dest));
  if (!quiet) process.stderr.write("\n");
}

/**
 * Ensure the Clearcote binary is present and verified; return the path to chrome.exe.
 * Cached per release tag, so subsequent calls are instant.
 */
export async function ensureBinary(opts: DownloadOptions = {}): Promise<string> {
  const base = path.join(opts.cacheDir || defaultCacheRoot(), RELEASE.tag);
  const browserDir = path.join(base, "browser");
  const marker = path.join(base, ".verified");

  if (existsSync(marker)) {
    const cached = findFile(browserDir, "chrome.exe");
    if (cached) return cached;
  }

  mkdirSync(base, { recursive: true });
  const zipPath = path.join(base, RELEASE.asset);

  log(opts.quiet, `fetching Clearcote ${RELEASE.version} (${RELEASE.tag}, ~${(RELEASE.size / 1e6).toFixed(0)} MB)`);
  await downloadTo(RELEASE.url, zipPath, opts.quiet);

  log(opts.quiet, "verifying SHA-256");
  const got = await sha256File(zipPath);
  if (got.toLowerCase() !== RELEASE.sha256.toLowerCase()) {
    await rm(zipPath, { force: true });
    throw new Error(
      `Clearcote archive SHA-256 mismatch — refusing to use it.\n  expected ${RELEASE.sha256}\n  got      ${got}`
    );
  }

  log(opts.quiet, "extracting");
  await rm(browserDir, { recursive: true, force: true });
  await extract(zipPath, { dir: browserDir });

  const exe = findFile(browserDir, "chrome.exe");
  if (!exe) {
    throw new Error("Clearcote archive verified but chrome.exe was not found inside it.");
  }
  // defense in depth: verify the extracted exe too
  const exeHash = await sha256File(exe);
  if (exeHash.toLowerCase() !== RELEASE.exeSha256.toLowerCase()) {
    throw new Error(
      `Clearcote chrome.exe SHA-256 mismatch — refusing to use it.\n  expected ${RELEASE.exeSha256}\n  got      ${exeHash}`
    );
  }

  writeFileSync(marker, `${RELEASE.sha256}\n`);
  await rm(zipPath, { force: true }); // reclaim ~250 MB; keep only the extracted tree
  log(opts.quiet, `ready: ${exe}`);
  return exe;
}
