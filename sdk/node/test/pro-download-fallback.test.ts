// A new PRO release must not break launches that cannot download it (2026-10-24: one licence on SDK
// 0.38.1 relaunched 4,048 times into the per-licence download limit after r50 shipped, with r48 on disk).
// A failed download of a newer build launches the newest installed build of the same major, a 429 is
// remembered until its Retry-After, and an exact revision pin never gets another build.
// A local server stands in for the download route and the archive; HOME and the browser cache are temp dirs.
import { describe, it, expect, afterEach, vi } from "vitest";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { BINARY, fakeArchive, verifiedTree } from "./helpers/fake-build.js";
import { tempDir } from "./helpers/temp.js";

const proPlatform = process.platform === "win32" || process.platform === "linux"; // the PRO route serves these
const OLD = "pro-155.0.8059.39-r48";
const NEW = "pro-155.0.8059.39-r50";
const OTHER_MAJOR = "pro-154.0.8037.57-r41";
const ENV_KEYS = ["HOME", "USERPROFILE", "LOCALAPPDATA", "CLEARCOTE_LICENSE_API", "CLEARCOTE_RELEASE_CHANNEL"] as const;
const savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
const servers: Server[] = [];

afterEach(async () => {
  vi.restoreAllMocks();
  for (const s of servers.splice(0)) {
    s.closeAllConnections();
    await new Promise((r) => s.close(r));
  }
  for (const [k, v] of Object.entries(savedEnv)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

async function setup() {
  const home = tempDir("cc-dlfb-");
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.LOCALAPPDATA = home;
  delete process.env.CLEARCOTE_LICENSE_API;
  delete process.env.CLEARCOTE_RELEASE_CHANNEL;
  vi.resetModules();
  const stderr: string[] = [];
  vi.spyOn(process.stderr, "write").mockImplementation((s: string | Uint8Array) => {
    stderr.push(String(s));
    return true;
  });
  return { cache: tempDir("cc-dlfb-cache-"), dl: await import("../src/download.js"), stderr };
}

/** The route answers NEW; the archive answers `archiveStatus` (with `retryAfter`, when set) or the build. */
async function route() {
  const archive = fakeArchive();
  const codeSha = createHash("sha256").update(Buffer.alloc(64, "x")).digest("hex");
  const state = {
    archiveStatus: 200,
    retryAfter: null as string | null,
    archiveHits: 0,
    userAgents: [] as string[],
  };
  let base = "";
  const srv = createServer((req, res) => {
    const url = req.url ?? "";
    if (url.startsWith("/api/v1/download/pro")) {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({
        tag: NEW, version: "155.0.8059.39", url: `${base}/a.zip`, sha256: createHash("sha256").update(archive).digest("hex"),
        exe_sha256: codeSha, chrome_dll_sha256: codeSha, asset: "fake.zip", archive: "zip", binary: BINARY, size: archive.length,
      }));
      return;
    }
    if (url.endsWith(".zip")) {
      state.archiveHits++;
      state.userAgents.push(String(req.headers["user-agent"] ?? ""));
      if (state.archiveStatus !== 200) {
        res.writeHead(state.archiveStatus, { "content-type": "application/json", ...(state.retryAfter ? { "retry-after": state.retryAfter } : {}) });
        res.end('{"error":"Download limit reached","code":"DOWNLOAD_RATE_LIMITED"}');
        return;
      }
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

const backoffFile = (cache: string, tag: string) => path.join(cache, `.download-backoff-${tag}.json`);

describe("Retry-After parsing", () => {
  it("reads delta-seconds and HTTP dates, and nothing else", async () => {
    const { dl } = await setup();
    const now = Date.parse("2026-10-24T10:00:00Z");
    expect(dl.retryAfterSeconds("86400", now)).toBe(86400);
    expect(dl.retryAfterSeconds("Sat, 24 Oct 2026 10:01:30 GMT", now)).toBe(90);
    expect(dl.retryAfterSeconds("Sat, 24 Oct 2026 09:00:00 GMT", now)).toBe(0);
    expect(dl.retryAfterSeconds("", now)).toBeNull();
    expect(dl.retryAfterSeconds("soon", now)).toBeNull();
    expect(dl.retryAfterSeconds(null, now)).toBeNull();
  });
});

describe.runIf(proPlatform)("PRO download fall-back and backoff", () => {
  it("a 429 on a new release launches the installed older build and pauses the download until Retry-After", async () => {
    const { cache, dl, stderr } = await setup();
    const r = await route();
    r.state.archiveStatus = 429;
    r.state.retryAfter = "3600";
    const old = verifiedTree(path.join(cache, OLD));
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };

    expect(await dl.proEnsureBinary("cc_lic_fb1", opts)).toBe(old);
    expect(r.state.archiveHits).toBe(1);
    const bo = JSON.parse(readFileSync(backoffFile(cache, NEW), "utf8"));
    expect(bo.status).toBe(429);
    expect(bo.until - Date.now()).toBeGreaterThan(3500_000);
    expect(bo.until - Date.now()).toBeLessThanOrEqual(3600_000);
    expect(stderr.join("")).toContain(`launching the installed ${OLD} instead`);

    // The next launches skip the download altogether while the pause lasts.
    expect(await dl.proEnsureBinary("cc_lic_fb1", opts)).toBe(old);
    expect(await dl.proEnsureBinary("cc_lic_fb1", opts)).toBe(old);
    expect(r.state.archiveHits).toBe(1);
  });

  it("once the pause is over the new build is downloaded, launched, and the pause file removed", async () => {
    const { cache, dl } = await setup();
    const r = await route();
    r.state.archiveStatus = 429;
    r.state.retryAfter = "3600";
    verifiedTree(path.join(cache, OLD));
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };
    await dl.proEnsureBinary("cc_lic_fb2", opts);

    const f = backoffFile(cache, NEW);
    writeFileSync(f, JSON.stringify({ ...JSON.parse(readFileSync(f, "utf8")), until: Date.now() - 1 })); // expired
    r.state.archiveStatus = 200;
    const got = await dl.proEnsureBinary("cc_lic_fb2", opts);
    expect(got).toBe(path.join(cache, NEW, "browser", BINARY));
    expect(r.state.archiveHits).toBe(2);
    expect(existsSync(f)).toBe(false);
  }, 30_000);

  it("a 429 with no older build installed fails, then fails fast without asking again while paused", async () => {
    const { cache, dl } = await setup();
    const r = await route();
    r.state.archiveStatus = 429;
    r.state.retryAfter = "600";
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };
    await expect(dl.proEnsureBinary("cc_lic_fb3", opts)).rejects.toThrow(/HTTP 429[\s\S]*CLEARCOTE_CACHE/);
    await expect(dl.proEnsureBinary("cc_lic_fb3", opts)).rejects.toThrow(/download is paused until/);
    expect(r.state.archiveHits).toBe(1);
  });

  it("a 429 without Retry-After pauses for 60 seconds", async () => {
    const { cache, dl } = await setup();
    const r = await route();
    r.state.archiveStatus = 429;
    verifiedTree(path.join(cache, OLD));
    await dl.proEnsureBinary("cc_lic_fb4", { apiBase: r.base, cacheDir: cache, quiet: true });
    const left = JSON.parse(readFileSync(backoffFile(cache, NEW), "utf8")).until - Date.now();
    expect(left).toBeGreaterThan(50_000);
    expect(left).toBeLessThanOrEqual(60_000);
  });

  it("any other failed download also falls back, with a 5-minute pause; without a fall-back nothing is paused", async () => {
    const { cache, dl } = await setup();
    const r = await route();
    r.state.archiveStatus = 503;
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };
    await expect(dl.proEnsureBinary("cc_lic_fb5", opts)).rejects.toThrow(/HTTP 503/);
    expect(existsSync(backoffFile(cache, NEW))).toBe(false); // nothing to stand in: every launch keeps trying

    const old = verifiedTree(path.join(cache, OLD));
    expect(await dl.proEnsureBinary("cc_lic_fb5", opts)).toBe(old);
    const left = JSON.parse(readFileSync(backoffFile(cache, NEW), "utf8")).until - Date.now();
    expect(left).toBeGreaterThan(290_000);
    expect(left).toBeLessThanOrEqual(300_000);
  });

  it("an exact revision pin never gets another build", async () => {
    const { cache, dl } = await setup();
    const r = await route();
    r.state.archiveStatus = 429;
    r.state.retryAfter = "3600";
    verifiedTree(path.join(cache, OLD));
    await expect(dl.proEnsureBinary("cc_lic_fb6", { apiBase: r.base, cacheDir: cache, quiet: true, version: "r50" }))
      .rejects.toThrow(/HTTP 429/);
  });

  it("never stands in a build of another Chromium major, and takes the newest revision of the same one", async () => {
    const { cache, dl } = await setup();
    const r = await route();
    r.state.archiveStatus = 429;
    r.state.retryAfter = "3600";
    verifiedTree(path.join(cache, OTHER_MAJOR));
    const opts = { apiBase: r.base, cacheDir: cache, quiet: true };
    await expect(dl.proEnsureBinary("cc_lic_fb7", opts)).rejects.toThrow(/HTTP 429/);

    verifiedTree(path.join(cache, "pro-155.0.8059.39-r42"));
    const r48 = verifiedTree(path.join(cache, OLD));
    expect(await dl.proEnsureBinary("cc_lic_fb7", opts)).toBe(r48);
  });

  it("a damaged older build is not a fall-back", async () => {
    const { cache, dl } = await setup();
    const r = await route();
    r.state.archiveStatus = 429;
    r.state.retryAfter = "3600";
    const old = verifiedTree(path.join(cache, OLD));
    writeFileSync(old, Buffer.alloc(3, "y")); // truncated: fails the manifest's size check
    await expect(dl.proEnsureBinary("cc_lic_fb8", { apiBase: r.base, cacheDir: cache, quiet: true })).rejects.toThrow(/HTTP 429/);
    expect(readdirSync(path.join(cache, OLD))).toContain("browser"); // left alone, never deleted
  });

  it("the archive download says which SDK fetched it", async () => {
    const { cache, dl } = await setup();
    const r = await route();
    await dl.proEnsureBinary("cc_lic_fb9", { apiBase: r.base, cacheDir: cache, quiet: true });
    expect(r.state.userAgents[0]).toMatch(/^clearcote-sdk\/\d+\.\d+\.\d+ \(node\)$/);
    expect(dl.DOWNLOAD_USER_AGENT).toBe(r.state.userAgents[0]);
  }, 30_000);
});
