// launch() on macOS runs the Clearcote Docker image (there is no native macOS build).
//
// The host platform is mocked (process.platform, only around the launch call) and the docker CLI is
// replaced by a recorder that answers like Docker would; the "container's" CDP endpoint is a real local
// Chromium, or a stand-in that answers /json/version, so the SDK's connect, close and the Playwright objects
// it returns are real. The real image is exercised end to end separately (CLEARCOTE_TEST_ONLY_ASSUME_MACOS,
// docker-launch.live.test.ts). Mirrors sdk/python/tests/test_docker_launch.py.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import http from "node:http";
import type net from "node:net";
import { homedir, hostname } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright-core";
import { DockerUnavailableError, launch } from "../src/index.js";
import {
  containerEnv, dockerCli, dockerRequested, hostProbe, legacyProxyRefusal, ownerAlive, ownerHere, ownerToken, pidAlive,
  processStart, removal, removeContainer, resetOwnerToken, startContainer, sweepStale, TEST_ONLY_ASSUME_MACOS, verifyApplied,
  type CliResult,
} from "../src/docker.js";
import { LocalChromium, findChromium } from "./helpers/chromium.js";
import { tempDir } from "./helpers/temp.js";

// installHumanize, made to fail on demand: a setup step after the connect (ESM exports cannot be spied on).
const hoisted = vi.hoisted(() => ({ failHumanize: false }));
vi.mock("../src/humanize.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("../src/humanize.js")>();
  return {
    ...real,
    installHumanize: (...a: Parameters<typeof real.installHumanize>) => {
      if (hoisted.failHumanize) throw new Error("humanize setup failed");
      return real.installHumanize(...a);
    },
  };
});

const SDK_VERSION = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version as string;
const IMAGE = `teamflatearth/clearcote:sdk-${SDK_VERSION}`;
const ENV = ["HOME", "USERPROFILE", "CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_DOCKER_IMAGE", "CLEARCOTE_CLOUD", "CLEARCOTE_LICENSE_KEY",
  "CLEARCOTE_DOCKER_IDLE_EXIT", "CLEARCOTE_DOCKER_CACHE_VOLUME", TEST_ONLY_ASSUME_MACOS];
const CHROMIUM = findChromium();
const DEAD_PID = 2 ** 22 + 12345; // above any pid_max: never a live process
const PROTOCOL = { "com.clearcotelabs.serve-protocol": "2" };
const GONE_ARGV = ["ps", "-a", "-q", "--no-trunc", "--filter", "id=c0ffee1234"]; // is the container still listed?
const REMOVAL = { ...removal };
const saved: Record<string, string | undefined> = {};

const serveState = (engine = "open", proxy: string | null = null, idle = 30, secrets = false, proxyAuth: string | null = null) =>
  `[clearcote] serve-state ${JSON.stringify({ engine, idle_exit: idle, protocol: 2, proxy, proxy_auth: proxyAuth, secrets_file: secrets })}`;

/** Stands in for the docker CLI: records every call (argv, the environment it was given, stdin). */
class FakeDocker {
  calls: Array<{ argv: string[]; env: Record<string, string>; input?: Buffer }> = [];
  cdpPort = 1;
  running = true;
  infoError = "";
  createError = "";
  pullError = "";
  labels: Record<string, string> | null = { ...PROTOCOL }; // null: not here until pulled
  logs = "";
  marks: string[] = [serveState()]; // what the entrypoint logged about what it applied
  ps = ""; // `docker ps` rows: id \t owner-token
  containers: Array<Record<string, string>> = []; // or: containers as {id, <label>: value}, rendered per --format
  // How many more `docker ps -a --filter id=` looks still list the container once it was stopped, as the daemon's
  // own --rm removal runs on after `docker stop` returns. -1: it never goes (a Dead container).
  lingering = 0;
  psError = ""; // `docker ps --filter id=` fails (Docker stopped answering)
  run = async (argv: string[], env?: Record<string, string>, _t?: number, input?: Buffer): Promise<CliResult> => {
    this.calls.push({ argv: [...argv], env: { ...(env ?? {}) }, input });
    switch (argv[1]) {
      case "info": return this.infoError ? { code: 1, stdout: "", stderr: this.infoError } : { code: 0, stdout: "29.1.3\n", stderr: "" };
      case "ps": {
        if (argv.some((a) => a.startsWith("id="))) {
          if (this.psError) return { code: 1, stdout: "", stderr: this.psError };
          const listed = this.listed();
          if (this.lingering > 0) this.lingering--;
          return { code: 0, stdout: listed ? "c0ffee1234\n" : "", stderr: "" };
        }
        if (!this.containers.length) return { code: 0, stdout: this.ps, stderr: "" };
        const names = [...argv[argv.indexOf("--format") + 1].matchAll(/\.Label "([^"]+)"/g)].map((m) => m[1]);
        return { code: 0, stdout: this.containers.map((c) => [c.id, ...names.map((n) => c[n] ?? "")].join("\t") + "\n").join(""), stderr: "" };
      }
      case "image":
        return this.labels === null ? { code: 1, stdout: "", stderr: `Error: No such image: ${argv.at(-1)}` } : { code: 0, stdout: JSON.stringify(Object.keys(this.labels).length ? this.labels : null) + "\n", stderr: "" };
      case "pull":
        if (this.pullError) return { code: 1, stdout: "", stderr: this.pullError };
        this.labels ??= { ...PROTOCOL };
        return { code: 0, stdout: "", stderr: "" };
      case "create": return this.createError ? { code: 125, stdout: "", stderr: this.createError } : { code: 0, stdout: "c0ffee1234\n", stderr: "" };
      case "port": return { code: 0, stdout: `127.0.0.1:${this.cdpPort}\n[::1]:${this.cdpPort}\n`, stderr: "" };
      case "inspect": return this.running ? { code: 0, stdout: "true\n", stderr: "" } : { code: 1, stdout: "", stderr: "Error: No such object: c0ffee1234" };
      default: return { code: 0, stdout: "", stderr: "" }; // cp, start, stop, rm
    }
  };
  /** What `docker ps -a` would answer about the container right now (once it was stopped). */
  listed = () => this.lingering !== 0;
  commands = () => this.calls.map((c) => c.argv[1]);
  call = (cmd: string) => this.calls.find((c) => c.argv[1] === cmd)!;
  stopsAndRms = () => this.calls.filter((c) => ["stop", "rm"].includes(c.argv[1])).map((c) => c.argv.slice(2));
}

let docker: FakeDocker;

beforeEach(() => {
  for (const k of ENV) saved[k] = process.env[k];
  for (const k of ENV) delete process.env[k];
  const home = tempDir("cc-docker-home-"); // never this machine's own saved licence key or owner records
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  resetOwnerToken();
  removal.pollMs = 10;
  docker = new FakeDocker();
  vi.spyOn(dockerCli, "which").mockReturnValue("docker");
  vi.spyOn(dockerCli, "run").mockImplementation(docker.run);
  vi.spyOn(dockerCli, "followLogs").mockImplementation(() => ({ text: async () => docker.logs, waitMarks: async () => [...docker.marks], stop: () => {} }));
  vi.spyOn(process.stderr, "write").mockImplementation((() => true) as never);
});
afterEach(() => {
  hoisted.failHumanize = false;
  Object.assign(removal, REMOVAL);
  resetOwnerToken();
  for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
  vi.restoreAllMocks();
});

/** Run `fn` with the host looking like macOS. */
async function onPlatform<T>(platform: string, fn: () => Promise<T> | T): Promise<T> {
  const desc = Object.getOwnPropertyDescriptor(process, "platform")!;
  Object.defineProperty(process, "platform", { ...desc, value: platform });
  try {
    return await fn();
  } finally {
    Object.defineProperty(process, "platform", desc);
  }
}
const onMac = <T>(fn: () => Promise<T> | T) => onPlatform("darwin", fn);
const after = (argv: string[], flag: string) => argv.filter((_, i) => i > 0 && argv[i - 1] === flag);

/** Answers /json/version like a browser: the container "came up". Its WebSocket is not there. */
async function cdpStub(): Promise<{ port: number; close: () => Promise<void> }> {
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ Browser: "x", webSocketDebuggerUrl: "ws://127.0.0.1:1/devtools/browser/x" }));
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
  return { port: (server.address() as net.AddressInfo).port, close: () => new Promise((r) => server.close(() => r())) };
}

/** Reads a one-file ustar archive (what `docker cp -` gets). */
function untar(buf: Buffer): { name: string; mode: number; uid: number; gid: number; data: string } {
  const field = (off: number, len: number) => buf.subarray(off, off + len).toString("ascii").replace(/\0.*$/s, "");
  const size = parseInt(field(124, 12), 8);
  let sum = 0;
  for (let i = 0; i < 512; i++) sum += i >= 148 && i < 156 ? 32 : buf[i];
  expect(parseInt(field(148, 8), 8)).toBe(sum); // a valid header checksum
  return { name: field(0, 100), mode: parseInt(field(100, 8), 8), uid: parseInt(field(108, 8), 8), gid: parseInt(field(116, 8), 8), data: buf.subarray(512, 512 + size).toString("utf8") };
}

const KEYED = { licenseKey: "cc_lic_docker_test_key_1234", proxy: { server: "http://proxy.example:8080", username: "u", password: "p w" } };
// what an image from before sdk-0.40.0 can still log in to: a SOCKS5 proxy, on the licensed engine
const KEYED_SOCKS = { licenseKey: "cc_lic_docker_test_key_1234", proxy: { server: "socks5://proxy.example:1080", username: "u", password: "pw-plain" } };
const ownersDir = () => join(homedir(), ".clearcote", "docker-owners");
function record(token: string, fields: Record<string, unknown>): string {
  mkdirSync(ownersDir(), { recursive: true });
  const path = join(ownersDir(), `${token}.json`);
  writeFileSync(path, JSON.stringify({ token, ...fields }));
  return path;
}
/** What a record written by this process carries besides pid and start (host, boot id, PID namespace). */
const here = () => ({ host: hostname(), boot: hostProbe.bootId(), pidns: hostProbe.pidNamespace() });

describe("launch() on macOS runs the Clearcote Docker image", () => {
  it("decides by platform, option, binary and environment", async () => {
    const on = (platform: string, docker?: unknown, opts: Record<string, unknown> = {}, env: Record<string, string> = {}) =>
      onPlatform(platform, () => {
        Object.assign(process.env, env);
        try { return dockerRequested(docker, opts); } finally { for (const k of Object.keys(env)) delete process.env[k]; }
      });
    expect(await on("darwin")).toBe(true);
    expect(await on("linux")).toBe(false);
    expect(await on("win32")).toBe(false);
    expect(await on("darwin", false)).toBe(false);
    expect(await on("darwin", "0")).toBe(false);
    expect(await on("linux", true)).toBe(true);
    expect(await on("darwin", undefined, { executablePath: "/opt/cc/chrome" })).toBe(false);
    expect(await on("darwin", undefined, {}, { CLEARCOTE_DOCKER: "0" })).toBe(false);
    expect(await on("linux", undefined, {}, { CLEARCOTE_DOCKER: "1" })).toBe(true);
    expect(await on("darwin", undefined, {}, { CLEARCOTE_BINARY: "/opt/cc/chrome" })).toBe(false);
    expect(await on("linux", undefined, {}, { [TEST_ONLY_ASSUME_MACOS]: "1" })).toBe(true);
  });

  it.skipIf(!CHROMIUM)("starts the image, connects, and resolves to a working Playwright Browser", async () => {
    docker.marks = [serveState("licensed", "http://proxy.example:8080", 30, true, "engine")];
    const local = await new LocalChromium().start(CHROMIUM!);
    try {
      docker.cdpPort = Number(/:(\d+)$/.exec(local.httpUrl)![1]);
      const browser = await onMac(() => launch({
        fingerprint: "seed-1", platform: "windows", timezone: "Europe/Berlin", hardwareConcurrency: 8,
        canvasNoise: false, headless: true, args: ["--lang=de-DE"], humanize: true, ...KEYED,
      }));
      try {
        const direct = await chromium.connectOverCDP(local.httpUrl);
        expect(browser.constructor).toBe(direct.constructor); // the same Playwright Browser type
        await direct.close();
        expect(browser.isConnected()).toBe(true);
        expect((browser as unknown as { dockerContainer: unknown }).dockerContainer).toEqual({
          id: "c0ffee1234", image: IMAGE, endpoint: `http://127.0.0.1:${docker.cdpPort}`, serveProtocol: 2,
        });
        const page = await browser.newPage();
        expect((page as unknown as { _clearcotePersona?: unknown })._clearcotePersona).toBeDefined(); // humanized, as locally
        await page.setContent("<title>in docker</title>");
        expect(await page.title()).toBe("in docker");
        expect(page.viewportSize()).toBeNull(); // no emulated viewport over the container's real window
      } finally {
        docker.lingering = 2; // still listed for two looks after `docker stop`, as under load
        await browser.close();
      }
      expect(browser.isConnected()).toBe(false);
      expect(local.alive()).toBe(true); // close() disconnects; ending the browser is the container's stop
      expect(docker.listed()).toBe(false); // close() resolved once the container was gone
    } finally {
      await local.stop();
    }
    expect(docker.commands()).toEqual(["info", "ps", "image", "create", "cp", "start", "port", "stop", "rm", "ps", "ps", "ps"]);
    expect(docker.calls.at(-1)!.argv.slice(1)).toEqual(GONE_ARGV);
    const { argv, env } = docker.call("create");
    // --rm: a stopped container (and its anonymous engine volume) goes away by itself
    expect(argv.slice(0, 6)).toEqual(["docker", "create", "--rm", "--platform", "linux/amd64", "--shm-size"]);
    expect(argv[argv.indexOf("-p") + 1]).toBe("127.0.0.1::9222"); // loopback only: CDP is full control
    expect(argv[argv.length - 1]).toBe(IMAGE);
    expect(argv[argv.indexOf("-v") + 1]).toBe("clearcote-cache:/opt/xdg-cache"); // licensed engine downloads once
    // owned through this process's token: the next launch removes it once this process is gone
    const lab = after(argv, "--label");
    expect(lab.slice(0, 2)).toEqual(["com.clearcotelabs.sdk-launch=1", `com.clearcotelabs.owner-host=${hostname()}`]);
    expect(lab[2]).toMatch(/^com\.clearcotelabs\.owner-token=[0-9a-f]{32}$/);
    const rec = JSON.parse(readFileSync(join(ownersDir(), `${lab[2].split("=")[1]}.json`), "utf8"));
    expect(rec).toMatchObject({ pid: process.pid, sdk: "node" });
    const eNames = after(argv, "-e");
    expect(eNames.every((a) => !a.includes("="))).toBe(true); // values travel in the environment only
    expect(argv.some((a) => a.includes("cc_lic_docker_test_key_1234") || a.includes("p w") || a.includes("p%20w"))).toBe(false);
    expect(env).toEqual({
      CC_FINGERPRINT: "seed-1", CC_PLATFORM: "windows", CC_TIMEZONE: "Europe/Berlin",
      CC_HARDWARE_CONCURRENCY: "8", CC_CANVAS_NOISE: "0", CC_HEADLESS: "1",
      CC_EXTRA_ARGS: "--lang=de-DE", CC_IDLE_EXIT_SECONDS: "30", CC_SECRETS_FILE: "/tmp/clearcote-secrets.json",
    });
    expect([...eNames].sort()).toEqual(Object.keys(env).sort());
    // the licence key and the proxy URL go in as a file only the image's user can read
    const cp = docker.call("cp");
    expect(cp.argv.slice(2)).toEqual(["-", "c0ffee1234:/tmp"]);
    expect(cp.env).toEqual({});
    const file = untar(cp.input!);
    expect([file.name, file.uid, file.gid, file.mode]).toEqual(["clearcote-secrets.json", 10001, 10001, 0o600]);
    expect(JSON.parse(file.data)).toEqual({ CLEARCOTE_LICENSE_KEY: "cc_lic_docker_test_key_1234", CC_PROXY: "http://u:p%20w@proxy.example:8080" });
    expect(docker.stopsAndRms()).toEqual([["--time", "10", "c0ffee1234"], ["-f", "-v", "c0ffee1234"]]); // -v: the anonymous VOLUME too
  }, 60_000); // a real Chromium starts and is connected to: well past the 5 s default on a loaded machine

  it.skipIf(!CHROMIUM)("nothing asked for: the image's own defaults, no cache volume, no file; dockerImage picks the image", async () => {
    process.env.CLEARCOTE_DOCKER_IDLE_EXIT = "0"; // 0: never stops on its own
    docker.marks = [serveState("open", null, 0)];
    const local = await new LocalChromium().start(CHROMIUM!);
    try {
      docker.cdpPort = Number(/:(\d+)$/.exec(local.httpUrl)![1]);
      const browser = await onMac(() => launch({ dockerImage: "example/clearcote:test" }));
      docker.lingering = 2;
      await browser.close();
      expect(docker.listed()).toBe(false);
    } finally {
      await local.stop();
    }
    expect(docker.call("create").argv.at(-1)).toBe("example/clearcote:test");
    expect(docker.call("create").env).toEqual({});
    expect(docker.call("create").argv).not.toContain("-v");
    expect(docker.commands()).not.toContain("cp");
  }, 60_000);

  // ── what an image understands, and what it applied ──────────────────────────────────────────────

  it("an image without the protocol label gets plain variables and a warning", async () => {
    // An image older than sdk-0.40.0 ignores CC_SECRETS_FILE: handing it the key and proxy as a file would start
    // it on the open engine with no proxy. It gets the variables it understands instead, and a warning.
    const stub = await cdpStub();
    let errText = "";
    vi.spyOn(process.stderr, "write").mockImplementation(((c: string | Uint8Array) => { errText += String(c); return true; }) as never);
    try {
      docker.cdpPort = stub.port;
      docker.labels = {};
      docker.marks = ["[clearcote] engine: /opt/xdg-cache/clearcote/pro-154.0.8037.57-r30/browser/chrome (licensed)", "[clearcote] proxy: socks5://proxy.example:1080"];
      const c = await startContainer({ ...KEYED_SOCKS });
      expect(c.serveProtocol).toBe(0);
    } finally {
      await stub.close();
    }
    const { argv, env } = docker.call("create");
    expect(env.CLEARCOTE_LICENSE_KEY).toBe("cc_lic_docker_test_key_1234");
    expect(env.CC_PROXY).toBe("socks5://u:pw-plain@proxy.example:1080");
    expect(env.CC_SECRETS_FILE).toBeUndefined();
    expect(env.CC_IDLE_EXIT_SECONDS).toBeUndefined();
    expect(docker.commands()).not.toContain("cp");
    expect(argv.some((a) => a.includes("cc_lic_docker_test_key_1234") || a.includes("p%20w"))).toBe(false); // still not argv
    expect(errText).toContain("predates sdk-0.40.0");
    expect(errText).toContain("docker inspect");
    expect(errText).toContain("will not stop on its own");
  });

  for (const [protocol, marks, problem] of [
    [2, [serveState("open", "http://proxy.example:8080", 30, true, "relay")], "it runs the open engine"],
    [2, [serveState("licensed", null, 30, true)], "did not apply it"],
    [2, [], "did not report what it applied"],
    // the proxy needs a password and the container did not say it can log in to it: every request would fail
    [2, [serveState("licensed", "http://proxy.example:8080", 30, true)], "did not say it can log in"],
    [0, ["[clearcote] engine: /opt/xdg-cache/clearcote/v0.1.0-pre.23/browser/chrome (free)", "[clearcote] proxy: socks5://proxy.example:1080"], "it runs the open engine"],
    [0, ["[clearcote] engine: /opt/xdg-cache/clearcote/pro-154.0.8037.57-r30/browser/chrome (licensed)"], "did not apply it"],
    [0, ["[clearcote] engine: /opt/xdg-cache/clearcote/pro-154.0.8037.57-r30/browser/chrome (licensed)", "[clearcote] proxy: socks5://other.example:3128"], "a different proxy"],
  ] as Array<[number, string[], string]>) {
    it(`a container that did not apply the key or proxy is refused and removed (protocol ${protocol}: ${problem})`, async () => {
      const stub = await cdpStub();
      try {
        docker.cdpPort = stub.port;
        docker.labels = protocol ? { ...PROTOCOL } : {};
        docker.marks = marks;
        const err = await startContainer({ ...(protocol ? KEYED : KEYED_SOCKS), quiet: true }).then(() => null, (e) => e as Error);
        expect(err!.message).toContain("did not apply what launch() asked for");
        expect(err!.message).toContain(problem);
        expect(err!.message).toContain("stopped and removed");
        expect(err!.message).not.toMatch(/cc_lic_docker_test_key_1234|p%20w/);
      } finally {
        await stub.close();
      }
      expect(docker.commands().slice(-3)).toEqual(["stop", "rm", "ps"]);
    });
  }

  it("a missing image is pulled before its protocol is read", async () => {
    const stub = await cdpStub();
    try {
      docker.cdpPort = stub.port;
      docker.labels = null;
      const c = await startContainer({ quiet: true });
      expect(c.serveProtocol).toBe(2);
    } finally {
      await stub.close();
    }
    expect(docker.commands().slice(0, 6)).toEqual(["info", "ps", "image", "pull", "image", "create"]);
    expect(docker.call("pull").argv.slice(2)).toEqual(["--platform", "linux/amd64", IMAGE]);
  });

  it("an image not published yet says what to do (Docker 29's wording)", async () => {
    docker.labels = null;
    docker.pullError = `Error response from daemon: failed to resolve reference "docker.io/${IMAGE}": docker.io/${IMAGE}: not found\n`;
    const err = await onMac(() => launch().then(() => null, (e) => e as Error));
    expect(err!.message).toContain(`the Clearcote image ${IMAGE} is not available`);
    expect(err!.message).toContain("published a few minutes after its SDK release");
    expect(err!.message).toContain("older than sdk-0.40.0 still works, but"); // the trade-off, said
    expect(docker.commands()).not.toContain("create");
  });

  for (const [proxy, licensed] of [
    [{ server: "http://proxy.example:8080", username: "u", password: "p w" }, true],
    ["https://u:p%20w@proxy.example:8443", true],
    [{ server: "socks5://proxy.example:1080", username: "u", password: "p w" }, false],
    // its licensed engine takes a SOCKS5 password, but the image hands it over as written in the URL: escaped
    [{ server: "socks5://proxy.example:1080", username: "u", password: "p@ss w" }, true],
  ] as Array<[unknown, boolean]>) {
    it(`an old image is refused a proxy it cannot log in to, before it starts (${JSON.stringify(proxy).slice(0, 40)})`, async () => {
      // An image from before sdk-0.40.0 drops an http(s) proxy's password (Chrome is challenged and nothing
      // answers: every request fails), and only its licensed engine takes a SOCKS5 one. Said up front instead.
      docker.labels = {};
      const err = await startContainer({ proxy, ...(licensed ? { licenseKey: "cc_lic_docker_test_key_1234" } : {}), quiet: true })
        .then(() => null, (e) => e as Error);
      expect(err!.message).toMatch(/predates sdk-0\.40\.0 and cannot use .* needs a password/);
      expect(err!.message).not.toMatch(/p w|p%20w/);
      expect(docker.commands()).not.toContain("create");
    });
  }

  // The same table is in the Python and .NET tests: the three SDKs read a proxy's login alike. [proxy, licensed,
  // whether it carries a login, whether an image from before sdk-0.40.0 is refused it]
  for (const [proxy, licensed, login, refused] of [
    ["socks5://u:p^w@proxy.example:1080", true, true, false], // written unescaped: an old image passes it on as is
    ["socks5://u:p w@proxy.example:1080", true, true, false],
    ["socks5://u:p@ss@proxy.example:1080", true, true, false], // an unescaped '@': the last one ends the login
    ["socks5://u:p%40ss@proxy.example:1080", true, true, true], // escaped: an old image would send "p%40ss"
    [{ server: "socks5://proxy.example:1080", username: "u", password: "pa!s*s'()" }, true, true, true],
    [{ server: "socks5://proxy.example:1080", username: "u", password: "p-._~ss" }, true, true, false],
    ["socks5://u:p w@proxy.example:1080", false, true, true], // the open engine cannot log in to SOCKS5
    ["http://:@proxy.example:8080", false, false, false], // neither a username nor a password
    ["http://u:@proxy.example:8080", true, true, true], // an old image drops an http(s) proxy's login
  ] as Array<[unknown, boolean, boolean, boolean]>) {
    it(`the SDKs read a proxy login alike (${JSON.stringify(proxy)}, ${licensed ? "licensed" : "open"})`, () => {
      const url = containerEnv({ proxy }).CC_PROXY;
      expect(legacyProxyRefusal(IMAGE, url, licensed) !== null).toBe(refused);
      const applied = url.startsWith("socks5") ? "socks5://proxy.example:1080" : "http://proxy.example:8080";
      expect(verifyApplied([serveState(licensed ? "licensed" : "open", applied)], 2, { licensed, proxy: url })).toEqual(
        login ? ["the proxy needs a password, but it did not say it can log in to it (every request through it would fail)"] : []);
    });
  }

  it("a proxy password is accepted when the container logs in to it", async () => {
    const stub = await cdpStub();
    try {
      docker.cdpPort = stub.port;
      for (const how of ["engine", "relay"]) {
        docker.marks = [serveState("open", "http://proxy.example:8080", 30, true, how)];
        const c = await startContainer({ proxy: KEYED.proxy, quiet: true });
        expect(c.id).toBe("c0ffee1234");
      }
    } finally {
      await stub.close();
    }
  });

  it("an unreachable registry is not called an unpublished image (Docker 29)", async () => {
    // Docker 29 opens a registry it cannot reach with the same "failed to resolve reference" as a tag that is not
    // there: offline users of the default tag were told it "is not published yet".
    docker.labels = null;
    docker.pullError = `Error response from daemon: failed to resolve reference "docker.io/${IMAGE}": failed to do request: Head "https://registry-1.docker.io/v2/teamflatearth/clearcote/manifests/sdk-${SDK_VERSION}": dial tcp: lookup registry-1.docker.io on 192.168.65.7:53: no such host\n`;
    const err = await onMac(() => launch().then(() => null, (e) => e as Error));
    expect(err!.message).toContain("registry did not answer");
    expect(err!.message).toContain("network");
    expect(err!.message).toContain("no such host");
    expect(err!.message).not.toContain("not available");
    expect(err!.message).not.toContain("published a few minutes");
  });

  for (const e of [
    "Error response from daemon: manifest for x:y not found: manifest unknown: manifest unknown",
    "Error response from daemon: pull access denied for x, repository does not exist or may require 'docker login'",
  ]) {
    it(`older Docker wordings for a missing image: ${e.slice(28, 60)}`, async () => {
      docker.labels = null;
      docker.pullError = e;
      await expect(onMac(() => launch())).rejects.toThrow(/is not available/);
    });
  }

  // ── close(): the container is gone when it resolves ─────────────────────────────────────────────

  it("removeContainer waits until the container is gone", async () => {
    // The daemon's own --rm removal runs on after `docker stop` returns, and `docker rm` meanwhile answers
    // "removal ... already in progress" at once: `docker ps -a` still listed the container after close().
    docker.lingering = 3;
    await removeContainer("docker", "c0ffee1234");
    expect(docker.listed()).toBe(false);
    expect(docker.commands()).toEqual(["stop", "rm", "ps", "ps", "ps", "ps"]);
    expect(docker.calls[1].argv.slice(2)).toEqual(["-f", "-v", "c0ffee1234"]); // -v: the image's anonymous VOLUME too
    expect(docker.calls.at(-1)!.argv.slice(1)).toEqual(GONE_ARGV);
  });

  it("removeContainer gives up after its wait and removes a container that stayed", async () => {
    // A removal that failed leaves the container listed (Dead): close() does not hang or throw over it, and asks
    // once more for it to go, with its anonymous volume.
    removal.waitMs = 300;
    docker.lingering = -1;
    const started = Date.now();
    await removeContainer("docker", "c0ffee1234");
    expect(Date.now() - started).toBeLessThan(5000);
    const cmds = docker.commands();
    expect([cmds.slice(0, 2), cmds.at(-1), [...new Set(cmds.slice(2, -1))]]).toEqual([["stop", "rm"], "rm", ["ps"]]);
    expect(cmds.length).toBeGreaterThan(4);
    expect(docker.calls.at(-1)!.argv.slice(2)).toEqual(["-f", "-v", "c0ffee1234"]);
  });

  it("removeContainer does not wait on a Docker that stopped answering", async () => {
    docker.lingering = -1;
    docker.psError = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n";
    const started = Date.now();
    await removeContainer("docker", "c0ffee1234");
    expect(Date.now() - started).toBeLessThan(5000);
    expect(docker.commands()).toEqual(["stop", "rm", "ps"]);
  });

  // ── who owns a container ────────────────────────────────────────────────────────────────────────

  it("sweeps only containers whose owner is certainly gone", async () => {
    const t = (c: string) => c.repeat(32);
    const gone = record(t("a"), { pid: DEAD_PID, start: null, ...here() }); // owner gone: swept
    record(t("b"), { pid: process.pid, start: processStart(process.pid), ...here() }); // owner alive: kept
    const elsewhere = record(t("d"), { pid: DEAD_PID, ...here(), boot: "another-boot-entirely" }); // another boot: kept
    // t("e"): no record here (another machine, a container given the Docker socket, WSL2): kept
    docker.ps = ["a", "b", "d", "e"].map((c) => `${c.repeat(6)}\t${t(c)}\n`).join("") + "fff111\t\n"; // no token (older SDK): kept
    expect(await sweepStale("docker")).toEqual(["aaaaaa"]);
    expect(docker.stopsAndRms()[0]).toEqual(["--time", "10", "aaaaaa"]);
    expect(existsSync(gone)).toBe(false); // its record goes too
    expect(existsSync(elsewhere)).toBe(true);
  });

  it("a process in another namespace with this host name is left alone", async () => {
    // The reviewer's case: a process in a container started with --network host and the Docker socket has this
    // machine's host name, and a pid that may not exist here. Host name + pid called it dead and removed its
    // live container. Its owner record is in its own filesystem, not here: never touched.
    docker.containers = [{
      id: "live99", "com.clearcotelabs.sdk-launch": "1", "com.clearcotelabs.owner-host": hostname(),
      "com.clearcotelabs.owner-pid": String(DEAD_PID), "com.clearcotelabs.owner-token": "9".repeat(32),
    }];
    expect(await sweepStale("docker")).toEqual([]);
    expect(docker.commands()).not.toContain("stop");
    expect(docker.commands()).not.toContain("rm");
  });

  it("owner record", () => {
    const token = ownerToken();
    expect(ownerToken()).toBe(token); // one per process
    const rec = JSON.parse(readFileSync(join(ownersDir(), `${token}.json`), "utf8"));
    expect(rec).toMatchObject({ pid: process.pid, sdk: "node", start: processStart(process.pid), ...here() });
    expect(ownerHere()).toEqual(here());
    expect(ownerAlive(rec)).toBe(true);
    expect(ownerAlive({ ...rec, pid: DEAD_PID })).toBe(false);
    if (rec.start) expect(ownerAlive({ ...rec, start: `${rec.start.split(":")[0]}:1` })).toBe(false); // the same pid, another process
  });

  for (const pid of [0, -1, -4242, 2 ** 31, 99999999999]) {
    it(`a record whose pid is not a positive number is left alone (${pid})`, async () => {
      // pid 0 / negative pids name a process group or nothing at all: "not alive" proves nothing about the owner. No
      // process has a pid past 2**31-1: process.kill() threw on it, which read as "gone", and the container was removed.
      record("a".repeat(32), { pid, start: null, ...here() });
      docker.ps = `aaaaaa\t${"a".repeat(32)}\n`;
      expect(ownerAlive({ pid, ...here() })).toBeNull();
      expect(await sweepStale("docker")).toEqual([]);
      expect(docker.commands()).not.toContain("stop");
    });
  }

  for (const [label, fields] of [
    ["written on macOS/Windows into a shared home", {}],
    ["no PID namespace", { boot: "linux-boot-1" }],
    ["no boot id", { pidns: "pid:[4026531836]" }],
    ["another host", { boot: "linux-boot-1", pidns: "pid:[4026531836]", host: "another-machine" }],
  ] as Array<[string, Record<string, unknown>]>) {
    it(`a record that cannot be fully verified here is left alone (${label})`, async () => {
      // This process is on Linux (boot id + PID namespace). A record that lacks either, or names another host, was
      // judged by its pid alone, and its live container removed when that pid was free here.
      vi.spyOn(hostProbe, "bootId").mockReturnValue("linux-boot-1");
      vi.spyOn(hostProbe, "pidNamespace").mockReturnValue("pid:[4026531836]");
      const rec = { pid: DEAD_PID, start: "ps-utc:Tue Oct  6 08:00:00 2026", host: hostname(), ...fields };
      record("e".repeat(32), rec);
      docker.ps = `eeeeee\t${"e".repeat(32)}\n`;
      expect(ownerAlive(rec)).toBeNull();
      expect(await sweepStale("docker")).toEqual([]);
      expect(docker.commands()).not.toContain("stop");
    });
  }

  it("the start marker is the same in every time zone and locale", async () => {
    // macOS has no /proc: the start time comes from `ps -o lstart=`, which prints local time in the locale's words.
    // An owner launched with TZ=UTC and a sweeper with TZ=Asia/Tokyo read different strings for one process, and the
    // sweeper removed the live container as a reused pid (so did one script changing TZ between launches).
    vi.spyOn(hostProbe, "readText").mockReturnValue(null); // no /proc here
    vi.spyOn(hostProbe, "platform").mockReturnValue("darwin");
    vi.spyOn(hostProbe, "spawnSync").mockImplementation(((_cmd: string, _args: string[], opts?: { env?: NodeJS.ProcessEnv }) => {
      const e = opts?.env ?? process.env; // what the ps child would see
      return { status: 0, stdout: `started 2026-10-06 08:00:00 UTC, printed for TZ=${e.TZ} LC_ALL=${e.LC_ALL}\n`, stderr: "" };
    }) as never);
    vi.spyOn(process, "kill").mockImplementation((() => true) as never); // pid 4242 is alive
    const saveTz = process.env.TZ;
    const saveLc = process.env.LC_ALL;
    try {
      process.env.TZ = "UTC";
      process.env.LC_ALL = "en_US.UTF-8";
      const rec = { ...here(), pid: 4242, start: processStart(4242) };
      expect(rec.start).toBeTruthy();
      process.env.TZ = "Asia/Tokyo";
      process.env.LC_ALL = "de_DE.UTF-8";
      expect(processStart(4242)).toBe(rec.start);
      expect(ownerAlive(rec)).toBe(true);
      record("f".repeat(32), rec);
      docker.ps = `ffffff\t${"f".repeat(32)}\n`;
      expect(await sweepStale("docker")).toEqual([]);
    } finally {
      if (saveTz === undefined) delete process.env.TZ; else process.env.TZ = saveTz;
      if (saveLc === undefined) delete process.env.LC_ALL; else process.env.LC_ALL = saveLc;
    }
  });

  it("pidAlive", () => {
    expect(pidAlive(process.pid)).toBe(true);
    expect(pidAlive(DEAD_PID)).toBe(false);
  });

  it("the idle exit and the cache volume can be tuned", async () => {
    process.env.CLEARCOTE_DOCKER_IDLE_EXIT = "7";
    process.env.CLEARCOTE_DOCKER_CACHE_VOLUME = "my-cc-cache";
    docker.running = false;
    await expect(onMac(() => launch({ licenseKey: "cc_lic_x" }))).rejects.toThrow();
    const { argv, env } = docker.call("create");
    expect(env.CC_IDLE_EXIT_SECONDS).toBe("7");
    expect(argv[argv.indexOf("-v") + 1]).toBe("my-cc-cache:/opt/xdg-cache");
    process.env.CLEARCOTE_DOCKER_IDLE_EXIT = "soon";
    await expect(onMac(() => launch())).rejects.toThrow(/CLEARCOTE_DOCKER_IDLE_EXIT/);
  });

  it("Docker not installed: says so, and how to turn this off", async () => {
    vi.spyOn(dockerCli, "which").mockReturnValue(null);
    const err = await onMac(() => launch().then(() => null, (e) => e as Error));
    expect(err).toBeInstanceOf(DockerUnavailableError);
    expect(err!.message).toContain("no native macOS build");
    expect(err!.message).toContain("`docker` command was not found");
    expect(err!.message).toContain("Install Docker Desktop");
    expect(err!.message).toContain("docker: false");
    expect(err!.message).toContain("CLEARCOTE_DOCKER=0");
    expect(docker.calls).toEqual([]);
  });

  it("Docker not running: says so", async () => {
    docker.infoError = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n";
    const err = await onMac(() => launch().then(() => null, (e) => e as Error));
    expect(err).toBeInstanceOf(DockerUnavailableError);
    expect(err!.message).toContain("Docker is not running");
    expect(err!.message).toContain("Cannot connect to the Docker daemon");
    expect(err!.message).toContain("Start Docker Desktop");
    expect(docker.commands()).toEqual(["info"]);
  });

  it("an option the image cannot take is refused before docker runs", async () => {
    await expect(onMac(() => launch({ userDataDir: "/tmp/profile" }))).rejects.toThrow(/^userDataDir is not available when launch\(\) runs Clearcote in Docker/);
    await expect(onMac(() => launch({ extensions: ["/tmp/ext"] }))).rejects.toThrow(/^extensions is not available/);
    await expect(onMac(() => launch({ args: ["--window-name=two words"] }))).rejects.toThrow(/whitespace/);
    expect(docker.calls).toEqual([]);
  });

  it("docker: false, a named binary, or CLEARCOTE_DOCKER=0 keeps the local launch", async () => {
    const missing = join(tempDir("cc-docker-nobin-"), "no-such-chrome");
    for (const o of [{ docker: false, executablePath: missing }, { executablePath: missing }]) {
      await expect(onMac(() => launch(o))).rejects.toThrow("Clearcote binary not found");
    }
    process.env.CLEARCOTE_DOCKER = "0";
    process.env.CLEARCOTE_BINARY = missing;
    await expect(onMac(() => launch())).rejects.toThrow("Clearcote binary not found");
    expect(docker.calls).toEqual([]); // the local path, untouched
  });

  it("a container that stops early reports its logs and is removed", async () => {
    docker.running = false;
    docker.logs = "[clearcote] ERROR: could not lease a run token (LicenseError: Invalid license key.).\n";
    const err = await onMac(() => launch({ licenseKey: "cc_lic_bad" }).then(() => null, (e) => e as Error));
    expect(err!.message).toContain("stopped before its browser came up");
    expect(err!.message).toContain("could not lease a run token");
    expect(docker.commands().slice(-3)).toEqual(["stop", "rm", "ps"]);
  });

  it("a failed create names the image", async () => {
    process.env.CLEARCOTE_DOCKER_IMAGE = "example/other:tag";
    docker.createError = "docker: Error response from daemon: Conflict. The container name is already in use.\n";
    await expect(onMac(() => launch())).rejects.toThrow(/`docker create example\/other:tag` failed: docker: Error response/);
  });

  it("a failed connect after the container started removes it", async () => {
    const stub = await cdpStub();
    try {
      docker.cdpPort = stub.port;
      await expect(onMac(() => launch({ timeout: 5000 }))).rejects.toThrow();
    } finally {
      await stub.close();
    }
    expect(docker.commands().slice(-3)).toEqual(["stop", "rm", "ps"]);
    expect(docker.calls.filter((c) => ["stop", "rm"].includes(c.argv[1])).map((c) => c.argv.at(-1))).toEqual(["c0ffee1234", "c0ffee1234"]);
  }, 60_000);

  it.skipIf(!CHROMIUM)("a failure after the connect disconnects and removes it", async () => {
    hoisted.failHumanize = true;
    const local = await new LocalChromium().start(CHROMIUM!);
    try {
      docker.cdpPort = Number(/:(\d+)$/.exec(local.httpUrl)![1]);
      await expect(onMac(() => launch())).rejects.toThrow("humanize setup failed");
      expect(local.alive()).toBe(true);
    } finally {
      await local.stop();
    }
    expect(docker.commands().slice(-3)).toEqual(["stop", "rm", "ps"]);
  }, 60_000);
});
