// A burst of launches from one machine used to send one request per launch to the site (www), whose host
// answers a burst from one address with a 403 challenge page and keeps challenging that address for minutes.
// With a PRO licence the challenge on GET /api/v1/download/pro failed the launch ("PRO download not
// authorized (HTTP 403)"), so a customer could not start more than ~20 browsers on one server (2026-11-24).
//
// Now: one request per machine at a time (the others wait for the answer it keeps), and a challenge, a 5xx or
// no connection launches the installed build the last answer named. A licence verdict (the route's own JSON
// 401/403) still fails the launch. The lock is a file, so async callers in one process (Promise.all here)
// take it the way processes do. HOME is a temp dir; the catalog is a fetch stub, the PRO route the local fake.
// Mirrors sdk/python/tests/test_site_burst.py.
import { describe, it, expect, afterEach, vi } from "vitest";
import { existsSync, mkdirSync, readFileSync, readdirSync, utimesSync, writeFileSync } from "node:fs";
import path from "node:path";
import { CATALOG_URL } from "../src/release.js";
import { startFakeBuild, type FakeBuild } from "./helpers/fake-build.js";
import { tempDir } from "./helpers/temp.js";

const proPlatform = process.platform === "win32" || process.platform === "linux"; // the PRO route serves these
const ENV_KEYS = ["HOME", "USERPROFILE", "LOCALAPPDATA", "CLEARCOTE_LICENSE_API", "CLEARCOTE_RELEASE_CHANNEL"] as const;
const savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
const LIVE = { schema: 1, builds: [{ major: 999, version: "999.0.1.2", tier: "free", platforms: {} }] };
const fakes: FakeBuild[] = [];

afterEach(async () => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const f of fakes.splice(0)) await f.close();
  for (const [k, v] of Object.entries(savedEnv)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

/** A new process, as far as download.ts's memory goes, with no TTL spread of its own. */
async function newProcess() {
  vi.spyOn(Math, "random").mockReturnValue(0);
  vi.resetModules();
  const dl = await import("../src/download.js");
  return { dl, release: await import("../src/release.js") }; // the same module instance download.ts uses
}

async function setup() {
  const home = tempDir("cc-burst-");
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.LOCALAPPDATA = home;
  delete process.env.CLEARCOTE_LICENSE_API;
  delete process.env.CLEARCOTE_RELEASE_CHANNEL;
  const dir = path.join(home, ".clearcote");
  return { home, dir, ...(await newProcess()) };
}

/** A stub for the catalog: each request takes `delayMs` (the holder is still asking while the others arrive). */
function site(opts: { status?: number; delayMs?: number } = {}) {
  const s = { hits: 0 };
  vi.stubGlobal("fetch", async (url: string) => {
    expect(url).toBe(CATALOG_URL);
    s.hits++;
    await new Promise((r) => setTimeout(r, opts.delayMs ?? 0));
    const status = opts.status ?? 200;
    return new Response(status === 200 ? JSON.stringify(LIVE) : "<html>challenge</html>", { status });
  });
  return s;
}

/** Run fn n times at once: results, or the error each threw. */
function together<T>(n: number, fn: () => Promise<T>): Promise<Array<T | Error>> {
  return Promise.all(Array.from({ length: n }, () => fn().catch((e: Error) => e)));
}

function age(file: string, sec: number) {
  const t = new Date(Date.now() - sec * 1000);
  utimesSync(file, t, t);
}

// ── catalog ──────────────────────────────────────────────────────────────────────────────────────────────

describe("one catalog request per machine", () => {
  it("a burst with nothing kept asks the catalog once", async () => {
    const { dir, dl } = await setup();
    const s = site({ delayMs: 300 });
    expect(await together(20, () => dl.fetchCatalog(true))).toEqual(Array(20).fill(LIVE));
    expect(s.hits).toBe(1);
    expect(existsSync(path.join(dir, "catalog.lock"))).toBe(false);
  });

  it("a failed catalog request is not repeated by the others", async () => {
    const { dl, release } = await setup();
    const s = site({ status: 403, delayMs: 300 });
    for (const r of await together(20, () => dl.fetchCatalog(true))) expect(r).toBe(release.CATALOG_FALLBACK);
    expect(s.hits).toBe(1);
  });

  it("a failure older than the quiet time is asked again", async () => {
    const { dir, dl } = await setup();
    const s = site();
    mkdirSync(dir, { recursive: true });
    const failed = path.join(dir, "catalog.failed");
    writeFileSync(failed, "");
    age(failed, dl.flightTiming.failedQuietSec + 1);
    expect(await dl.fetchCatalog(true)).toEqual(LIVE);
    expect(s.hits).toBe(1);
    expect(existsSync(failed)).toBe(false);
  });

  it("a lock left by a dead process is taken over", async () => {
    const { dir, dl } = await setup();
    const s = site();
    mkdirSync(dir, { recursive: true });
    const lock = path.join(dir, "catalog.lock");
    writeFileSync(lock, "12345");
    age(lock, dl.flightTiming.staleSec + 1);
    const t0 = performance.now();
    expect(await dl.fetchCatalog(true)).toEqual(LIVE);
    expect(s.hits).toBe(1);
    expect(performance.now() - t0).toBeLessThan(5000);
  });

  it("a stuck holder is waited for at most the flight wait", async () => {
    const { dir, dl } = await setup();
    const s = site();
    dl.flightTiming.waitSec = 0.5;
    mkdirSync(dir, { recursive: true });
    writeFileSync(path.join(dir, "catalog.lock"), "12345"); // live: just made
    expect(await dl.fetchCatalog(true)).toEqual(LIVE);
    expect(s.hits).toBe(1);
  });
});

// ── PRO build lookup ─────────────────────────────────────────────────────────────────────────────────────

async function proSetup() {
  const env = await setup();
  const srv = await startFakeBuild();
  fakes.push(srv);
  const stderr: string[] = [];
  vi.spyOn(process.stderr, "write").mockImplementation((s: string | Uint8Array) => {
    stderr.push(String(s));
    return true;
  });
  const cache = tempDir("cc-burst-cache-");
  const ensure = (dl: typeof env.dl) => dl.proEnsureBinary("cc_lic_burst", { apiBase: srv.url, cacheDir: cache, quiet: true });
  return { ...env, srv, stderr, ensure };
}

/** Make every kept PRO answer `sec` older. */
function expireKept(dir: string, sec: number) {
  for (const name of readdirSync(dir).filter((n) => /^pro-meta-[0-9a-f]{16}\.json$/.test(n))) {
    const file = path.join(dir, name);
    const e = JSON.parse(readFileSync(file, "utf8"));
    e.at -= sec;
    writeFileSync(file, JSON.stringify(e));
  }
}

describe.runIf(proPlatform)("one PRO build lookup per machine", () => {
  it("a burst of PRO launches asks once", async () => {
    const { dir, srv, ensure, dl } = await proSetup();
    const first = await ensure(dl); // installs the build
    expireKept(dir, 3600);
    const other = (await newProcess()).dl; // only the file is left
    expect(await together(20, () => ensure(other))).toEqual(Array(20).fill(first));
    expect(srv.metaHits).toBe(2); // the install, then one for the whole burst
    expect(readdirSync(dir).filter((n) => n.endsWith(".lock"))).toEqual([]);
  }, 60_000);

  it("a challenge launches the installed build the last answer named", async () => {
    const { dir, srv, ensure, dl, stderr } = await proSetup();
    const first = await ensure(dl);
    expireKept(dir, 3600);
    const other = (await newProcess()).dl;
    srv.metaChallenge = true;
    expect(await together(10, () => ensure(other))).toEqual(Array(10).fill(first));
    expect(srv.metaHits).toBe(2); // the others did not ask after the challenge
    const said = stderr.join("");
    expect(said.split("PRO build lookup").length - 1).toBe(1);
    expect(said).toContain("challenge page");
    expect(said).toContain(`the PRO build lookup at ${srv.url} failed (HTTP 403: a challenge page from the site's host)`);
  }, 60_000);

  it("a challenge with no installed build fails with a clear message", async () => {
    const { srv, ensure, dl } = await proSetup();
    srv.metaChallenge = true;
    await expect(ensure(dl)).rejects.toThrow(/build lookup .* failed .*challenge page.*Try again/);
  });

  it("an answer older than a week is not used", async () => {
    const { dir, srv, ensure, dl } = await proSetup();
    await ensure(dl);
    expireKept(dir, 7 * 86400 + 1);
    const other = (await newProcess()).dl;
    srv.metaChallenge = true;
    await expect(ensure(other)).rejects.toThrow(/no earlier answer names an installed build/);
  });

  it.each([401, 403])("a licence verdict (%i) still fails the launch", async (status) => {
    const { dir, srv, ensure, dl } = await proSetup();
    await ensure(dl);
    expireKept(dir, 3600);
    const other = (await newProcess()).dl;
    srv.metaStatus = status;
    const got = await together(5, () => ensure(other));
    for (const r of got) {
      expect(r).toBeInstanceOf(Error);
      expect(String((r as Error).message)).toContain("not authorized");
    }
    expect(readdirSync(dir).filter((n) => n.endsWith(".failed"))).toEqual([]); // a verdict is not a site failure
  }, 60_000);

  it("a server error launches the installed build", async () => {
    const { dir, srv, ensure, dl } = await proSetup();
    const first = await ensure(dl);
    expireKept(dir, 3600);
    const other = (await newProcess()).dl;
    srv.metaStatus = 503;
    expect(await ensure(other)).toBe(first);
  });

  it("no connection launches the installed build", async () => {
    const { dir, srv, ensure, dl } = await proSetup();
    const first = await ensure(dl);
    expireKept(dir, 3600);
    const other = (await newProcess()).dl;
    await srv.close(); // nothing listens on the route's port any more
    expect(await ensure(other)).toBe(first);
  });
});
