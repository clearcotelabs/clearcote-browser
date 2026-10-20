// The PRO download route's answer is kept for 10 minutes (SDK 0.55), per process and in
// ~/.clearcote/pro-meta-<hash>.json, so a launch of an installed PRO build makes no request for it. Only an
// installed build that passes the cached-build check (re-hashing its protected files with the kept hashes)
// is launched from a kept answer; anything else asks again, and a download always follows a fresh answer.
// A local server stands in for the route and the archive; HOME and the browser cache are temp dirs.
import { describe, it, expect, afterEach, vi } from "vitest";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { BINARY, TAG, fakeArchive, verifiedTree } from "./helpers/fake-build.js";
import { tempDir } from "./helpers/temp.js";

const proPlatform = process.platform === "win32" || process.platform === "linux"; // the PRO route serves these
const plat = process.platform === "win32" ? "windows" : "linux";
const ENV_KEYS = ["HOME", "USERPROFILE", "LOCALAPPDATA", "CLEARCOTE_LICENSE_API", "CLEARCOTE_RELEASE_CHANNEL"] as const;
const savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
const servers: Server[] = [];

afterEach(async () => {
  vi.useRealTimers();
  for (const s of servers.splice(0)) {
    s.closeAllConnections();
    await new Promise((r) => s.close(r));
  }
  for (const [k, v] of Object.entries(savedEnv)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

/** A temp HOME and browser cache, and a fresh copy of download.ts (a new process, as far as its memory goes). */
async function setup() {
  const home = tempDir("cc-prometa-");
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.LOCALAPPDATA = home;
  delete process.env.CLEARCOTE_LICENSE_API;
  delete process.env.CLEARCOTE_RELEASE_CHANNEL;
  return { home, cache: tempDir("cc-prometa-cache-"), dl: await freshModule() };
}

async function freshModule() {
  vi.resetModules();
  return import("../src/download.js");
}

/** The download route and the archives it names. `meta` and `status` can change between calls. */
async function route() {
  const archive = fakeArchive();
  const codeSha = createHash("sha256").update(Buffer.alloc(64, "x")).digest("hex");
  const state = {
    status: 200,
    archivePath: "/a.zip",
    /** Answers with no download in it (the route's "nothing published"). */
    empty: false,
    metaHits: [] as string[],
    archiveHits: [] as string[],
  };
  let base = "";
  const meta = () => ({
    tag: TAG, version: "0.0.0", url: `${base}${state.archivePath}`, sha256: createHash("sha256").update(archive).digest("hex"),
    exe_sha256: codeSha, chrome_dll_sha256: codeSha, asset: "fake.zip", archive: "zip", binary: BINARY, size: archive.length,
  });
  const srv = createServer((req, res) => {
    const url = req.url ?? "";
    if (url.startsWith("/api/v1/download/pro")) {
      state.metaHits.push(url);
      res.writeHead(state.status, { "content-type": "application/json" });
      res.end(state.status === 200 ? JSON.stringify(state.empty ? { version: "0.0.0" } : meta()) : '{"error":"Invalid license key."}');
      return;
    }
    if (url.endsWith(".zip")) {
      state.archiveHits.push(url);
      res.writeHead(200, { "content-type": "application/zip", "content-length": String(archive.length) });
      res.end(archive);
      return;
    }
    res.writeHead(404).end();
  });
  servers.push(srv);
  await new Promise<void>((r) => srv.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${(srv.address() as AddressInfo).port}`;
  return { base, state };
}

const metaFile = (home: string, key: string, url: string) =>
  path.join(home, ".clearcote", `pro-meta-${createHash("sha256").update(`${key}\n${url}`).digest("hex").slice(0, 16)}.json`);

describe.runIf(proPlatform)("PRO download metadata cache", () => {
  it("a second launch within 10 minutes makes no request to the download route", async () => {
    const { home, cache, dl } = await setup();
    const r = await route();
    const exe = verifiedTree(path.join(cache, TAG));
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };
    expect(await dl.proEnsureBinary("cc_lic_a", opts)).toBe(exe);
    expect(await dl.proEnsureBinary("cc_lic_a", opts)).toBe(exe);
    expect(r.state.metaHits).toHaveLength(1);
    const kept = JSON.parse(readFileSync(metaFile(home, "cc_lic_a", dl.proDownloadUrl(r.base, plat)), "utf8"));
    expect(kept.meta.tag).toBe(TAG);
    expect(Math.abs(kept.at - Date.now() / 1000)).toBeLessThan(60); // epoch seconds
  });

  it("another process reuses the answer kept on disk", async () => {
    const { cache, dl } = await setup();
    const r = await route();
    const exe = verifiedTree(path.join(cache, TAG));
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };
    await dl.proEnsureBinary("cc_lic_b", opts);
    const other = await freshModule();
    expect(await other.proEnsureBinary("cc_lic_b", opts)).toBe(exe);
    expect(r.state.metaHits).toHaveLength(1);
  });

  it("a build that is not installed, or fails the cached-build check, gets a fresh answer, and its download follows that answer", async () => {
    const { cache, dl } = await setup();
    const r = await route();
    const base = path.join(cache, TAG);
    const exe = verifiedTree(base);
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };
    await dl.proEnsureBinary("cc_lic_c", opts);

    rmSync(base, { recursive: true, force: true }); // the build is gone
    r.state.archivePath = "/b.zip"; // and the route now signs another url
    const got = await dl.proEnsureBinary("cc_lic_c", opts);
    expect(r.state.metaHits).toHaveLength(2);
    expect(r.state.archiveHits).toEqual(["/b.zip"]); // never the kept answer's url

    writeFileSync(got, Buffer.alloc(64, "y")); // same size, other bytes: the launch-time hash check fails
    r.state.archivePath = "/c.zip";
    expect(await dl.proEnsureBinary("cc_lic_c", opts)).toBe(exe);
    expect(r.state.metaHits).toHaveLength(3);
    expect(r.state.archiveHits).toEqual(["/b.zip", "/c.zip"]);
    expect(readFileSync(exe, "utf8")).toBe("x".repeat(64)); // repaired
  }, 30_000);

  it("an answer older than 10 minutes is asked for again", async () => {
    const { home, cache, dl } = await setup();
    const r = await route();
    verifiedTree(path.join(cache, TAG));
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };
    const t0 = Date.now();
    await dl.proEnsureBinary("cc_lic_d", opts);
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(t0 + 590_000);
    await dl.proEnsureBinary("cc_lic_d", opts);
    expect(r.state.metaHits).toHaveLength(1);
    vi.setSystemTime(t0 + 601_000);
    await dl.proEnsureBinary("cc_lic_d", opts);
    expect(r.state.metaHits).toHaveLength(2);
    vi.useRealTimers();

    // on disk too: an entry written 601 s ago is a miss for another process
    const file = metaFile(home, "cc_lic_d", dl.proDownloadUrl(r.base, plat));
    const kept = JSON.parse(readFileSync(file, "utf8"));
    writeFileSync(file, JSON.stringify({ ...kept, at: Date.now() / 1000 - 601 }));
    await (await freshModule()).proEnsureBinary("cc_lic_d", opts);
    expect(r.state.metaHits).toHaveLength(3);
  });

  it("an error answer is never kept", async () => {
    const { home, cache, dl } = await setup();
    const r = await route();
    verifiedTree(path.join(cache, TAG));
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };
    r.state.status = 401;
    await expect(dl.proEnsureBinary("cc_lic_e", opts)).rejects.toThrow(/not authorized \(HTTP 401\)/);
    r.state.status = 200;
    r.state.empty = true;
    await expect(dl.proEnsureBinary("cc_lic_e", opts)).rejects.toThrow(/not currently available/);
    expect(existsSync(metaFile(home, "cc_lic_e", dl.proDownloadUrl(r.base, plat)))).toBe(false);
    r.state.empty = false;
    await dl.proEnsureBinary("cc_lic_e", opts);
    await dl.proEnsureBinary("cc_lic_e", opts);
    expect(r.state.metaHits).toHaveLength(3); // two errors asked again; then one answer, kept
  });

  it("licences, version pins and channels are separate entries", async () => {
    const { cache, dl } = await setup();
    const r = await route();
    verifiedTree(path.join(cache, TAG));
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };
    await dl.proEnsureBinary("cc_lic_f", { ...opts, version: "1" });
    await dl.proEnsureBinary("cc_lic_f", { ...opts, version: "2" });
    await dl.proEnsureBinary("cc_lic_f", { ...opts, version: "1" }); // kept
    await dl.proEnsureBinary("cc_lic_f", { ...opts, version: "1", releaseChannel: "preview" });
    await dl.proEnsureBinary("cc_lic_g", { ...opts, version: "1" });
    expect(r.state.metaHits).toEqual([
      `/api/v1/download/pro?platform=${plat}&version=1`,
      `/api/v1/download/pro?platform=${plat}&version=2`,
      `/api/v1/download/pro?platform=${plat}&version=1&channel=preview`,
      `/api/v1/download/pro?platform=${plat}&version=1`,
    ]);
  });

  it("a corrupt cache file is a miss, and is replaced", async () => {
    const { home, cache, dl } = await setup();
    const r = await route();
    const exe = verifiedTree(path.join(cache, TAG));
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };
    const file = metaFile(home, "cc_lic_h", dl.proDownloadUrl(r.base, plat));
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, "{not json");
    expect(await dl.proEnsureBinary("cc_lic_h", opts)).toBe(exe);
    expect(r.state.metaHits).toHaveLength(1);
    expect(JSON.parse(readFileSync(file, "utf8")).meta.tag).toBe(TAG);
    writeFileSync(file, JSON.stringify({ at: Date.now() / 1000, meta: "nope" }));
    await (await freshModule()).proEnsureBinary("cc_lic_h", opts);
    expect(r.state.metaHits).toHaveLength(2);
  });

  it("an entry dated ahead of this clock is a miss", async () => {
    const { home, cache, dl } = await setup();
    const r = await route();
    verifiedTree(path.join(cache, TAG));
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };
    await dl.proEnsureBinary("cc_lic_i", opts);
    const file = metaFile(home, "cc_lic_i", dl.proDownloadUrl(r.base, plat));
    const kept = JSON.parse(readFileSync(file, "utf8"));
    writeFileSync(file, JSON.stringify({ ...kept, at: Date.now() })); // milliseconds read as seconds: far ahead
    await (await freshModule()).proEnsureBinary("cc_lic_i", opts);
    expect(r.state.metaHits).toHaveLength(2);
  });
});
