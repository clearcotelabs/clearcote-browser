// launch()'s macOS path against the REAL Clearcote image: docker run, CDP, a page that loads, the persona
// options reaching the engine, and no container left after close().
//
// Off by default. Set CLEARCOTE_TEST_DOCKER_IMAGE to the image to run (and have a working docker); the test
// sets CLEARCOTE_TEST_ONLY_ASSUME_MACOS=1 so the macOS decision itself is what sends launch() to Docker.
// Mirrors sdk/python/tests/test_docker_launch_live.py and DockerLaunchLiveTests.cs.
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { execFileSync } from "node:child_process";
import type { Browser } from "playwright-core";
import { launch } from "../src/index.js";
import { dockerCli, TEST_ONLY_ASSUME_MACOS } from "../src/docker.js";
import { tempDir } from "./helpers/temp.js";

const IMAGE = process.env.CLEARCOTE_TEST_DOCKER_IMAGE;
const ENV = ["HOME", "USERPROFILE", "CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_CLOUD", "CLEARCOTE_DOCKER_IMAGE", TEST_ONLY_ASSUME_MACOS];
const saved: Record<string, string | undefined> = {};

const containerExists = (id: string) =>
  execFileSync("docker", ["ps", "-a", "-q", "--no-trunc", "--filter", `id=${id}`], { encoding: "utf8" }).trim().length > 0;
// The image declares VOLUME /opt/xdg-cache: every container gets an anonymous volume holding a copy of the
// engine (~0.5 GB). It must go when the container does.
const anonymousVolumes = (id: string) =>
  execFileSync("docker", ["inspect", "-f", '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}', id], { encoding: "utf8" })
    .trim().split(" ").filter(Boolean);
const volumeExists = (name: string) => {
  try { execFileSync("docker", ["volume", "inspect", name], { stdio: "ignore" }); return true; } catch { return false; }
};
const containerOf = (b: Browser) => (b as unknown as { dockerContainer: { id: string } }).dockerContainer;

async function probe(browser: Browser) {
  const page = await browser.newPage();
  await page.goto("data:text/html,<title>loaded in docker</title><p>hi</p>");
  const got = await page.evaluate(() => ({
    title: document.title, tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    platform: navigator.platform, ua: navigator.userAgent, w: innerWidth,
  }));
  return { page, got };
}

describe.skipIf(!IMAGE || !dockerCli.which())("launch() on macOS against the real Clearcote image", () => {
  beforeEach(() => {
    for (const k of ENV) saved[k] = process.env[k];
    for (const k of ENV) delete process.env[k];
    process.env[TEST_ONLY_ASSUME_MACOS] = "1";
    process.env.CLEARCOTE_DOCKER_IMAGE = IMAGE;
    const home = tempDir("cc-docker-live-home-"); // no saved licence key: the image's open engine
    process.env.HOME = home;
    process.env.USERPROFILE = home;
  });
  afterEach(() => {
    for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
  });

  it("runs the image, loads a page, carries the persona, and leaves no container", async () => {
    // control: the image's own defaults
    const plain = await launch({ quiet: true });
    const id0 = containerOf(plain).id;
    let base;
    let vols0: string[] = [];
    try {
      expect(containerExists(id0)).toBe(true);
      vols0 = anonymousVolumes(id0);
      expect(vols0.length).toBeGreaterThan(0);
      expect(vols0.every(volumeExists)).toBe(true);
      base = (await probe(plain)).got;
      expect(base.title).toBe("loaded in docker");
    } finally {
      await plain.close();
    }
    expect(containerExists(id0)).toBe(false);
    expect(vols0.some(volumeExists)).toBe(false);

    // treatment: the persona options reach the engine in the container
    const b = await launch({ fingerprint: "docker-e2e-seed", platform: "windows", timezone: "Asia/Tokyo", quiet: true });
    const id1 = containerOf(b).id;
    const vols1 = anonymousVolumes(id1);
    try {
      const { page, got } = await probe(b);
      expect(got.title).toBe("loaded in docker");
      expect(got.tz).toBe("Asia/Tokyo");
      expect(got.tz).not.toBe(base.tz);
      expect(got.platform).toBe("Win32");
      expect(base.platform).not.toBe("Win32");
      expect(got.ua).toContain("Windows NT");
      expect(page.viewportSize()).toBeNull();
      await page.goto("https://example.com/", { waitUntil: "domcontentloaded", timeout: 60_000 });
      expect(await page.title()).toBe("Example Domain");
    } finally {
      await b.close();
    }
    expect(containerExists(id1)).toBe(false);
    expect(vols1.length).toBeGreaterThan(0);
    expect(vols1.some(volumeExists)).toBe(false);
  }, 600_000);

  // Licensed and proxied, against whatever image CLEARCOTE_TEST_DOCKER_IMAGE names (one from before sdk-0.40.0 too):
  // launch() must come back on the licensed engine with the proxy applied -- or refuse -- never on the open engine
  // with traffic going direct. Needs CLEARCOTE_TEST_DOCKER_KEY, CLEARCOTE_TEST_DOCKER_PROXY (a proxy the container
  // can reach) and CLEARCOTE_TEST_DOCKER_CACHE_VOLUME (a scratch volume for the licensed engine).
  const KEY = process.env.CLEARCOTE_TEST_DOCKER_KEY;
  const PROXY = process.env.CLEARCOTE_TEST_DOCKER_PROXY;
  it.skipIf(!KEY || !PROXY)("a licensed, proxied launch is never downgraded", async () => {
    if (saved.CLEARCOTE_DOCKER_CACHE_VOLUME === undefined && process.env.CLEARCOTE_TEST_DOCKER_CACHE_VOLUME) {
      process.env.CLEARCOTE_DOCKER_CACHE_VOLUME = process.env.CLEARCOTE_TEST_DOCKER_CACHE_VOLUME;
    }
    const b = await launch({ licenseKey: KEY, proxy: { server: PROXY! }, quiet: true, timeout: 600_000 });
    const info = (b as unknown as { dockerContainer: { id: string; serveProtocol: number } }).dockerContainer;
    try {
      const env = execFileSync("docker", ["inspect", "-f", "{{json .Config.Env}}", info.id], { encoding: "utf8" });
      // protocol 2 keeps the key out of the container's configuration; an older image gets it as a variable
      expect(env.includes(KEY!)).toBe(info.serveProtocol < 2);
      const page = await b.newPage();
      await page.goto("https://example.com/", { waitUntil: "domcontentloaded", timeout: 60_000 });
      expect(await page.title()).toBe("Example Domain");
    } finally {
      await b.close();
      delete process.env.CLEARCOTE_DOCKER_CACHE_VOLUME;
    }
    expect(containerExists(info.id)).toBe(false);
  }, 900_000);
});
