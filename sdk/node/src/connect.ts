// `clearcote connect [CODE]`: start the Clearcote Helper, so the web profile manager at
// https://app.clearcotelabs.com can use this computer.
//
// The helper is its own open-source, reproducible program (clearcote-profile-manager/helper). This
// does not copy it: it downloads the release pinned below, checks the archive against the pinned
// SHA-256 (the same hashes as that release's SHA256SUMS), unpacks it once into the cache, and runs
// it with the Node.js runtime that ships inside it. The CODE is the one the web page shows: started
// with it, the helper pairs that page without asking again (typing it here is the approval).
//
// Kept field-for-field in step with HELPER in the Python SDK's _connect.py (test/connect.test.ts checks).

import { spawn, execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { createReadStream, existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { promisify } from "node:util";
import extract from "extract-zip";
import { defaultCacheRoot, downloadTo, renameDir } from "./download.js";

export interface HelperAsset {
  asset: string;
  sha256: string;
  size: number;
}

/** The helper release this SDK runs. */
export const HELPER = {
  version: "0.24.0",
  baseUrl: "https://github.com/clearcotelabs/clearcote-profile-manager/releases/download/helper-v0.24.0/",
  assets: {
    "win-x64": { asset: "clearcote-helper-0.24.0-win-x64.zip", sha256: "68524cfdebf3883d320d631b623d4275c0ccb1aafc89ad0b6af2c256c68e2366", size: 36271294 },
    "win-arm64": { asset: "clearcote-helper-0.24.0-win-arm64.zip", sha256: "c84b092bd86c0ce4f33695e41af6e03495361ecc4f0908ab107be52164b98801", size: 32315906 },
    "mac-x64": { asset: "clearcote-helper-0.24.0-mac-x64.tar.gz", sha256: "df97912b603db3029410fba49358c56e0e36468c1bd0f99ea78cd5d3715f0e6e", size: 41674553 },
    "mac-arm64": { asset: "clearcote-helper-0.24.0-mac-arm64.tar.gz", sha256: "248960e09678c199618217bdef7e77d1e9700c9f544672e4805f58c8740d4fc5", size: 40572955 },
    "linux-x64": { asset: "clearcote-helper-0.24.0-linux-x64.tar.gz", sha256: "f49ffccfee74e72e159778703418a8079deb01f3ad17982e0f221b55966bc19e", size: 45835993 },
    "linux-arm64": { asset: "clearcote-helper-0.24.0-linux-arm64.tar.gz", sha256: "116c1ff310248468f58ea527c5f13b6f30212f42fd74541f963a07c44f0bbe09", size: 45779083 },
  } as Record<string, HelperAsset>,
};

/** Where the web profile manager lives. */
export const APP_URL = "https://app.clearcotelabs.com";

/** "win-x64" etc. for this machine, or null where there is no helper build. */
export function helperPlatform(platform: string = process.platform, arch: string = process.arch): string | null {
  const os = platform === "win32" ? "win" : platform === "darwin" ? "mac" : platform === "linux" ? "linux" : null;
  const cpu = arch === "x64" ? "x64" : arch === "arm64" ? "arm64" : null;
  return os && cpu ? `${os}-${cpu}` : null;
}

/** The code as the web page shows it ("4F7-K2Q"), or null when it cannot be one. Same alphabet as the helper. */
export function normalizePairCode(code: string): string | null {
  const c = String(code || "").toUpperCase().replace(/[\s-]/g, "");
  if (!/^[2-9A-HJKMNP-Z]{6}$/.test(c)) return null;
  return `${c.slice(0, 3)}-${c.slice(3)}`;
}

function sha256File(file: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const h = createHash("sha256");
    createReadStream(file).on("error", reject).on("data", (d) => h.update(d)).on("end", () => resolve(h.digest("hex")));
  });
}

/** Windows' own bsdtar: a GNU tar earlier on PATH (Git Bash) reads "C:" as a remote host. */
export function tarCommand(): string {
  return process.platform === "win32" ? join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe") : "tar";
}

/** The unpacked helper's runtime and program. */
export function helperFiles(dir: string, plat: string, version: string = HELPER.version): { node: string; script: string } {
  const top = join(dir, `clearcote-helper-${version}-${plat}`);
  const root = existsSync(top) ? top : dir; // CLEARCOTE_HELPER_DIR may point inside the folder already
  return {
    node: join(root, "runtime", plat.startsWith("win") ? "node.exe" : "node"),
    script: join(root, "lib", "clearcote-helper.js"),
  };
}

/**
 * The helper on disk, downloading and checking it the first time. CLEARCOTE_HELPER_DIR uses an
 * unpacked helper you already have instead (nothing is downloaded or checked).
 */
export async function ensureHelper(opts: { cacheRoot?: string; quiet?: boolean; helper?: typeof HELPER; plat?: string } = {}): Promise<{ node: string; script: string }> {
  const plat = opts.plat ?? helperPlatform();
  if (!plat) throw new Error(`There is no Clearcote Helper for ${process.platform}-${process.arch}.`);
  if (process.env.CLEARCOTE_HELPER_DIR) {
    const f = helperFiles(process.env.CLEARCOTE_HELPER_DIR, plat);
    if (!existsSync(f.script)) throw new Error(`CLEARCOTE_HELPER_DIR: no lib/clearcote-helper.js in ${process.env.CLEARCOTE_HELPER_DIR}`);
    return f;
  }
  const helper = opts.helper ?? HELPER;
  const a = helper.assets[plat];
  if (!a?.sha256) throw new Error(`This SDK has no Clearcote Helper pinned for ${plat}.`);
  const dir = join(opts.cacheRoot ?? defaultCacheRoot(), "helper", `${helper.version}-${plat}`);
  if (existsSync(join(dir, ".verified"))) return helperFiles(dir, plat, helper.version);

  const log = (m: string) => { if (!opts.quiet) process.stderr.write(`[clearcote] ${m}\n`); };
  mkdirSync(join(dir, ".."), { recursive: true });
  const tmp = `${dir}.tmp-${process.pid}`;
  rmSync(tmp, { recursive: true, force: true });
  mkdirSync(tmp, { recursive: true });
  try {
    const file = join(tmp, a.asset);
    log(`downloading Clearcote Helper ${helper.version} (${a.asset})`);
    await downloadTo(helper.baseUrl + a.asset, file, a.size, opts.quiet);
    const got = await sha256File(file);
    if (got !== a.sha256) throw new Error(`Clearcote Helper download does not match its pinned SHA-256 (got ${got}, want ${a.sha256}). Nothing was run.`);
    const out = join(tmp, "x");
    mkdirSync(out);
    if (a.asset.endsWith(".zip")) await extract(file, { dir: out });
    // tar keeps the runtime's exec bit, which extract-zip/zipfile would not on macOS/Linux.
    else await promisify(execFile)(tarCommand(), ["-xzf", file, "-C", out]);
    rmSync(file, { force: true });
    writeFileSync(join(out, ".verified"), `${a.sha256}\n`);
    rmSync(dir, { recursive: true, force: true });
    renameDir(out, dir);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
  return helperFiles(dir, plat, helper.version);
}

/** Open a URL in the default browser. No shell: the URL is one argument. */
function openUrl(url: string): void {
  const [cmd, args] = process.platform === "win32" ? ["explorer.exe", [url]] : process.platform === "darwin" ? ["open", [url]] : ["xdg-open", [url]];
  try {
    const p = spawn(cmd, args as string[], { stdio: "ignore", detached: true, windowsHide: true });
    p.on("error", () => {});
    p.unref();
  } catch { /* the person opens it themselves */ }
}

export interface ConnectOptions {
  code?: string;
  port?: number;
  allowOrigins?: string[];
  /** Do not open anything in the browser (the app, or the helper's approval page). */
  noOpen?: boolean;
}

/** Run the helper in this terminal until it exits (Ctrl+C closes its browsers first). Returns its exit code. */
export async function connect(opts: ConnectOptions = {}): Promise<number> {
  let code: string | undefined;
  if (opts.code !== undefined) {
    const c = normalizePairCode(opts.code);
    if (!c) throw new Error(`"${opts.code}" is not a code the profile manager shows (it looks like 4F7-K2Q).`);
    code = c;
  }
  const files = await ensureHelper();
  const args = [files.script];
  if (opts.port) args.push("--port", String(opts.port));
  for (const o of opts.allowOrigins ?? []) args.push("--allow-origin", o);
  if (opts.noOpen) args.push("--no-open");

  if (code) {
    process.stderr.write(`[clearcote] starting the helper with code ${code}. The profile manager tab showing it connects by itself.\n`);
  } else {
    process.stderr.write(`[clearcote] starting the helper. In ${APP_URL} press "Connect this computer", then approve the pairing.\n`);
    if (!opts.noOpen) openUrl(APP_URL);
  }
  const env = { ...process.env };
  if (code) env.CLEARCOTE_HELPER_PAIR_CODE = code;
  else delete env.CLEARCOTE_HELPER_PAIR_CODE;

  const child = spawn(files.node, args, { stdio: "inherit", env });
  // Ctrl+C reaches the helper too (same console): it closes its browsers and exits, and so do we.
  // A SIGTERM/SIGHUP sent to us alone is passed on (POSIX; on Windows kill() would not let it stop cleanly).
  const handlers = new Map<NodeJS.Signals, () => void>();
  for (const s of ["SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"] as NodeJS.Signals[]) {
    const h = () => { if (process.platform !== "win32" && (s === "SIGTERM" || s === "SIGHUP")) child.kill(s); };
    handlers.set(s, h);
    process.on(s, h);
  }
  try {
    return await new Promise<number>((resolve, reject) => {
      child.on("error", reject);
      child.on("exit", (c, sig) => resolve(c ?? (sig ? 130 : 1)));
    });
  } finally {
    for (const [s, h] of handlers) process.off(s, h);
  }
}
