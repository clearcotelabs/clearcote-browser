// `clearcote` CLI (#6): commands present, info never downloads, reports cache/engine support/licence,
// login refuses a rejected key, logout, clear-cache.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync, utimesSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { USAGE, buildInfo, main, readHidden } from "../src/cli-commands.js";
import { listCachedBuilds } from "../src/download.js";
import { RELEASE } from "../src/release.js";
import { startOrigin } from "./helpers/proxies.js";

const saved: Record<string, string | undefined> = {};
let home: string;
let cache: string;
const extraCleanups: Array<() => Promise<void>> = [];

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "cc-cli-home-"));
  cache = mkdtempSync(join(tmpdir(), "cc-cli-cache-"));
  for (const k of ["HOME", "USERPROFILE", "CLEARCOTE_CACHE", "CLEARCOTE_BINARY", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_RELEASE_CHANNEL", "CLEARCOTE_LICENSE_API"]) saved[k] = process.env[k];
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.CLEARCOTE_CACHE = cache;
  delete process.env.CLEARCOTE_BINARY;
  delete process.env.CLEARCOTE_LICENSE_KEY;
  delete process.env.CLEARCOTE_RELEASE_CHANNEL;
});
afterEach(async () => {
  for (const c of extraCleanups.splice(0)) await c();
  for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
  rmSync(home, { recursive: true, force: true });
  rmSync(cache, { recursive: true, force: true });
  vi.restoreAllMocks();
});

function fakeCachedBuild(tag: string, switches: string[] = []): string {
  const bin = process.platform === "win32" ? "chrome.exe" : "chrome";
  const dir = join(cache, tag, "browser", "sub");
  mkdirSync(dir, { recursive: true });
  const body = Buffer.concat([Buffer.from("x"), ...switches.map((s) => Buffer.from(`\0${s}\0`, "latin1"))]);
  writeFileSync(join(dir, bin), body);
  if (process.platform === "win32") writeFileSync(join(dir, "chrome.dll"), body);
  writeFileSync(join(cache, tag, ".verified"), "sha\n");
  return join(dir, bin);
}

function captureStdout(): { text: () => string } {
  let out = "";
  vi.spyOn(process.stdout, "write").mockImplementation(((chunk: string | Uint8Array) => { out += String(chunk); return true; }) as never);
  return { text: () => out };
}

describe("clearcote CLI", () => {
  it("documents every command and the info flags", () => {
    for (const c of ["install", "info", "doctor", "update", "clear-cache", "login", "logout", "serve", "cloud", "--quick", "--json", "--proxy"]) {
      expect(USAGE).toContain(c);
    }
  });

  it("listCachedBuilds only lists verified builds that still have a binary", () => {
    const good = fakeCachedBuild("pro-152.0.7977.82-r21");
    mkdirSync(join(cache, "half-downloaded", "browser"), { recursive: true }); // no .verified
    expect(listCachedBuilds(cache)).toEqual([{ tag: "pro-152.0.7977.82-r21", path: good }]);
  });

  it("info --quick: no network, no launch, reports cache + engine support + licence source", async () => {
    // the open build this SDK pins: what a keyless launch() runs
    const exe = fakeCachedBuild(RELEASE.tag, ["fingerprint-passthrough", "allow-third-party-cookies", "proxy-auth"]);
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const r = await buildInfo({ quick: true });
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(r.license).toEqual({ source: "none" });
    expect(r.binary).toMatchObject({ source: "cache", path: exe, tag: RELEASE.tag, releaseChannel: "stable" });
    expect(r.engineFeatures).toMatchObject({
      "fingerprint-passthrough": true, "allow-third-party-cookies": true, "proxy-auth": true,
      "transparent-proxy": false, "disable-fingerprint-voices": false,
    });
    expect(r.launch).toEqual({ tested: false, reason: "skipped (--quick)" });
  });

  it("info honours CLEARCOTE_BINARY and CLEARCOTE_RELEASE_CHANNEL", async () => {
    const exe = fakeCachedBuild("custom", []);
    process.env.CLEARCOTE_BINARY = exe;
    process.env.CLEARCOTE_RELEASE_CHANNEL = "preview";
    const r = await buildInfo({ quick: true });
    expect(r.binary).toMatchObject({ source: "CLEARCOTE_BINARY", path: exe, releaseChannel: "preview" });
  });

  it("info launch-tests the build launch() would use, not the newest cached one", async () => {
    // An open-build user with an old licensed build still cached: the newest cached build is the licensed one,
    // which cannot run without a key. info must test the open build a keyless launch() runs.
    const free = fakeCachedBuild(RELEASE.tag);
    const pro = fakeCachedBuild("pro-152.0.7977.82-r21");
    const old = new Date(Date.now() - 3600_000);
    utimesSync(join(cache, RELEASE.tag), old, old); // the licensed build is the newer one in the cache
    const tested: string[] = [];
    const launchFn = async (o: Record<string, unknown>) => {
      tested.push(o.executablePath as string);
      return { version: () => "149.0.1", close: async () => {} };
    };
    let r = await buildInfo({ quick: false }, launchFn);
    expect(tested).toEqual([free]);
    expect(r.binary).toMatchObject({ path: free, tag: RELEASE.tag });
    expect(r.binary.selectedBy).toContain("open build pinned by this SDK");
    expect(r.launch).toMatchObject({ tested: true, ok: true, build: RELEASE.tag });

    // with a key, launch() runs a licensed build: that one is tested
    process.env.CLEARCOTE_LICENSE_KEY = "cc_lic_info_pick_test_key";
    process.env.CLEARCOTE_LICENSE_API = "http://127.0.0.1:1"; // seats: unreachable, never a real server
    tested.length = 0;
    r = await buildInfo({ quick: false }, launchFn);
    expect(tested).toEqual([pro]);
    expect(r.launch).toMatchObject({ build: "pro-152.0.7977.82-r21" });
    expect(r.binary.selectedBy).toMatch(/^licensed/);
  });

  it("info, keyless, with only a licensed build cached does not launch it", async () => {
    fakeCachedBuild("pro-152.0.7977.82-r21");
    const r = await buildInfo({ quick: false }, async () => { throw new Error("launched"); });
    expect(r.launch).toEqual({ tested: false, reason: `the open build this SDK pins (${RELEASE.tag}) is not installed — run: clearcote install` });
  });

  it("info with nothing installed says how to install", async () => {
    const r = await buildInfo({ quick: false });
    expect(r.binary.source).toBe("none");
    expect(r.launch).toEqual({ tested: false, reason: "no binary installed — run: clearcote install" });
  });

  it("info --json prints parseable JSON", async () => {
    const out = captureStdout();
    await main(["info", "--quick", "--json"]);
    const parsed = JSON.parse(out.text());
    expect(parsed.sdk.version).toBeTruthy();
    expect(parsed.launch.tested).toBe(false);
  });

  it("login validates the key first, saves it; logout removes it", async () => {
    const api = await startOrigin((req) => (req.headers.authorization === "Bearer cc_lic_valid_key_1234"
      ? { body: '{"used":0,"limit":5,"plan":"team"}' }
      : { status: 401, body: '{"error":"Invalid license key."}' }));
    extraCleanups.push(() => api.close());
    process.env.CLEARCOTE_LICENSE_API = `http://127.0.0.1:${api.port}`;
    const out = captureStdout();
    await main(["login", "cc_lic_valid_key_1234"]);
    expect(out.text()).toMatch(/saved to .*license\.key/);
    expect(out.text()).toMatch(/valid: 0 of 5 seats in use, plan team/);
    expect(existsSync(join(home, ".clearcote", "license.key"))).toBe(true);
    await main(["logout"]);
    expect(existsSync(join(home, ".clearcote", "license.key"))).toBe(false);
  });

  it("readHidden reads a line in raw mode: nothing typed or pasted is echoed", async () => {
    const input = Object.assign(new PassThrough(), { isTTY: true, setRawMode: vi.fn() });
    let shown = "";
    const output = { write: (s: string) => { shown += s; return true; } };
    const got = readHidden(input, output, "Key: ");
    input.write("cc_lic_typed_kez\x7fy_1234"); // a typo fixed with Backspace
    input.write("\x1b[201~\r"); // the end of a bracketed paste, then Enter
    expect(await got).toBe("cc_lic_typed_key_1234");
    expect(shown).toBe("Key: \n"); // the prompt and a newline: none of the key
    expect(input.setRawMode.mock.calls).toEqual([[true], [false]]); // raw while reading, restored after
    const ctrlC = Object.assign(new PassThrough(), { isTTY: true, setRawMode: vi.fn() });
    const cancelled = readHidden(ctrlC, output, "Key: ");
    ctrlC.write("cc_lic_part\x03"); // Ctrl-C half way
    await expect(cancelled).rejects.toThrow("cancelled");
    expect(ctrlC.setRawMode).toHaveBeenLastCalledWith(false);
  });

  it("login with no key asks on the terminal without echoing it", async () => {
    const api = await startOrigin(() => ({ body: '{"used":0,"limit":5,"plan":"team"}' }));
    extraCleanups.push(() => api.close());
    process.env.CLEARCOTE_LICENSE_API = `http://127.0.0.1:${api.port}`;
    const tty = Object.assign(new PassThrough(), { isTTY: true, setRawMode: vi.fn() });
    const desc = Object.getOwnPropertyDescriptor(process, "stdin")!;
    Object.defineProperty(process, "stdin", { configurable: true, enumerable: true, get: () => tty });
    let errText = "";
    vi.spyOn(process.stderr, "write").mockImplementation(((c: string | Uint8Array) => { errText += String(c); return true; }) as never);
    const out = captureStdout();
    try {
      setTimeout(() => tty.write("cc_lic_typed_key_123456\r\n"), 20);
      await main(["login"]);
    } finally {
      Object.defineProperty(process, "stdin", desc);
    }
    expect(tty.setRawMode).toHaveBeenCalledWith(true); // the terminal does not echo what is pasted
    expect(tty.setRawMode).toHaveBeenLastCalledWith(false);
    expect(readFileSync(join(home, ".clearcote", "license.key"), "utf8").trim()).toBe("cc_lic_typed_key_123456");
    expect(out.text() + errText).not.toContain("cc_lic_typed_key_123456");
  });

  it("subcommand help: `clearcote login --help` lists --device and saves nothing", async () => {
    process.env.CLEARCOTE_LICENSE_API = "http://127.0.0.1:1";
    const out = captureStdout();
    await main(["login", "--help"]);
    expect(out.text()).toContain("clearcote login --device");
    expect(out.text()).toContain("clearcote login [key]");
    expect(out.text()).toContain("not shown");
    expect(existsSync(join(home, ".clearcote", "license.key"))).toBe(false); // "--help" is not a key
    for (const args of [["login", "-h"], ["logout", "--help"], ["info", "--help"], ["doctor", "-h"], ["serve", "--help"], ["install", "--help"], ["update", "--help"], ["clear-cache", "--help"]]) {
      const o = captureStdout();
      await main(args);
      const cmd = args[0] === "doctor" ? "info" : args[0];
      expect(o.text()).toContain(`clearcote ${cmd}`);
      expect(o.text()).toContain("USAGE");
    }
  });

  it("login refuses a key the server rejects and saves nothing", async () => {
    const api = await startOrigin(() => ({ status: 401, body: '{"error":"Invalid license key."}' }));
    extraCleanups.push(() => api.close());
    process.env.CLEARCOTE_LICENSE_API = `http://127.0.0.1:${api.port}`;
    const exit = vi.spyOn(process, "exit").mockImplementation(((code?: number) => { throw new Error(`exit ${code}`); }) as never);
    vi.spyOn(process.stderr, "write").mockImplementation((() => true) as never);
    await expect(main(["login", "cc_lic_bad"])).rejects.toThrow("exit 1");
    expect(exit).toHaveBeenCalledWith(1);
    expect(existsSync(join(home, ".clearcote", "license.key"))).toBe(false);
  });

  it("clear-cache removes cached builds only, never the root or unrelated files", async () => {
    fakeCachedBuild("pro-x");
    mkdirSync(join(cache, "pro-152.0.7977.82-r22", "browser"), { recursive: true }); // half-downloaded, no marker yet
    mkdirSync(join(cache, "my-notes"), { recursive: true });
    writeFileSync(join(cache, "my-notes", "keep.txt"), "x");
    writeFileSync(join(cache, "unrelated.txt"), "x");
    const out = captureStdout();
    await main(["clear-cache"]);
    expect(existsSync(join(cache, "pro-x"))).toBe(false);
    expect(existsSync(join(cache, "pro-152.0.7977.82-r22"))).toBe(false);
    expect(existsSync(join(cache, "my-notes", "keep.txt"))).toBe(true);
    expect(existsSync(join(cache, "unrelated.txt"))).toBe(true);
    expect(out.text()).toMatch(/removed 2 cached builds/);
  });

  it("clear-cache pointed at a home-like directory deletes nothing", async () => {
    const fakeHome = mkdtempSync(join(tmpdir(), "cc-cli-fakehome-"));
    mkdirSync(join(fakeHome, "Documents"));
    writeFileSync(join(fakeHome, ".bashrc"), "x");
    process.env.CLEARCOTE_CACHE = fakeHome;
    const out = captureStdout();
    await main(["clear-cache"]);
    expect(existsSync(join(fakeHome, "Documents"))).toBe(true);
    expect(existsSync(join(fakeHome, ".bashrc"))).toBe(true);
    expect(out.text()).toMatch(/no cached builds/);
    rmSync(fakeHome, { recursive: true, force: true });
  });

  it("serve --proxy splits credentials out of the URL (a creds-in-URL --proxy-server goes DIRECT)", async () => {
    const { proxyOption } = await import("../src/cli-commands.js");
    expect(proxyOption("http://user:p%40ss@gw.test:8000")).toEqual({ server: "http://gw.test:8000", username: "user", password: "p@ss" });
    expect(proxyOption("socks5://gw.test")).toEqual({ server: "socks5://gw.test:1080" });
  });
});
