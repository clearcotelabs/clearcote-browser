// The version catalog is asked once per ~10 minutes per machine, not once per launch: a burst of launches
// from one address was answered with a 403 challenge page by the site's host. The answer is kept per process
// and in ~/.clearcote/catalog.json; when the catalog can't be reached, the last kept answer (up to a week old)
// is used before the bundled snapshot. HOME is a temp dir and fetch is a stub.
// Mirrors sdk/python/tests/test_catalog_cache.py and sdk/dotnet/tests/Clearcote.Tests/CatalogCacheTests.cs.
import { describe, it, expect, afterEach, vi } from "vitest";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { CATALOG_FALLBACK, CATALOG_URL } from "../src/release.js";
import { tempDir } from "./helpers/temp.js";

const ENV_KEYS = ["HOME", "USERPROFILE"] as const;
const savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
const LIVE = { schema: 1, builds: [{ major: 999, version: "999.0.1.2", tier: "free", platforms: {} }] };

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const [k, v] of Object.entries(savedEnv)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

/** A new process, as far as download.ts's memory goes. `spread` is the fraction Math.random answers. */
async function newProcess(spread = 0) {
  vi.spyOn(Math, "random").mockReturnValue(spread);
  vi.resetModules();
  return import("../src/download.js");
}

/** A temp HOME and a stub for the site: counts the requests; `site.status` 403 answers like the challenge. */
async function setup(body: unknown = LIVE) {
  const home = tempDir("cc-catalog-");
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  const site = { hits: 0, status: 200, throws: false };
  vi.stubGlobal("fetch", async (url: string) => {
    expect(url).toBe(CATALOG_URL);
    site.hits++;
    if (site.throws) throw new Error("getaddrinfo ENOTFOUND");
    return new Response(site.status === 200 ? JSON.stringify(body) : "<html>challenge</html>", { status: site.status });
  });
  const file = path.join(home, ".clearcote", "catalog.json");
  const age = (sec: number) => {
    const e = JSON.parse(readFileSync(file, "utf8"));
    e.at -= sec;
    writeFileSync(file, JSON.stringify(e));
  };
  return { home, site, file, age, dl: await newProcess() };
}

describe("version catalog cache", () => {
  it("asks once for many launches, and keeps the answer in ~/.clearcote", async () => {
    const { site, file, dl } = await setup();
    for (let i = 0; i < 25; i++) expect(await dl.fetchCatalog(true)).toEqual(LIVE);
    expect(site.hits).toBe(1);
    const e = JSON.parse(readFileSync(file, "utf8"));
    expect(e.url).toBe(CATALOG_URL);
    expect(e.catalog).toEqual(LIVE);
    expect(Math.abs(e.at - Date.now() / 1000)).toBeLessThan(60);
  });

  it("another process uses the kept file", async () => {
    const { site, dl } = await setup();
    await dl.fetchCatalog(true);
    expect(await (await newProcess()).fetchCatalog(true)).toEqual(LIVE);
    expect(site.hits).toBe(1);
  });

  it("asks again once the answer is older than the TTL", async () => {
    const { site, age, dl } = await setup();
    await dl.fetchCatalog(true);
    age(601);
    await (await newProcess()).fetchCatalog(true);
    expect(site.hits).toBe(2);
  });

  it("a process's own spread keeps the answer a little longer", async () => {
    const { site, age, dl } = await setup();
    await dl.fetchCatalog(true);
    age(601);
    await (await newProcess(0.9)).fetchCatalog(true); // 600 + 270 s
    expect(site.hits).toBe(1);
  });

  it("a 403 uses the last kept answer, not the bundled snapshot", async () => {
    const { site, age, dl } = await setup();
    await dl.fetchCatalog(true);
    age(3600);
    site.status = 403;
    const err = vi.spyOn(process.stderr, "write").mockReturnValue(true);
    expect(await (await newProcess()).fetchCatalog(false)).toEqual(LIVE);
    expect(site.hits).toBe(2);
    const said = err.mock.calls.map((c) => String(c[0])).join("");
    expect(said).toContain("HTTP 403");
    expect(said).toContain("last answer kept on this machine");
  });

  it("a failure with nothing kept uses the bundled snapshot and keeps nothing", async () => {
    const { site, file, dl } = await setup();
    site.status = 403;
    expect(await dl.fetchCatalog(true)).toBe((await import("../src/release.js")).CATALOG_FALLBACK);
    site.throws = true;
    expect((await dl.fetchCatalog(true)).builds.length).toBe(CATALOG_FALLBACK.builds.length);
    expect(existsSync(file)).toBe(false);
  });

  it("an answer older than a week is not used on failure", async () => {
    const { site, age, dl } = await setup();
    await dl.fetchCatalog(true);
    age(7 * 86400 + 1);
    site.status = 403;
    expect((await (await newProcess()).fetchCatalog(true)).builds).not.toEqual(LIVE.builds);
  });

  it.each(["", "{", "[]", '{"at":"x"}', '{"at":1,"url":"u","catalog":{}}'])("an unreadable file (%j) is a miss", async (content) => {
    const { site, file, dl } = await setup();
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, content);
    expect(await dl.fetchCatalog(true)).toEqual(LIVE);
    expect(site.hits).toBe(1);
  });

  it("a file for another url, or with a time ahead of this clock, is a miss", async () => {
    const { site, file, dl } = await setup();
    const other = { schema: 1, builds: [{ major: 1, version: "1.0.0.0", platforms: {} }] };
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, JSON.stringify({ at: Date.now() / 1000, url: "https://example.test/versions", catalog: other }));
    expect(await dl.fetchCatalog(true)).toEqual(LIVE);
    writeFileSync(file, JSON.stringify({ at: Date.now() / 1000 + 3600, url: CATALOG_URL, catalog: other }));
    expect(await (await newProcess()).fetchCatalog(true)).toEqual(LIVE);
    expect(site.hits).toBe(2);
  });

  it("an answer without builds is not kept", async () => {
    const { file, dl } = await setup({ schema: 1, builds: [] });
    expect((await dl.fetchCatalog(true)).builds.length).toBe(CATALOG_FALLBACK.builds.length);
    expect(existsSync(file)).toBe(false);
  });
});
