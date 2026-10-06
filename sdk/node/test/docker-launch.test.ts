// launch() on macOS runs the Clearcote Docker image (there is no native macOS build).
//
// The host platform is mocked (process.platform, only around the launch call) and the docker CLI is
// replaced by a recorder that answers like Docker would; the "container's" CDP endpoint is a real local
// Chromium, so the SDK's connect, close and the Playwright objects it returns are all real. The real
// image is exercised end to end separately (CLEARCOTE_TEST_ONLY_ASSUME_MACOS, docker-launch.live.test.ts).
// Mirrors sdk/python/tests/test_docker_launch.py.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright-core";
import { DockerUnavailableError, launch } from "../src/index.js";
import { dockerCli, dockerRequested, TEST_ONLY_ASSUME_MACOS, type CliResult } from "../src/docker.js";
import { LocalChromium, findChromium } from "./helpers/chromium.js";
import { tempDir } from "./helpers/temp.js";

const SDK_VERSION = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version as string;
const ENV = ["HOME", "USERPROFILE", "CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_DOCKER_IMAGE", "CLEARCOTE_CLOUD", "CLEARCOTE_LICENSE_KEY", TEST_ONLY_ASSUME_MACOS];
const CHROMIUM = findChromium();
const saved: Record<string, string | undefined> = {};

/** Stands in for the docker CLI: records every call (argv + the environment it was given). */
class FakeDocker {
  calls: Array<{ argv: string[]; env: Record<string, string> }> = [];
  cdpPort = 1;
  running = true;
  infoError = "";
  runError = "";
  logs = "";
  run = async (argv: string[], env?: Record<string, string>): Promise<CliResult> => {
    this.calls.push({ argv: [...argv], env: { ...(env ?? {}) } });
    switch (argv[1]) {
      case "info": return this.infoError ? { code: 1, stdout: "", stderr: this.infoError } : { code: 0, stdout: "29.1.3\n", stderr: "" };
      case "run": return this.runError ? { code: 125, stdout: "", stderr: this.runError } : { code: 0, stdout: "c0ffee1234\n", stderr: "" };
      case "port": return { code: 0, stdout: `127.0.0.1:${this.cdpPort}\n[::1]:${this.cdpPort}\n`, stderr: "" };
      case "inspect": return { code: 0, stdout: `${this.running}\n`, stderr: "" };
      case "logs": return { code: 0, stdout: this.logs, stderr: "" };
      default: return { code: 0, stdout: "", stderr: "" }; // stop, rm
    }
  };
  commands = () => this.calls.map((c) => c.argv[1]);
  runCall = () => this.calls.find((c) => c.argv[1] === "run")!;
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
  vi.spyOn(process.stderr, "write").mockImplementation((() => true) as never);
});
afterEach(() => {
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
          id: "c0ffee1234", image: `teamflatearth/clearcote:sdk-${SDK_VERSION}`, endpoint: `http://127.0.0.1:${docker.cdpPort}`,
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
    expect(docker.commands()).toEqual(["info", "run", "port", "stop", "rm"]);
    const { argv, env } = docker.runCall();
    expect(argv.slice(0, 5)).toEqual(["docker", "run", "-d", "--platform", "linux/amd64"]);
    expect(argv[argv.indexOf("-p") + 1]).toBe("127.0.0.1::9222"); // loopback only: CDP is full control
    expect(argv[argv.length - 1]).toBe(`teamflatearth/clearcote:sdk-${SDK_VERSION}`);
    expect(argv[argv.indexOf("-v") + 1]).toBe("clearcote-cache:/opt/xdg-cache"); // licensed engine downloads once
    const eNames = argv.filter((_, i) => i > 0 && argv[i - 1] === "-e");
    expect(eNames.every((a) => !a.includes("="))).toBe(true); // values travel in the environment only
    expect(argv.some((a) => a.includes("cc_lic_docker_test_key_1234") || a.includes("p w") || a.includes("p%20w"))).toBe(false);
    expect(env).toEqual({
      CC_FINGERPRINT: "seed-1", CC_PLATFORM: "windows", CC_TIMEZONE: "Europe/Berlin",
      CC_HARDWARE_CONCURRENCY: "8", CC_CANVAS_NOISE: "0", CC_HEADLESS: "1",
      CC_EXTRA_ARGS: "--lang=de-DE", CC_PROXY: "http://u:p%20w@proxy.example:8080",
      CLEARCOTE_LICENSE_KEY: "cc_lic_docker_test_key_1234",
    });
    expect(eNames.sort()).toEqual(Object.keys(env).sort());
    expect(docker.calls.filter((c) => ["stop", "rm"].includes(c.argv[1])).map((c) => c.argv.slice(2))).toEqual([
      ["--time", "10", "c0ffee1234"], ["-f", "-v", "c0ffee1234"], // -v: the image's anonymous VOLUME too
    ]);
  }, 60_000); // a real Chromium starts and is connected to: well past the 5 s default on a loaded machine

  it.skipIf(!CHROMIUM)("nothing asked for: the image's own defaults, no cache volume; dockerImage picks the image", async () => {
    const local = await new LocalChromium().start(CHROMIUM!);
    try {
      docker.cdpPort = Number(/:(\d+)$/.exec(local.httpUrl)![1]);
      const browser = await onMac(() => launch({ dockerImage: "example/clearcote:test" }));
      await browser.close();
    } finally {
      await local.stop();
    }
    expect(docker.runCall().argv.at(-1)).toBe("example/clearcote:test");
    expect(docker.runCall().env).toEqual({});
    expect(docker.runCall().argv).not.toContain("-v");
  }, 60_000);

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

  it("a failed docker run names the image", async () => {
    process.env.CLEARCOTE_DOCKER_IMAGE = "example/missing:tag";
    docker.runError = "Unable to find image 'example/missing:tag' locally\nmanifest unknown\n";
    await expect(onMac(() => launch())).rejects.toThrow(/docker run example\/missing:tag` failed: Unable to find image/);
    expect(docker.commands()).toEqual(["info", "run"]);
  });
});
