// Entry-point wiring the pure helper tests cannot reach: profile:"auto" through launch(),
// launchPersistentContext() and serve(); the throwaway-profile lifecycle of launch() and
// launchAgent(); and the proxy switches serve() puts on the engine command line.
//
// Stubbed: Playwright, the child-process spawn, and the two expensive "auto" collaborators (the
// host measurement launches a browser, the resolver calls the profile service). Everything between
// them — option merging, binary resolution, arg assembly, temp-directory handling — runs for real.
import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from "vitest";
import { createServer, type Server as HttpServer } from "node:http";
import type { EventEmitter } from "node:events";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { gunzipSync } from "node:zlib";

const stub = vi.hoisted(() => ({
  // PROFILE_DIR is read once, at import: point it somewhere empty before the SDK loads, so no saved
  // profile on this machine (a real auto.json included) can change what these tests see.
  savedProfileDir: ((prev) => { process.env.CLEARCOTE_PROFILE_DIR = `${process.cwd()}/.no-saved-profiles`; return prev; })(process.env.CLEARCOTE_PROFILE_DIR),
  launches: [] as Array<{ kind: "persistent" | "incognito"; userDataDir?: string; opts: Record<string, unknown> }>,
  spawns: [] as string[][],
  failLaunch: null as Error | null,
  // The stand-in browser: what it leaves in its profile at launch, and what it does while it shuts down.
  browser: {
    onLaunch: null as null | ((userDataDir: string) => void),
    shutdown: null as null | ((userDataDir: string) => Promise<void>),
  },
  host: {
    os_family: "windows", browser_major: 153, gpu_vendor: "intel",
    screen_width: 1920, screen_height: 1080, device_pixel_ratio: 1,
    hardware_concurrency: 8, device_memory: 8,
  },
  serviceProfile: { navigator: { user_agent: "captured-ua", languages: ["en-US", "en"] } },
}));

vi.mock("playwright-core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("playwright-core")>();
  const { EventEmitter } = await import("node:events");
  const fakeContext = (userDataDir?: string) => {
    const ctx = new EventEmitter() as EventEmitter & Record<string, unknown>;
    ctx.pages = () => [];
    ctx.browser = () => null;
    // As real Playwright does: "close" is emitted when the browser's pipe drops, and close() resolves
    // only once the browser process has exited — which on Linux is ~100 ms later, and Chrome keeps
    // writing its profile in between.
    ctx.close = async () => {
      ctx.emit("close", ctx);
      if (userDataDir && stub.browser.shutdown) await stub.browser.shutdown(userDataDir);
    };
    return ctx;
  };
  const chromium = {
    launchPersistentContext: async (userDataDir: string, opts: Record<string, unknown>) => {
      stub.launches.push({ kind: "persistent", userDataDir, opts });
      if (stub.failLaunch) throw stub.failLaunch;
      stub.browser.onLaunch?.(userDataDir);
      return fakeContext(userDataDir);
    },
    launch: async (opts: Record<string, unknown>) => {
      stub.launches.push({ kind: "incognito", opts });
      if (stub.failLaunch) throw stub.failLaunch;
      const browser = new EventEmitter() as EventEmitter & Record<string, unknown>;
      browser.newPage = async () => ({});
      browser.newContext = async () => fakeContext();
      browser.close = async () => {};
      return browser;
    },
  };
  return { ...actual, chromium };
});

vi.mock("node:child_process", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:child_process")>();
  const { EventEmitter } = await import("node:events");
  return {
    ...actual,
    spawn: (_cmd: string, args: string[]) => {
      stub.spawns.push(args);
      const p = new EventEmitter() as EventEmitter & Record<string, unknown>;
      p.exitCode = null;
      p.killed = false;
      p.pid = 4242;
      p.kill = () => { p.killed = true; return true; };
      queueMicrotask(() => p.emit("spawn"));
      return p;
    },
  };
});

vi.mock("../src/profilesource.js", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/profilesource.js")>()),
  measureHost: async () => stub.host,
}));

vi.mock("../src/profileauto.js", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/profileauto.js")>()),
  resolveAuto: async () => ({ profile: stub.serviceProfile, selection: { entry: { id: "svc-1" } }, source: "service" }),
}));

import { launch, launchAgent, launchPersistentContext, serve, serveNeedsNoSandbox } from "../src/index.js";
import { resolveLicenseKey } from "../src/license.js";

afterAll(() => {
  if (stub.savedProfileDir === undefined) delete process.env.CLEARCOTE_PROFILE_DIR;
  else process.env.CLEARCOTE_PROFILE_DIR = stub.savedProfileDir;
});

const ENV_KEYS = ["HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "CLEARCOTE_NO_WARN", "CLEARCOTE_LICENSE_KEY",
  "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION"] as const;
let savedEnv: Record<string, string | undefined>;
let root: string;
let temp: string; // the TEMP every launch sees: anything left in it after a test leaked
let exe: string;

beforeEach(() => {
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  root = mkdtempSync(join(tmpdir(), "cc-entry-"));
  temp = join(root, "temp");
  const home = join(root, "home");
  for (const d of [temp, home, join(root, "engine")]) mkdirSync(d);
  // A modern engine as far as the capability probes can tell. Deliberately no chrome.dll next to
  // it: checkInstall only verifies a tree that looks like a full install.
  exe = join(root, "engine", process.platform === "win32" ? "chrome.exe" : "chrome");
  writeFileSync(exe, Buffer.from("MZ\0socks5-credentials\0\0proxy-auth\0\0fingerprint-profile\0", "latin1"));
  // No licence (a key would select the PRO binary and a lease), no saved profiles, no warnings.
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.TEMP = temp;
  process.env.TMP = temp;
  process.env.TMPDIR = temp;
  process.env.CLEARCOTE_NO_WARN = "1";
  for (const k of ["CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION"]) delete process.env[k];
  stub.launches.length = 0;
  stub.spawns.length = 0;
  stub.failLaunch = null;
  stub.browser.onLaunch = null;
  stub.browser.shutdown = null;
});

afterEach(() => {
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
  rmSync(root, { recursive: true, force: true });
});

/** The persona a --fingerprint-profile switch carries (gzip + base64 JSON). */
function decodedProfile(args: string[]): unknown {
  const a = args.find((x) => x.startsWith("--fingerprint-profile="));
  return a ? JSON.parse(gunzipSync(Buffer.from(a.slice("--fingerprint-profile=".length), "base64")).toString("utf8")) : undefined;
}

const base = () => ({ executablePath: exe, headless: false, quiet: true });

it("harness: no licence is visible, so no launch here can take a real lease", () => {
  expect(resolveLicenseKey()).toBeUndefined();
});

describe("profile: \"auto\" on every entry point", () => {
  // Regression: launch() is profile-backed by default (0.23.0), and launchPersistentContext loaded
  // "auto" as a SAVED profile name -> ENOENT ~/.clearcote/profiles/auto.json. Only the incognito
  // path (ephemeralProfile: false) knew "auto" was special.
  it("launch() — the default ephemeral profile", async () => {
    const browser = await launch({ ...base(), profile: "auto" });
    const call = stub.launches.at(-1)!;
    expect(call.kind).toBe("persistent");
    const args = call.opts.args as string[];
    expect(decodedProfile(args)).toEqual(stub.serviceProfile);
    expect(args.some((a) => a.startsWith("--fingerprint="))).toBe(false); // "auto" never seeds
    await browser.close();
  });

  it("launchPersistentContext()", async () => {
    const udd = join(root, "profile");
    const context = await launchPersistentContext(udd, { ...base(), profile: "auto" });
    const call = stub.launches.at(-1)!;
    expect(call.userDataDir).toBe(udd);
    expect(decodedProfile(call.opts.args as string[])).toEqual(stub.serviceProfile);
    expect(call.opts).not.toHaveProperty("profileSelect"); // an SDK option, not a Playwright one
    await context.close();
  });

  it("launch({ ephemeralProfile: false }) — unchanged", async () => {
    const browser = await launch({ ...base(), profile: "auto", ephemeralProfile: false });
    const call = stub.launches.at(-1)!;
    expect(call.kind).toBe("incognito");
    expect(decodedProfile(call.opts.args as string[])).toEqual(stub.serviceProfile);
    await browser.close();
  });

  it("serve()", async () => {
    const { port, close } = await fakeCdpEndpoint();
    try {
      const srv = await serve({ ...base(), port, profile: "auto" });
      expect(decodedProfile(stub.spawns.at(-1)!)).toEqual(stub.serviceProfile);
      await srv.close();
    } finally {
      await close();
    }
  });

  it("a saved profile name that does not exist still fails loudly", async () => {
    await expect(launch({ ...base(), profile: "no-such-profile" })).rejects.toThrow(/no-such-profile/);
  });
});

describe("throwaway profiles are never left behind", () => {
  const leftovers = () => readdirSync(temp);

  // Regression: the temp profile was created before the launch and never removed when it failed.
  it("launch() removes its profile when the launch fails", async () => {
    stub.failLaunch = new Error("browser failed to start");
    await expect(launch(base())).rejects.toThrow("browser failed to start");
    expect(stub.launches.at(-1)!.userDataDir).toMatch(/clearcote-run-/);
    expect(leftovers()).toEqual([]);
  });

  it("launch() removes its profile when option resolution fails before any browser starts", async () => {
    await expect(launch({ ...base(), profile: "no-such-profile" })).rejects.toThrow();
    expect(leftovers()).toEqual([]);
  });

  it("launchAgent() removes its profile when the launch fails", async () => {
    stub.failLaunch = new Error("browser failed to start");
    await expect(launchAgent(base())).rejects.toThrow("browser failed to start");
    expect(stub.launches.at(-1)!.userDataDir).toMatch(/clearcote-agent-/);
    expect(leftovers()).toEqual([]);
  });

  // The caller never learns the path of a profile it did not name, so keeping it only leaks it.
  it("launchAgent() removes the profile it created when the context closes", async () => {
    const context = await launchAgent(base());
    const dir = stub.launches.at(-1)!.userDataDir!;
    expect(existsSync(dir)).toBe(true);
    await context.close();
    await vi.waitFor(() => expect(existsSync(dir)).toBe(false));
    expect(leftovers()).toEqual([]);
  });

  it("launchAgent({ userDataDir }) keeps the caller's profile", async () => {
    const udd = join(root, "keep-me");
    mkdirSync(udd);
    const context = await launchAgent({ ...base(), userDataDir: udd });
    await context.close();
    await new Promise((r) => setTimeout(r, 50));
    expect(existsSync(udd)).toBe(true);
  });

  // Regression (0.35.0/0.36.0 release smoke on Linux: a clearcote-run-* directory left in /tmp per
  // run). The profile was deleted on the "close" event, while the browser was still shutting down;
  // Chrome then wrote the directory back (Default/, Local State) after the cleanup had marked itself
  // done, so nothing ever retried.
  it.each([
    ["launch()", () => launch(base()) as unknown as Promise<{ close(): Promise<void> }>],
    ["launchAgent()", () => launchAgent(base()) as Promise<{ close(): Promise<void> }>],
  ])("%s: close() removes the profile only after the browser has exited, so its last writes cannot outlive it", async (_name, start) => {
    stub.browser.shutdown = async (udd) => {
      await new Promise((r) => setTimeout(r, 20));
      mkdirSync(join(udd, "Default"), { recursive: true });
      writeFileSync(join(udd, "Local State"), "{}");
    };
    const browser = await start();
    const dir = stub.launches.at(-1)!.userDataDir!;
    await browser.close();
    expect(existsSync(dir)).toBe(false);
    expect(leftovers()).toEqual([]);
  });

  // The browser can also go away by itself (a crash, its last window closed): no close() to wait for.
  // Chrome names its pid in the SingletonLock symlink ("<host>-<pid>") on Linux and macOS.
  it.skipIf(process.platform === "win32")("a browser that exits on its own: the profile is removed once its process has exited, not when its pipe drops", async () => {
    const real = await vi.importActual<typeof import("node:child_process")>("node:child_process");
    const proc = real.spawn(process.execPath, ["-e", "setTimeout(() => {}, 60000)"], { stdio: "ignore" });
    try {
      stub.browser.onLaunch = (udd) => symlinkSync(`test-host-${proc.pid}`, join(udd, "SingletonLock"));
      const browser = await launch(base());
      const dir = stub.launches.at(-1)!.userDataDir!;
      (browser as unknown as EventEmitter).emit("close", browser); // the pipe dropped
      await new Promise((r) => setTimeout(r, 300));
      expect(existsSync(dir)).toBe(true); // still shutting down: deleting it now is what leaked
      const exited = new Promise((r) => proc.once("exit", r));
      proc.kill();
      await exited;
      await vi.waitFor(() => expect(existsSync(dir)).toBe(false));
      expect(leftovers()).toEqual([]);
    } finally {
      proc.kill();
    }
  });

  // Chrome keeps its singleton socket in a temp directory of its own, found through the profile's
  // SingletonSocket symlink. A browser that never shuts down cleanly (killed at process exit) leaves
  // it behind: 71 org.chromium.Chromium.* directories in one Linux machine's /tmp in a single day.
  it.skipIf(process.platform === "win32")("removes the browser's singleton-socket directory with the profile", async () => {
    const socketDir = join(temp, "org.chromium.Chromium.Ab12Cd");
    mkdirSync(socketDir);
    writeFileSync(join(socketDir, "SingletonSocket"), "");
    stub.browser.onLaunch = (udd) => symlinkSync(join(socketDir, "SingletonSocket"), join(udd, "SingletonSocket"));
    const browser = await launch(base());
    await browser.close();
    expect(leftovers()).toEqual([]);
  });

  it.skipIf(process.platform === "win32")("never deletes a SingletonSocket target that is not Chrome's own temp directory", async () => {
    const precious = join(root, "precious");
    mkdirSync(precious);
    writeFileSync(join(precious, "SingletonSocket"), "");
    stub.browser.onLaunch = (udd) => symlinkSync(join(precious, "SingletonSocket"), join(udd, "SingletonSocket"));
    const browser = await launch(base());
    await browser.close();
    expect(leftovers()).toEqual([]);
    expect(existsSync(join(precious, "SingletonSocket"))).toBe(true);
  });

  // A licensed launch writes a run-token file for the engine (<tmp>/clearcote-rt-*.tok) before the
  // browser starts. When the browser then failed to start, the lease was released but the file stayed
  // in the temp directory until the process exited (a paid lease is shared, so its stop() only drops a
  // reference) — and for good when the process was killed.
  it.each([
    ["launch()", {}],
    ["launch({ ephemeralProfile: false })", { ephemeralProfile: false }],
  ])("%s: a licensed launch that fails to start leaves no run-token file behind", async (_name, extra) => {
    const realFetch = globalThis.fetch;
    const token = Buffer.from(JSON.stringify({ v: 1, plan: "pro", n: 1 })).toString("base64url") + ".sig";
    globalThis.fetch = (async (url: unknown) => {
      if (String(url).endsWith("/checkout")) {
        return new Response(JSON.stringify({
          lease_id: "L1", token, exp: Math.floor(Date.now() / 1000) + 900, lease_ttl_sec: 810,
          heartbeat_interval_sec: 270, concurrency: { used: 1, limit: 5 },
        }), { status: 200 });
      }
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    try {
      stub.failLaunch = new Error("browser failed to start");
      const key = `cc_lic_pro_${Date.now()}_${Math.random().toString(36).slice(2)}`; // its own lease
      await expect(launch({ ...base(), ...extra, licenseKey: key, licenseApiBase: "http://test.local" }))
        .rejects.toThrow("browser failed to start");
      expect(stub.launches).toHaveLength(1); // the browser really was asked to start, under a lease
      expect(leftovers()).toEqual([]);
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  // Handed an executable that does not exist, Playwright fails only after creating its temp
  // directories (playwright-artifacts-*, plus playwright_chromiumdev_profile-* for launch()), which it
  // leaves behind — and a licensed launch had taken a lease by then.
  it.each([
    ["executablePath", (missing: string) => launch({ ...base(), executablePath: missing })],
    ["CLEARCOTE_BINARY", (missing: string) => { process.env.CLEARCOTE_BINARY = missing; return launch({ headless: false, quiet: true }); }],
  ])("a missing %s is refused before any browser is started", async (_name, start) => {
    const missing = join(root, "nowhere", process.platform === "win32" ? "chrome.exe" : "chrome");
    await expect(start(missing)).rejects.toThrow(/Clearcote binary not found/);
    expect(stub.launches).toEqual([]);
    expect(leftovers()).toEqual([]);
  });

  // One process-exit hook per launch, never removed: a caller running hundreds of short-lived
  // browsers piled them up (MaxListenersExceededWarning past 10).
  it("a closed launch leaves no process-exit hook behind", async () => {
    const before = process.listenerCount("exit");
    for (let i = 0; i < 3; i++) await (await launch(base())).close();
    expect(process.listenerCount("exit")).toBe(before);
  });
});

describe("serve() proxy switches", () => {
  // Regression: serve() appended --proxy-server=<the caller's raw server> AFTER the resolved proxy
  // args. The last --proxy-server wins, and with user:pass@ still in it Chromium drops the entry.
  it("sends URL credentials to the engine and never puts userinfo on --proxy-server", async () => {
    const { port, close } = await fakeCdpEndpoint();
    try {
      const srv = await serve({ ...base(), port, proxy: { server: "socks5://user:p%40ss@127.0.0.1:1080" } });
      const args = stub.spawns.at(-1)!;
      expect(args).toContain("--socks5-credentials=user:p@ss");
      expect(args.filter((a) => a.startsWith("--proxy-server="))).toEqual(["--proxy-server=socks5://127.0.0.1:1080"]);
      await srv.close();
    } finally {
      await close();
    }
  });

  it("keeps a plain proxy on a single --proxy-server", async () => {
    const { port, close } = await fakeCdpEndpoint();
    try {
      const srv = await serve({ ...base(), port, proxy: { server: "http://127.0.0.1:3128" } });
      expect(stub.spawns.at(-1)!.filter((a) => a.startsWith("--proxy-server="))).toEqual(["--proxy-server=http://127.0.0.1:3128"]);
      await srv.close();
    } finally {
      await close();
    }
  });
});

describe("serve(): the unsupported-flag infobar", () => {
  // Any flag on Chromium's list (--no-sandbox among them) puts a warning bar on the first tab, ~56px
  // off its innerHeight. The switch that suppresses it has to reach the engine's command line.
  it("headless: --disable-infobars on the command line, with or without --no-sandbox", async () => {
    const { port, close } = await fakeCdpEndpoint();
    try {
      for (const args of [[], ["--no-sandbox"]]) {
        const srv = await serve({ ...base(), headless: true, port, args });
        const line = stub.spawns.at(-1)!;
        expect(line.filter((a) => a === "--disable-infobars")).toHaveLength(1);
        expect(line.some((a) => a.startsWith("--test-type"))).toBe(false);
        await srv.close();
      }
    } finally {
      await close();
    }
  });

  it("headed: --test-type only when --no-sandbox is on the command line", async () => {
    const { port, close } = await fakeCdpEndpoint();
    try {
      // As root on Linux serve() adds --no-sandbox itself, and --test-type comes with it.
      const asRoot = serveNeedsNoSandbox(process.platform, process.getuid?.(), []);
      const plain = await serve({ ...base(), port });
      expect(stub.spawns.at(-1)!.filter((a) => a === "--test-type")).toHaveLength(asRoot ? 1 : 0);
      expect(stub.spawns.at(-1)).not.toContain("--disable-infobars");
      await plain.close();
      const unsandboxed = await serve({ ...base(), port, args: ["--no-sandbox"] });
      expect(stub.spawns.at(-1)!.filter((a) => a === "--test-type")).toHaveLength(1);
      await unsandboxed.close();
    } finally {
      await close();
    }
  });
});

describe("launch() proxy options", () => {
  it("routes SOCKS5 credentials written in the URL to the engine, not Playwright", async () => {
    const browser = await launch({ ...base(), proxy: { server: "socks5://user:pass@127.0.0.1:1080" } });
    const call = stub.launches.at(-1)!;
    expect(call.opts.args).toEqual(expect.arrayContaining(["--proxy-server=socks5://127.0.0.1:1080", "--socks5-credentials=user:pass"]));
    expect(call.opts.proxy).toBeUndefined();
    await browser.close();
  });
});

/** A stand-in for the browser's CDP HTTP endpoint, which serve() polls until it answers. */
async function fakeCdpEndpoint(): Promise<{ port: number; close: () => Promise<void> }> {
  const server: HttpServer = createServer((_req, res) => {
    res.setHeader("content-type", "application/json");
    res.end("{}");
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const addr = server.address();
  const port = typeof addr === "object" && addr ? addr.port : 0;
  return { port, close: () => new Promise<void>((r) => server.close(() => r())) };
}

// -- 2026-10-06 coherence fixes, end to end through every entry point -------------------------------

/** The single --disable-features on a launch line, split. */
function disabledFeatures(args: string[]): string[] {
  const found = args.filter((a) => a.startsWith("--disable-features="));
  expect(found).toHaveLength(1);
  return found[0].slice("--disable-features=".length).split(",");
}

describe("Playwright's --disable-features is replaced without ThirdPartyStoragePartitioning", () => {
  // Measured on r30 vs genuine Chrome 154: with Playwright's switch a cross-site iframe read the
  // top-level site's storage, or got a SecurityError from localStorage with third-party cookies
  // blocked; genuine gives it an empty partition either way.
  for (const platform of ["windows", "linux"] as const) {
    it(`launch() under a ${platform} claim`, async () => {
      const browser = await launch({ ...base(), fingerprint: "s1", platform });
      const feats = disabledFeatures(stub.launches.at(-1)!.opts.args as string[]);
      for (const f of ["ThirdPartyStoragePartitioning", "AcceptCHFrame", "HttpsUpgrades"]) expect(feats).not.toContain(f);
      for (const f of ["MediaRouter", "Translate", "RenderDocument", "PaintHolding"]) expect(feats).toContain(f);
      expect(feats.includes("WebBluetooth")).toBe(platform === "linux");
      await browser.close();
    });
  }

  it("launchPersistentContext()", async () => {
    const context = await launchPersistentContext(join(root, "p"), { ...base(), fingerprint: "s1" });
    const feats = disabledFeatures(stub.launches.at(-1)!.opts.args as string[]);
    expect(feats).not.toContain("ThirdPartyStoragePartitioning");
    expect(feats).toContain("MediaRouter");
    await context.close();
  });

  it("not when the caller dropped Playwright's defaults", async () => {
    const browser = await launch({ ...base(), ephemeralProfile: false, ignoreDefaultArgs: true } as never);
    const args = stub.launches.at(-1)!.opts.args as string[];
    expect(args.some((a) => a.includes("MediaRouter"))).toBe(false);
    await browser.close();
  });

  it("never on serve(), which starts Chromium itself", async () => {
    const { port, close } = await fakeCdpEndpoint();
    try {
      const srv = await serve({ ...base(), port, fingerprint: "s1" });
      const line = stub.spawns.at(-1)!;
      expect(line.some((a) => a.includes("MediaRouter") || a.includes("ThirdPartyStoragePartitioning"))).toBe(false);
      await srv.close();
    } finally {
      await close();
    }
  });
});

describe("profile: \"auto\" from a Windows 11 donor carries the Windows 11 system faces", () => {
  it("launch()", async () => {
    const saved = stub.serviceProfile;
    (stub as { serviceProfile: unknown }).serviceProfile = {
      navigator: { platform: "Win32", uadata: { platform: "Windows", high_entropy: { platform: "Windows", platformVersion: "15.0.0" } } },
      fonts: { detected: ["Arial", "HoloLens MDL2 Assets", "Segoe MDL2 Assets"] },
    };
    try {
      const browser = await launch({ ...base(), profile: "auto" });
      const detected = (decodedProfile(stub.launches.at(-1)!.opts.args as string[]) as { fonts: { detected: string[] } }).fonts.detected;
      expect(detected).toEqual(expect.arrayContaining(["Segoe Fluent Icons", "Segoe UI Variable Display", "Segoe UI Variable Text"]));
      expect(detected).not.toContain("Cascadia Code");
      expect(detected).not.toContain("Segoe UI Variable");  // genuine Windows 11 reports the bare family absent
      await browser.close();
    } finally {
      (stub as { serviceProfile: unknown }).serviceProfile = saved;
    }
  });
});
