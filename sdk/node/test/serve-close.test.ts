/**
 * serve()'s close() waits for the browser to exit, then removes what it leaves in the temp directory:
 * the profile serve() made, and on Linux and macOS the browser's singleton-socket directory.
 *
 * WHY: close() sent SIGTERM and returned at once. A browser stopped with a signal never removes its
 * org.chromium.Chromium.* socket directory, and the profile was deleted while the browser was still
 * writing it: every serve() + close() on Linux left one directory in the temp directory (measured
 * with the open build, in all three SDKs).
 *
 * The stand-in browser does what Chrome does there: links its pid and socket from the profile, keeps
 * the socket in a temp directory of its own, goes on writing the profile for half a second after
 * SIGTERM, and leaves the socket directory behind. The last block runs a real engine
 * (CLEARCOTE_TEST_BINARY or CLEARCOTE_LIVE_ENGINE).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EventEmitter } from "node:events";
import { createServer } from "node:http";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readlinkSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { serve, Server } from "../src/index.js";
import { resolveLicenseKey } from "../src/license.js";
import { runServeChild } from "./helpers/serve-child.js";

const POSIX = process.platform !== "win32";
// A Clearcote build (serve() checks the install, so not just any Chromium), as the serve smoke test uses.
const ENGINE = process.env.CLEARCOTE_TEST_BINARY || process.env.CLEARCOTE_LIVE_ENGINE;

const STANDIN = [
  "#!/bin/sh",
  "udd=",
  'for a in "$@"; do case "$a" in --user-data-dir=*) udd="${a#--user-data-dir=}" ;; esac; done',
  'sock=$(mktemp -d "${TMPDIR:-/tmp}/org.chromium.Chromium.XXXXXX") || exit 1',
  ': > "$sock/SingletonSocket"',
  'ln -s "$sock/SingletonSocket" "$udd/SingletonSocket"',
  'ln -s "standin-host-$$" "$udd/SingletonLock"',
  'shutdown() { i=0; while [ $i -lt 10 ]; do mkdir -p "$udd/Default" && : > "$udd/Default/Shutdown $i"; i=$((i + 1)); sleep 0.05; done; exit 0; }',
  'if [ -n "$STANDIN_IGNORE_TERM" ]; then trap "" TERM; else trap shutdown TERM; fi',
  "while :; do sleep 0.05; done",
].join("\n") + "\n";

const ENV_KEYS = ["HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "CLEARCOTE_NO_WARN", "CLEARCOTE_LICENSE_KEY",
  "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION"] as const;
let savedEnv: Record<string, string | undefined>;
let root: string;
let temp: string; // the temp directory serve() and its browser see: anything left in it leaked
let standin: string;

beforeEach(() => {
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  // Short: a real browser's socket path (<temp>/org.chromium.Chromium.XXXXXX/SingletonSocket) has to fit
  // a Unix socket address, 107 bytes, and the browser exits at once when it does not.
  root = mkdtempSync(join(tmpdir(), "cc-sc-"));
  temp = join(root, "t");
  const home = join(root, "home");
  for (const d of [temp, home, join(root, "engine")]) mkdirSync(d);
  standin = join(root, "engine", "chrome");
  writeFileSync(standin, STANDIN);
  chmodSync(standin, 0o755);
  // No licence (it would take a real lease), no warnings, and a temp directory of this test's own.
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.TEMP = temp;
  process.env.TMP = temp;
  process.env.TMPDIR = temp;
  process.env.CLEARCOTE_NO_WARN = "1";
  for (const k of ["CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION"]) delete process.env[k];
});

afterEach(() => {
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
  rmSync(root, { recursive: true, force: true, maxRetries: 5 });
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/**
 * Stands in for the browser's CDP endpoint, which serve() polls until it answers. Like Chrome's, it
 * answers once the browser is up, its singleton included: the profile has a SingletonLock link.
 */
async function cdpEndpoint(profile?: string): Promise<{ port: number; close: () => Promise<void> }> {
  const up = () => (profile ? [profile] : readdirSync(temp).filter((n) => n.startsWith("clearcote-serve-")).map((n) => join(temp, n)))
    .some((d) => { try { return !!readlinkSync(join(d, "SingletonLock")); } catch { return false; } });
  const server = createServer(async (_req, res) => {
    for (let i = 0; i < 500 && !up(); i++) await sleep(10);
    res.setHeader("content-type", "application/json");
    res.end("{}");
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const addr = server.address();
  const port = typeof addr === "object" && addr ? addr.port : 0;
  return { port, close: () => new Promise<void>((r) => server.close(() => r())) };
}

it("harness: no licence is visible, so no serve() here takes a real lease", () => {
  expect(resolveLicenseKey()).toBeUndefined();
});

describe.skipIf(!POSIX)("serve(): close() against a browser that behaves like Chrome on Linux and macOS", () => {
  it("waits for the browser to exit, then removes its profile and its singleton-socket directory", async () => {
    const cdp = await cdpEndpoint();
    try {
      const srv = await serve({ executablePath: standin, port: cdp.port, quiet: true });
      const pid = srv.pid!;
      // the harness: a profile and the browser's socket directory, both in this test's temp directory
      expect(readdirSync(temp).map((n) => n.replace(/[^-.]+$/, "*")).sort())
        .toEqual(["clearcote-serve-*", "org.chromium.Chromium.*"]);
      await srv.close();
      expect(alive(pid)).toBe(false);
      await sleep(700); // a browser still shutting down would have written its profile back by now
      expect(readdirSync(temp)).toEqual([]);
    } finally {
      await cdp.close();
    }
  });

  it("keeps a profile that is the caller's, and removes the socket directory it links to", async () => {
    const profile = join(root, "profile");
    mkdirSync(profile);
    const cdp = await cdpEndpoint(profile);
    try {
      const srv = await serve({ executablePath: standin, port: cdp.port, quiet: true, userDataDir: profile });
      await srv.close();
      expect(readdirSync(temp)).toEqual([]);
      expect(existsSync(join(profile, "Default"))).toBe(true);
    } finally {
      await cdp.close();
    }
  });

  it("a process that exits without close() leaves nothing behind either", async () => {
    const cdp = await cdpEndpoint();
    try {
      // process.exit() runs no async work: the exit hook stops the browser and removes it all synchronously
      const { pid } = await runServeChild({ executablePath: standin, port: cdp.port, quiet: true });
      expect(alive(pid)).toBe(false);
      await sleep(700);
      expect(readdirSync(temp)).toEqual([]);
    } finally {
      await cdp.close();
    }
  }, 60_000);
});

/** A browser process: exits on `diesOn`, ignores any other signal. */
function fakeBrowser(diesOn: "SIGTERM" | "SIGKILL") {
  const proc = Object.assign(new EventEmitter(), {
    pid: undefined,
    exitCode: null as number | null,
    signalCode: null as string | null,
    signals: [] as string[],
    kill(signal = "SIGTERM") {
      proc.signals.push(signal);
      if (signal === diesOn) {
        queueMicrotask(() => {
          proc.signalCode = signal;
          proc.emit("exit", null, signal);
        });
      }
      return true;
    },
  });
  return proc;
}

describe("serve(): close() stops the browser", () => {
  it("kills a browser that is still running 10 s after SIGTERM, and only returns once it has exited", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const proc = fakeBrowser("SIGKILL");
      const srv = new Server(proc as never, "127.0.0.1", 9, join(root, "profile"), false);
      let closed = false;
      const closing = srv.close().then(() => { closed = true; });
      await vi.advanceTimersByTimeAsync(9_900);
      expect(proc.signals).toEqual(["SIGTERM"]);
      expect(closed).toBe(false);
      await vi.advanceTimersByTimeAsync(200);
      await closing;
      expect(proc.signals).toEqual(["SIGTERM", "SIGKILL"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("closes once, and leaves no process-exit hook behind", async () => {
    const before = process.listenerCount("exit");
    const proc = fakeBrowser("SIGTERM");
    const srv = new Server(proc as never, "127.0.0.1", 9, join(root, "profile"), false);
    expect(process.listenerCount("exit")).toBe(before + 1);
    await Promise.all([srv.close(), srv.close()]);
    await srv.close();
    expect(proc.signals).toEqual(["SIGTERM"]);
    expect(process.listenerCount("exit")).toBe(before);
  });
});

describe.runIf(ENGINE)("serve(): close() with a real engine", () => {
  it("leaves nothing in the temp directory", async () => {
    const srv = await serve({ executablePath: ENGINE!, quiet: true });
    const version = (await (await fetch(`${srv.cdpUrl}/json/version`)).json()) as { Browser?: string };
    expect(version.Browser).toMatch(/Chrom/);
    await srv.close();
    expect(alive(srv.pid!)).toBe(false);
    await sleep(1_000);
    // cc-fc-cache: the fontconfig cache every launch on a Linux machine shares, kept on purpose (fonts.ts)
    expect(readdirSync(temp).filter((n) => n !== "cc-fc-cache")).toEqual([]);
  }, 60_000);

  it("leaves nothing either when the process exits without close()", async () => {
    const { pid } = await runServeChild({ executablePath: ENGINE!, quiet: true });
    expect(alive(pid)).toBe(false);
    await sleep(1_000);
    expect(readdirSync(temp).filter((n) => n !== "cc-fc-cache")).toEqual([]);
  }, 60_000);
});
