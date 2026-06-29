// Opt-in Widevine CDM fetch + persistent-profile seeding (Windows).
//
// clearcote is a 100%-open-source build: it ships the EME/Widevine *plumbing* (enable_widevine=true)
// but NOT Google's proprietary CDM binary (that blob can't live in a FOSS package). This module lets
// a user *opt in* to fetching the CDM from Google's component server at runtime — exactly how a real
// Chrome receives it via the component updater — so requestMediaKeySystemAccess('com.widevine.alpha')
// resolves (DRM plays, and the EME surface matches a real Chrome instead of a no-Widevine tell).
//
//   import { fetchWidevine, launchPersistentContext } from "clearcote";
//   await fetchWidevine();                                  // download + verify into ~/.clearcote
//   const ctx = await launchPersistentContext("profile", { widevine: true });  // seeds + enables it

import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import extract from "extract-zip";

/** Chrome's component-updater app id for the Widevine CDM + Google's Omaha JSON endpoint. */
export const WIDEVINE_APP_ID = "oimompecagnajdejgnnjijobebaeigek";
export const OMAHA_URL = "https://update.googleapis.com/service/update2/json";
export const HINT_FILE = "latest-component-updated-widevine-cdm";
// update.googleapis.com sits behind Google's edge; a bare fetch UA can 403 — send a browser-ish one.
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36";

export interface WidevineOptions {
  /** Destination root (default ~/.clearcote/WidevineCdm). */
  dest?: string;
  /** Suppress progress logging. */
  quiet?: boolean;
}

function log(quiet: boolean | undefined, msg: string): void {
  if (!quiet) process.stderr.write(`[clearcote] [widevine] ${msg}\n`);
}

function cacheRoot(): string {
  return process.env.CLEARCOTE_WIDEVINE_DIR || path.join(os.homedir(), ".clearcote", "WidevineCdm");
}

/** Minimal Omaha v3.1 update check for the Windows x64 Widevine CDM (version 0.0.0.0 -> latest). */
export function omahaRequestBody(): unknown {
  return {
    request: {
      "@os": "win",
      "@updater": "clearcote",
      acceptformat: "crx3",
      protocol: "3.1",
      arch: "x64",
      nacl_arch: "x86-64",
      prodversion: "149.0.0.0",
      updaterversion: "149.0.0.0",
      dedup: "cr",
      os: { arch: "x86_64", platform: "Windows", version: "10.0.19045.0" },
      app: [{ appid: WIDEVINE_APP_ID, version: "0.0.0.0", updatecheck: {}, ping: { r: -2 } }],
    },
  };
}

async function postJson(url: string, body: unknown): Promise<any> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": UA },
    body: JSON.stringify(body),
    redirect: "follow",
    signal: AbortSignal.timeout(60_000),
  });
  if (!res.ok) throw new Error(`Widevine update check HTTP ${res.status}`);
  let raw = await res.text();
  if (raw.startsWith(")]}'")) raw = raw.includes("\n") ? raw.slice(raw.indexOf("\n") + 1) : raw.slice(4); // strip XSSI guard
  return JSON.parse(raw);
}

/** Pull [downloadUrl, sha256, version] from an Omaha JSON response (pipelines or classic shape). */
export function parseUpdate(resp: any): [string, string, string] {
  const app = resp?.response?.app?.[0];
  const uc = app?.updatecheck;
  if (!uc || uc.status !== "ok") throw new Error(`Widevine update check status: ${uc?.status}`);
  for (const pl of uc.pipelines || []) {
    for (const op of pl.operations || []) {
      const urls = op.urls || [];
      const out = op.out || {};
      if (urls.length && out.sha256) return [urls[0].url, out.sha256, app.nextversion || uc.nextversion || ""];
    }
  }
  const base = uc.urls?.url?.[0]?.codebase;
  const pkg = uc.manifest?.packages?.package?.[0];
  if (base && pkg?.name) return [base.replace(/\/+$/, "") + "/" + pkg.name, pkg.hash_sha256 || "", uc.manifest?.version || ""];
  throw new Error("could not find a CDM download URL in the update response");
}

/** A CRX3 file is 'Cr24' + u32 version + u32 headerLen + header + zip. Return the zip bytes. */
export function crx3ToZip(buf: Buffer): Buffer {
  if (buf.subarray(0, 4).toString("latin1") !== "Cr24") return buf; // already a plain zip
  if (buf.length < 12) throw new Error("malformed CRX3 (truncated header)");
  const headerLen = buf.readUInt32LE(8);
  if (12 + headerLen > buf.length) throw new Error("malformed CRX3 (header overruns buffer)");
  return buf.subarray(12 + headerLen);
}

/**
 * Download + verify the Windows x64 Widevine CDM into `dest` (default ~/.clearcote/WidevineCdm/
 * <version>). Returns the versioned CDM directory (manifest.json + _platform_specific/win_x64/
 * widevinecdm.dll). Re-fetch is skipped if already present.
 */
export async function fetchWidevine(opts: WidevineOptions = {}): Promise<string> {
  const [url, sha256, version] = parseUpdate(await postJson(OMAHA_URL, omahaRequestBody()));
  const root = opts.dest || cacheRoot();
  const verDir = path.join(root, version || "current");
  const dll = path.join(verDir, "_platform_specific", "win_x64", "widevinecdm.dll");
  if (existsSync(dll) && existsSync(path.join(verDir, "manifest.json"))) {
    log(opts.quiet, `already present: ${verDir}`);
    return verDir;
  }
  log(opts.quiet, `fetching CDM ${version || "(latest)"}`);
  const res = await fetch(url, { headers: { "User-Agent": UA }, redirect: "follow", signal: AbortSignal.timeout(120_000) });
  if (!res.ok) throw new Error(`Widevine CDM download HTTP ${res.status}`);
  const blob = Buffer.from(await res.arrayBuffer());
  // The CDM is a NATIVE DLL loaded into the browser process — never install it unverified. A
  // missing hash in the update response is a hard failure, not a skip.
  if (!sha256) throw new Error("Widevine update response had no sha256 — refusing to install an unverified CDM");
  if (createHash("sha256").update(blob).digest("hex") !== sha256.toLowerCase()) {
    throw new Error("Widevine CDM sha256 mismatch — refusing to install");
  }
  mkdirSync(verDir, { recursive: true });
  const tmp = mkdtempSync(path.join(os.tmpdir(), "cc-wv-"));
  const zipPath = path.join(tmp, "cdm.zip");
  try {
    writeFileSync(zipPath, crx3ToZip(blob));
    await extract(zipPath, { dir: verDir });
  } finally {
    await rm(tmp, { recursive: true, force: true });
  }
  if (!existsSync(dll)) throw new Error(`extracted CDM but widevinecdm.dll not at ${dll}`);
  log(opts.quiet, `installed: ${verDir}`);
  return verDir;
}

/**
 * Make a persistent profile load the fetched CDM: copy it under <userDataDir>/WidevineCdm/<version>/
 * and write the component hint file the engine reads. Fetches the CDM first if needed.
 */
export async function seedWidevine(userDataDir: string, opts: WidevineOptions = {}): Promise<string> {
  const src = await fetchWidevine(opts);
  const version = path.basename(src);
  const wvRoot = path.join(userDataDir, "WidevineCdm");
  const target = path.join(wvRoot, version);
  if (!existsSync(path.join(target, "_platform_specific", "win_x64", "widevinecdm.dll"))) {
    mkdirSync(wvRoot, { recursive: true });
    cpSync(src, target, { recursive: true });
  }
  try {
    writeFileSync(path.join(wvRoot, HINT_FILE), JSON.stringify({ Path: target }));
  } catch {
    /* hint file is Linux-only; harmless if it can't be written */
  }
  log(opts.quiet, `seeded into ${wvRoot}`);
  return target;
}

/**
 * Adjust launch args so the engine actually registers the seeded CDM: un-suppress the component
 * updater (Playwright disables it by default) and force the startup scan. Pure — returns the new
 * values. `ignoreDefaultArgs` may be a list, `undefined`, or the boolean form Playwright accepts
 * (`true` = ignore ALL defaults, so the updater is already un-suppressed); only the list/undefined
 * forms need `--disable-component-update` added. Verified: with these, requestMediaKeySystemAccess
 * + createMediaKeys succeed.
 */
export function widevineArgs(
  ignoreDefaultArgs: string[] | boolean | undefined,
  userArgs: string[],
): { ignoreDefaultArgs: string[] | boolean | undefined; args: string[] } {
  let ida: string[] | boolean | undefined = ignoreDefaultArgs;
  if (Array.isArray(ignoreDefaultArgs)) {
    ida = ignoreDefaultArgs.includes("--disable-component-update")
      ? ignoreDefaultArgs
      : [...ignoreDefaultArgs, "--disable-component-update"];
  } else if (ignoreDefaultArgs === undefined) {
    ida = ["--disable-component-update"];
  }
  const args = userArgs.some((a) => a.includes("component-updater"))
    ? userArgs
    : [...userArgs, "--component-updater=fast-update"];
  return { ignoreDefaultArgs: ida, args };
}
