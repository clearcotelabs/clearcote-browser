// launch() on macOS runs the Clearcote Docker image (there is no native macOS build).
//
// The host platform is mocked (process.platform, only around the launch call) and the docker CLI is
// replaced by a recorder that answers like Docker would; the "container's" CDP endpoint is a real local
// Chromium, so the SDK's connect, close and the Playwright objects it returns are all real. The real
// image is exercised end to end separately (CLEARCOTE_TEST_ONLY_ASSUME_MACOS, docker-launch.live.test.ts).
// Mirrors sdk/python/tests/test_docker_launch.py.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { readFileSync } from "node:fs";
import http from "node:http";
import type net from "node:net";
import { hostname } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright-core";
import { DockerUnavailableError, launch } from "../src/index.js";
import { dockerCli, dockerRequested, pidAlive, TEST_ONLY_ASSUME_MACOS, type CliResult } from "../src/docker.js";
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
const saved: Record<string, string | undefined> = {};

/** Stands in for the docker CLI: records every call (argv, the environment it was given, stdin). */
class FakeDocker {
  calls: Array<{ argv: string[]; env: Record<string, string>; input?: Buffer }> = [];
  cdpPort = 1;
  running = true;
  infoError = "";
  createError = "";
  logs = "";
  ps = ""; // `docker ps` rows: id \t owner-host \t owner-pid
  run = async (argv: string[], env?: Record<string, string>, _t?: number, input?: Buffer): Promise<CliResult> => {
    this.calls.push({ argv: [...argv], env: { ...(env ?? {}) }, input });
    switch (argv[1]) {
      case "info": return this.infoError ? { code: 1, stdout: "", stderr: this.infoError } : { code: 0, stdout: "29.1.3\n", stderr: "" };
      case "ps": return { code: 0, stdout: this.ps, stderr: "" };
      case "create": return this.createError ? { code: 125, stdout: "", stderr: this.createError } : { code: 0, stdout: "c0ffee1234\n", stderr: "" };
      case "port": return { code: 0, stdout: `127.0.0.1:${this.cdpPort}\n[::1]:${this.cdpPort}\n`, stderr: "" };
      case "inspect": return this.running ? { code: 0, stdout: "true\n", stderr: "" } : { code: 1, stdout: "", stderr: "Error: No such object: c0ffee1234" };
      default: return { code: 0, stdout: "", stderr: "" }; // cp, start, stop, rm
    }
  };
  commands = () => this.calls.map((c) => c.argv[1]);
  call = (cmd: string) => this.calls.find((c) => c.argv[1] === cmd)!;
}

let docker: FakeDocker;

beforeEach(() => {
  for (const k of ENV) saved[k] = process.env[k];
  for (const k of ENV) delete process.env[k];
  const home = tempDir("cc-docker-home-"); // never this machine's own saved licence key
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  docker = new FakeDocker();
  vi.spyOn(dockerCli, "which").mockReturnValue("docker");
  vi.spyOn(dockerCli, "run").mockImplementation(docker.run);
  vi.spyOn(dockerCli, "followLogs").mockImplementation(() => ({ text: async () => docker.logs, stop: () => {} }));
  vi.spyOn(process.stderr, "write").mockImplementation((() => true) as never);
});
afterEach(() => {
  hoisted.failHumanize = false;
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

/** Reads a one-file ustar archive (what `docker cp -` gets). */
function untar(buf: Buffer): { name: string; mode: number; uid: number; gid: number; data: string } {
  const field = (off: number, len: number) => buf.subarray(off, off + len).toString("ascii").replace(/\0.*$/s, "");
  const size = parseInt(field(124, 12), 8);
  let sum = 0;
  for (let i = 0; i < 512; i++) sum += i >= 148 && i < 156 ? 32 : buf[i];
  expect(parseInt(field(148, 8), 8)).toBe(sum); // a valid header checksum
  return { name: field(0, 100), mode: parseInt(field(100, 8), 8), uid: parseInt(field(108, 8), 8), gid: parseInt(field(116, 8), 8), data: buf.subarray(512, 512 + size).toString("utf8") };
}

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
    const local = await new LocalChromium().start(CHROMIUM!);
    try {
      docker.cdpPort = Number(/:(\d+)$/.exec(local.httpUrl)![1]);
      const browser = await onMac(() => launch({
        fingerprint: "seed-1", platform: "windows", timezone: "Europe/Berlin", hardwareConcurrency: 8,
        canvasNoise: false, headless: true, args: ["--lang=de-DE"], humanize: true,
        proxy: { server: "http://proxy.example:8080", username: "u", password: "p w" },
        licenseKey: "cc_lic_docker_test_key_1234",
      }));
      try {
        const direct = await chromium.connectOverCDP(local.httpUrl);
        expect(browser.constructor).toBe(direct.constructor); // the same Playwright Browser type
        await direct.close();
        expect(browser.isConnected()).toBe(true);
        expect((browser as unknown as { dockerContainer: unknown }).dockerContainer).toEqual({
          id: "c0ffee1234", image: IMAGE, endpoint: `http://127.0.0.1:${docker.cdpPort}`,
        });
        const page = await browser.newPage();
        expect((page as unknown as { _clearcotePersona?: unknown })._clearcotePersona).toBeDefined(); // humanized, as locally
        await page.setContent("<title>in docker</title>");
        expect(await page.title()).toBe("in docker");
        expect(page.viewportSize()).toBeNull(); // no emulated viewport over the container's real window
      } finally {
        await browser.close();
      }
      expect(browser.isConnected()).toBe(false);
      expect(local.alive()).toBe(true); // close() disconnects; ending the browser is the container's stop
    } finally {
      await local.stop();
    }
    expect(docker.commands()).toEqual(["info", "ps", "create", "cp", "start", "port", "stop", "rm"]);
    const { argv, env } = docker.call("create");
    // --rm: a stopped container (and its anonymous engine volume) goes away by itself
    expect(argv.slice(0, 6)).toEqual(["docker", "create", "--rm", "--platform", "linux/amd64", "--shm-size"]);
    expect(argv[argv.indexOf("-p") + 1]).toBe("127.0.0.1::9222"); // loopback only: CDP is full control
    expect(argv[argv.length - 1]).toBe(IMAGE);
    expect(argv[argv.indexOf("-v") + 1]).toBe("clearcote-cache:/opt/xdg-cache"); // licensed engine downloads once
    // owned: the next launch on this machine removes it if this process is gone
    expect(after(argv, "--label")).toEqual(["com.clearcotelabs.sdk-launch=1", `com.clearcotelabs.owner-host=${hostname()}`, `com.clearcotelabs.owner-pid=${process.pid}`]);
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
    expect(docker.calls.filter((c) => ["stop", "rm"].includes(c.argv[1])).map((c) => c.argv.slice(2))).toEqual([
      ["--time", "10", "c0ffee1234"], ["-f", "-v", "c0ffee1234"], // -v: the image's anonymous VOLUME too
    ]);
  }, 60_000); // a real Chromium starts and is connected to: well past the 5 s default on a loaded machine

  it.skipIf(!CHROMIUM)("nothing asked for: the image's own defaults, no cache volume, no file; dockerImage picks the image", async () => {
    process.env.CLEARCOTE_DOCKER_IDLE_EXIT = "0"; // 0: never stops on its own
    const local = await new LocalChromium().start(CHROMIUM!);
    try {
      docker.cdpPort = Number(/:(\d+)$/.exec(local.httpUrl)![1]);
      const browser = await onMac(() => launch({ dockerImage: "example/clearcote:test" }));
      await browser.close();
    } finally {
      await local.stop();
    }
    expect(docker.call("create").argv.at(-1)).toBe("example/clearcote:test");
    expect(docker.call("create").env).toEqual({});
    expect(docker.call("create").argv).not.toContain("-v");
    expect(docker.commands()).not.toContain("cp");
  }, 60_000);

  it("sweeps stale containers of dead owners at the next launch", async () => {
    const here = hostname();
    docker.ps = `aaa111\t${here}\t${DEAD_PID}\n` // this machine, owner gone: swept
      + `bbb222\t${here}\t${process.pid}\n` // this machine, owner alive: kept
      + `ccc333\tsome-other-host\t${DEAD_PID}\n` // another machine on the same daemon: kept
      + "ddd444\t\t\n"; // no owner labels: kept
    docker.running = false; // this launch then fails; only the sweep matters here
    await expect(onMac(() => launch())).rejects.toThrow();
    expect(docker.call("ps").argv.slice(2, 5)).toEqual(["-a", "--filter", "label=com.clearcotelabs.sdk-launch=1"]);
    const swept = docker.calls.filter((c) => ["stop", "rm"].includes(c.argv[1])).map((c) => c.argv.slice(2)).slice(0, 2);
    expect(swept).toEqual([["--time", "10", "aaa111"], ["-f", "-v", "aaa111"]]);
    expect(docker.commands().indexOf("ps")).toBeLessThan(docker.commands().indexOf("create"));
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
    expect(docker.commands().slice(-2)).toEqual(["stop", "rm"]);
  });

  it("an image not published yet says what to do", async () => {
    docker.createError = `Unable to find image '${IMAGE}' locally\nError response from daemon: manifest for ${IMAGE} not found: manifest unknown: manifest unknown\n`;
    const err = await onMac(() => launch().then(() => null, (e) => e as Error));
    expect(err!.message).toContain(`the Clearcote image ${IMAGE} is not available`);
    expect(err!.message).toContain("published a few minutes after each SDK release");
    expect(err!.message).toContain("CLEARCOTE_DOCKER_IMAGE=teamflatearth/clearcote:latest");
    expect(docker.commands()).toEqual(["info", "ps", "create"]);
  });

  it("a failed create names the image", async () => {
    process.env.CLEARCOTE_DOCKER_IMAGE = "example/other:tag";
    docker.createError = "docker: Error response from daemon: Conflict. The container name is already in use.\n";
    await expect(onMac(() => launch())).rejects.toThrow(/`docker create example\/other:tag` failed: docker: Error response/);
  });

  it("a failed connect after the container started removes it", async () => {
    // Answers /json/version like a browser whose WebSocket is not there.
    const server = http.createServer((_req, res) => {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ Browser: "x", webSocketDebuggerUrl: "ws://127.0.0.1:1/devtools/browser/x" }));
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
    try {
      docker.cdpPort = (server.address() as net.AddressInfo).port;
      await expect(onMac(() => launch({ timeout: 5000 }))).rejects.toThrow();
    } finally {
      await new Promise((r) => server.close(r));
    }
    expect(docker.commands().slice(-2)).toEqual(["stop", "rm"]);
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
    expect(docker.commands().slice(-2)).toEqual(["stop", "rm"]);
  }, 60_000);
});
