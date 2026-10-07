import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { chmodSync, copyFileSync, existsSync, mkdirSync, readdirSync, rmSync, statSync, utimesSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { warmFiles } from "../src/download.js";
import { isWinLaunchRace, winAvRetry } from "../src/index.js";
import { main } from "../src/cli-commands.js";
import { RECOVERED_MARKER, discardCopy, recoveryKey, removeFailedLaunchDirs } from "../src/winlaunch.js";
import { tempDir } from "./helpers/temp.js";

// Windows: launching a cached build that Windows refuses to start in place ("spawn UNKNOWN" / "side-by-side
// configuration is incorrect"). Inside an MSIX-packaged app a build downloaded into %LOCALAPPDATA% is
// invisible to the activation-context check; real-time AV scanning a fresh chrome_elf.dll causes the same
// error for a while. winAvRetry retries in place, then launches from one recovered copy per build in
// ~/.clearcote/recovered that later launches reuse.
//
// The launch must leave the temp directory as it found it: the old fallback copied the whole browser to a
// fresh %TEMP%\clearcote-recover-* on every launch and never deleted it, and every failed Playwright attempt
// leaked a playwright_chromiumdev_profile-* and a playwright-artifacts-* directory.

const ENV_KEYS = ["HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "CLEARCOTE_CACHE"] as const;
const saved: Record<string, string | undefined> = {};
let base: string;
let home: string;
let tmp: string;
let exe: string;
const realPlatform = Object.getOwnPropertyDescriptor(process, "platform")!;

function fakeWindows(): void {
  Object.defineProperty(process, "platform", { value: "win32", configurable: true });
}

const birthtimeSupported = (() => {
  const d = tempDir("cc-birth-");
  return statSync(d).birthtimeMs > 0;
})();

beforeEach(() => {
  base = tempDir("cc-winl-");
  for (const k of ENV_KEYS) saved[k] = process.env[k];
  home = join(base, "home");
  tmp = join(base, "tmp");
  mkdirSync(home);
  mkdirSync(tmp);
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.TEMP = process.env.TMP = process.env.TMPDIR = tmp;
  const bdir = join(base, "cache", "pro-1.2.3-r9", "browser");
  mkdirSync(bdir, { recursive: true });
  exe = join(bdir, "chrome.exe");
  writeFileSync(exe, "stub");
  writeFileSync(join(bdir, "1.2.3.manifest"), "<assembly/>");
});

afterEach(() => {
  Object.defineProperty(process, "platform", realPlatform);
  for (const k of ENV_KEYS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
  vi.restoreAllMocks();
});

const root = () => join(home, ".clearcote", "recovered");

/** What Playwright does when Windows cannot start the process: make its two directories, then throw. */
let made = 0;
function failingPlaywrightLaunch(): never {
  const id = String(made++).padStart(6, "0");
  mkdirSync(join(tmp, `playwright-artifacts-${id}`));
  const prof = join(tmp, `playwright_chromiumdev_profile-${id}`);
  mkdirSync(prof);
  throw new Error(`browserType.launch: spawn UNKNOWN\nCall log:\n  - <launching> ${exe} --no-first-run --user-data-dir=${prof} --remote-debugging-pipe about:blank`);
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("Windows launch of a build that cannot start in place", () => {
  it("warmFiles reads a tree (forcing the AV scan) without throwing", () => {
    const d = tempDir("cc-warm-");
    writeFileSync(join(d, "chrome.exe"), Buffer.alloc(1000));
    mkdirSync(join(d, "locales"));
    writeFileSync(join(d, "locales", "en-US.pak"), Buffer.alloc(500));
    expect(() => warmFiles(d)).not.toThrow();
    expect(() => warmFiles(join(d, "missing"))).not.toThrow(); // missing dir is a no-op
  });

  it("isWinLaunchRace classifies the race errors", () => {
    expect(isWinLaunchRace(new Error("browserType.launch: spawn UNKNOWN"))).toBe(true);
    expect(isWinLaunchRace(new Error("failed to start ... side-by-side configuration is incorrect"))).toBe(true);
    expect(isWinLaunchRace(new Error("Timeout 30000ms exceeded"))).toBe(false);
    expect(isWinLaunchRace("net::ERR_CONNECTION_REFUSED")).toBe(false);
  });

  it("winAvRetry is a pass-through off Windows (one call, no retry)", async () => {
    Object.defineProperty(process, "platform", { value: "linux", configurable: true });
    let n = 0;
    const r = await winAvRetry(async (e) => {
      n++;
      return `browser:${e}`;
    }, "/x/chrome");
    expect(r).toBe("browser:/x/chrome");
    expect(n).toBe(1);
  });

  it("launches in place when the cached build starts", async () => {
    fakeWindows();
    const calls: string[] = [];
    expect(await winAvRetry(async (e) => (calls.push(e), "browser"), exe)).toBe("browser");
    expect(calls).toEqual([exe]);
    expect(existsSync(root())).toBe(false);
  });

  it.skipIf(!birthtimeSupported)("recovers once into ~/.clearcote/recovered, reuses it, and leaves the temp dir empty", async () => {
    fakeWindows();
    const calls: string[] = [];
    const doLaunch = async (e: string) => {
      calls.push(e);
      if (e === exe) failingPlaywrightLaunch();
      return e;
    };
    const first = await winAvRetry(doLaunch, exe, 1);
    const key = recoveryKey(exe);
    expect(first).toBe(join(root(), key, "browser", "chrome.exe"));
    expect(calls).toEqual([exe, exe, exe, first]); // three attempts in place, then the copy
    expect(existsSync(join(root(), key, RECOVERED_MARKER))).toBe(true);
    expect(existsSync(join(root(), key, "browser", "1.2.3.manifest"))).toBe(true); // the whole build came along
    expect(readdirSync(tmp)).toEqual([]); // no Playwright leftovers, no clearcote-recover-* copy

    calls.length = 0;
    expect(await winAvRetry(doLaunch, exe, 1)).toBe(first);
    expect(calls).toEqual([first]); // straight to the copy: no failed attempt, no new copy
    expect(readdirSync(root())).toEqual([key]);
  });

  it("a rebuilt build gets a fresh copy and the stale one goes; old SDKs' temp copies are swept", async () => {
    fakeWindows();
    const oldTemp = join(tmp, "clearcote-recover-old");
    mkdirSync(join(oldTemp, "browser"), { recursive: true });
    writeFileSync(join(oldTemp, "browser", "chrome.exe"), "x");
    const twoMinAgo = (Date.now() - 120_000) / 1000;
    utimesSync(oldTemp, twoMinAgo, twoMinAgo);
    mkdirSync(join(tmp, "clearcote-recover-new")); // possibly a concurrent old-SDK launch mid-copy
    const doLaunch = async (e: string) => {
      if (e === exe) throw new Error("spawn UNKNOWN");
      return e;
    };
    const first = await winAvRetry(doLaunch, exe, 1);
    const oldKey = recoveryKey(exe);
    writeFileSync(exe, "rebuilt stub"); // same path, new build
    const second = await winAvRetry(doLaunch, exe, 1);
    expect(second).not.toBe(first);
    expect(readdirSync(root())).toEqual([recoveryKey(exe)]);
    expect(recoveryKey(exe)).not.toBe(oldKey);
    expect(readdirSync(tmp)).toEqual(["clearcote-recover-new"]);
  });

  it("a copy a browser is running from is never deleted", async () => {
    const copy = join(base, "copy");
    mkdirSync(join(copy, "browser"), { recursive: true });
    if (realPlatform.value === "win32") {
      // Real Windows semantics: the exe of a running process cannot be deleted.
      const running = join(copy, "browser", "chrome.exe");
      copyFileSync(join(process.env.SystemRoot ?? "C:\\Windows", "System32", "PING.EXE"), running);
      const p = spawn(running, ["-n", "30", "127.0.0.1"], { stdio: "ignore", windowsHide: true });
      await new Promise((r) => p.once("spawn", r));
      try {
        expect(discardCopy(copy)).toBe(false);
        expect(existsSync(running)).toBe(true);
      } finally {
        p.kill();
        await new Promise((r) => p.once("exit", r));
      }
      await sleep(200);
      expect(discardCopy(copy)).toBe(true);
    } else {
      if (process.getuid?.() === 0) return; // root ignores the permission that stands in for "in use"
      writeFileSync(join(copy, "browser", "chrome.exe"), "x");
      chmodSync(join(copy, "browser"), 0o500); // unlink fails, as for a running exe on Windows
      try {
        expect(discardCopy(copy)).toBe(false);
        expect(existsSync(join(copy, "browser", "chrome.exe"))).toBe(true);
      } finally {
        chmodSync(join(copy, "browser"), 0o700);
      }
      expect(discardCopy(copy)).toBe(true);
    }
  }, 20000);

  it("recoveryKey matches the Python SDK's recovery_key", () => {
    const b = join(base, "pro-9.9.9-r1", "browser");
    mkdirSync(b, { recursive: true });
    const e = join(b, "chrome.exe");
    writeFileSync(e, "12345");
    utimesSync(e, 1_700_000_000.123, 1_700_000_000.123);
    const ms = Math.floor(statSync(e).mtimeMs); // what the file system kept of .123
    const ident = `${process.platform === "win32" ? b.toLowerCase() : b}|5|${ms}`;
    expect(recoveryKey(e)).toBe(`pro-9.9.9-r1-${createHash("sha256").update(ident).digest("hex").slice(0, 12)}`);
  });

  it("winAvRetry re-raises non-race errors immediately", async () => {
    fakeWindows();
    await expect(winAvRetry(async () => {
      throw new Error("Timeout 30000ms exceeded");
    }, exe)).rejects.toThrow("Timeout");
  });

  it("clear-cache removes the recovered copies too", async () => {
    fakeWindows();
    process.env.CLEARCOTE_CACHE = join(base, "no-cache");
    await winAvRetry(async (e) => {
      if (e === exe) throw new Error("spawn UNKNOWN");
      return e;
    }, exe, 1);
    let out = "";
    vi.spyOn(process.stdout, "write").mockImplementation(((c: string | Uint8Array) => { out += String(c); return true; }) as never);
    await main(["clear-cache"]);
    expect(out).toMatch(/removed 1 recovered build copy from/);
    expect(existsSync(root())).toBe(false);
  });
});

describe.skipIf(!birthtimeSupported)("removeFailedLaunchDirs", () => {
  const mk = async (name: string) => {
    mkdirSync(join(tmp, name));
    await sleep(30); // distinct creation times
    return join(tmp, name);
  };
  const callLog = (udd: string) =>
    new Error(`browserType.launch: spawn UNKNOWN\nCall log:\n  - <launching> C:\\c\\chrome.exe --x --user-data-dir=${udd} --remote-debugging-pipe`);

  it("launch(): removes the profile named in the call log and the artifacts dir made just before it", async () => {
    await mk("playwright-artifacts-old111");
    const started = Date.now();
    await sleep(30);
    await mk("playwright-artifacts-ours22");
    const prof = await mk("playwright_chromiumdev_profile-abcDEF");
    await mk("playwright-artifacts-later3"); // another launch, after our profile
    const busy = await mk("playwright-artifacts-busy44");
    writeFileSync(join(busy, "download.bin"), "x");
    removeFailedLaunchDirs(callLog(prof), started);
    expect(readdirSync(tmp).sort()).toEqual(["playwright-artifacts-busy44", "playwright-artifacts-later3", "playwright-artifacts-old111"]);
  });

  it("persistent launch: removes only its artifacts dir, never the caller's profile", async () => {
    const mine = await mk("my-profile");
    const started = Date.now();
    await sleep(30);
    await mk("playwright-artifacts-ours22");
    removeFailedLaunchDirs(callLog(mine), started);
    expect(readdirSync(tmp)).toEqual(["my-profile"]);
  });

  it("removes nothing when it cannot tell which directory is its own", async () => {
    const started = Date.now();
    await sleep(30);
    await mk("playwright-artifacts-aaaaaa");
    await mk("playwright-artifacts-bbbbbb"); // a concurrent launch
    removeFailedLaunchDirs(callLog(join(tmp, "clearcote-run-x")), started);
    expect(readdirSync(tmp)).toHaveLength(2);
  });

  it("needs a Playwright call log (a raw spawn, as in serve, made no directories)", async () => {
    const started = Date.now();
    await sleep(30);
    await mk("playwright-artifacts-aaaaaa");
    removeFailedLaunchDirs(new Error("spawn UNKNOWN"), started);
    expect(readdirSync(tmp)).toHaveLength(1);
  });
});

