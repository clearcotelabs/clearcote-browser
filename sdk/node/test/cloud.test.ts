// The cloud client (src/cloud.ts) against an in-memory hosted API: option mapping, local-only option
// rejection, env switching, CloudError mapping, every resource, run polling, webhook signatures and
// cookie selection. Offline: the only server is test/helpers/fake-cloud.ts. Mirrors
// sdk/python/tests/test_cloud.py.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { createHmac } from "node:crypto";
import { createServer } from "node:net";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { inspect } from "node:util";
import { chromium } from "playwright-core";
import {
  Cloud, CloudError, CloudTimeoutError, cloudRequested, cloudSessionOf, cookiesFromState, filterCookies,
  launch, launchPersistentContext, sessionBody, verifyWebhook, LOCAL_ONLY_OPTIONS, SESSION_FIELDS, Profile,
} from "../src/index.js";
import { FINGERPRINT_KEYS } from "../src/fingerprint.js";
import { AGENT_KEYS } from "../src/agent.js";
import { API_KEY, LIVE, RECORDING, startFakeCloud, type FakeCloud } from "./helpers/fake-cloud.js";

const ENV = ["CLEARCOTE_API_KEY", "CLEARCOTE_API_URL", "CLEARCOTE_CLOUD"];
const saved: Record<string, string | undefined> = {};
let api: FakeCloud;
let tmp: string;

beforeEach(async () => {
  for (const k of ENV) saved[k] = process.env[k];
  api = await startFakeCloud();
  process.env.CLEARCOTE_API_KEY = API_KEY;
  process.env.CLEARCOTE_API_URL = api.url;
  delete process.env.CLEARCOTE_CLOUD;
  tmp = mkdtempSync(join(tmpdir(), "cc-cloud-"));
});

afterEach(async () => {
  vi.restoreAllMocks();
  await api.close();
  for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
  rmSync(tmp, { recursive: true, force: true });
});

// ── local or cloud ───────────────────────────────────────────────────────────────────────────────

describe("local or cloud", () => {
  it.each([["1", true], ["true", true], ["YES", true], [" yes ", true], ["0", false], ["", false], ["no", false], ["cloud", false]])(
    "CLEARCOTE_CLOUD=%j -> %s", (value, expected) => {
      process.env.CLEARCOTE_CLOUD = value as string;
      expect(cloudRequested(undefined)).toBe(expected);
    });

  it("an explicit flag beats the environment", () => {
    process.env.CLEARCOTE_CLOUD = "1";
    expect(cloudRequested(false)).toBe(false);
    expect(cloudRequested("false" as never)).toBe(false);
    delete process.env.CLEARCOTE_CLOUD;
    expect(cloudRequested(true)).toBe(true);
  });

  it("launch() follows CLEARCOTE_CLOUD, and cloud:false keeps it local", async () => {
    const connect = vi.spyOn(chromium, "connectOverCDP").mockResolvedValue(fakeBrowser() as never);
    process.env.CLEARCOTE_CLOUD = "1";
    await launch({ country: "us" } as never);
    expect(connect).toHaveBeenCalledTimes(1);
    expect(api.requests("POST", "/api/v1/browsers")[0].body).toEqual({ country: "us" });
    // local: fails on the missing binary, without ever talking to the API
    await expect(launch({ cloud: false, executablePath: join(tmp, "missing", "chrome"), apiKey: "k", quiet: true })).rejects.toThrow();
    expect(api.requests("POST")).toHaveLength(1);
    expect(connect).toHaveBeenCalledTimes(1);
  });

  it("needs an API key", async () => {
    delete process.env.CLEARCOTE_API_KEY;
    expect(() => new Cloud()).toThrow(/CLEARCOTE_API_KEY/);
    await expect(launch({ cloud: true, country: "us" })).rejects.toThrow(/CLEARCOTE_API_KEY/);
  });

  it("base URL: default, env, argument", () => {
    delete process.env.CLEARCOTE_API_URL;
    expect(new Cloud({ apiKey: "k" }).baseUrl).toBe("https://www.clearcotelabs.com");
    process.env.CLEARCOTE_API_URL = "http://127.0.0.1:8480/";
    expect(new Cloud({ apiKey: "k" }).baseUrl).toBe("http://127.0.0.1:8480");
    expect(new Cloud({ apiKey: "k", baseUrl: "https://staging.example" }).baseUrl).toBe("https://staging.example");
    expect(JSON.stringify(new Cloud({ apiKey: "cc_live_secret" }))).not.toContain("cc_live_secret");
    expect(inspect(new Cloud({ apiKey: "cc_live_secret" }), { depth: 10 })).not.toContain("cc_live_secret");
  });

  it.each(["http://127.0.0.1:8480", "http://localhost:3000/", "http://[::1]:8480", "HTTP://LOCALHOST",
    "https://www.clearcotelabs.com", "https://10.0.0.5"])("accepts the base URL %s", (url) => {
    expect(new Cloud({ apiKey: "k", baseUrl: url }).baseUrl).toBe(url.replace(/[/]+$/, ""));
  });

  it.each(["http://www.clearcotelabs.com", "http://10.0.0.5:8480", "http://127.0.0.2", "http://localhost.evil.com",
    "http://127.0.0.1.nip.io", "http://[::2]"])("refuses plain http off this machine: %s", async (url) => {
    expect(() => new Cloud({ apiKey: "k", baseUrl: url })).toThrow(/unencrypted/);
    process.env.CLEARCOTE_API_URL = url;
    await expect(launch({ cloud: true, apiKey: "k" })).rejects.toThrow(/unencrypted/);
  });

  it.each(["ftp://example.com", "www.clearcotelabs.com", "https://", "https:example.com"])("refuses the non-web URL %s", (url) => {
    expect(() => new Cloud({ apiKey: "k", baseUrl: url })).toThrow(/must start with https:[/][/]/);
  });
});

// ── option mapping ───────────────────────────────────────────────────────────────────────────────

describe("option mapping", () => {
  it("every documented option keeps its API name", () => {
    const opts = {
      fingerprint: "seed-1", platform: "windows", brand: "Chrome", timezone: "Europe/Amsterdam", acceptLanguage: "nl-NL",
      geoip: false, headless: false, lightStealth: true, country: "us", state: "ca", city: "los angeles",
      proxySession: "sticky-1", timeoutSec: 600, idleTimeoutSec: 120, maxGb: 0.5, version: "153", profile: "acct-1",
      url: "https://example.com", adblock: true, keepAlive: true, record: true, note: "n", worker: "w1", identity: "acct-1",
      proxy: "managed",
    };
    const { acceptLanguage, ...same } = opts;
    expect(sessionBody(opts)).toEqual({ ...same, locale: acceptLanguage });
  });

  it("leaves undefined/null out and refuses locale + acceptLanguage together", () => {
    expect(sessionBody({ country: undefined, record: null, note: "x" })).toEqual({ note: "x" });
    expect(sessionBody({ locale: "de-DE" })).toEqual({ locale: "de-DE" });
    expect(() => sessionBody({ locale: "de-DE", acceptLanguage: "en-US" })).toThrow(/locale or acceptLanguage/);
  });

  it.each([
    ["managed", "managed"],
    ["http://us%40er:p%3Ass@proxy.example:3128", { server: "http://proxy.example:3128", username: "us@er", password: "p:ss" }],
    ["socks5://proxy.example:1080", { server: "socks5://proxy.example:1080" }],
    [{ server: "http://proxy.example:8080", username: "u", password: "p" }, { server: "http://proxy.example:8080", username: "u", password: "p" }],
    [{ server: "http://a:b@proxy.example:8080" }, { server: "http://proxy.example:8080", username: "a", password: "b" }],
    [{ server: "http://proxy.example:8080", bypass: null }, { server: "http://proxy.example:8080" }],
  ])("proxy %j", (given, sent) => {
    expect(sessionBody({ proxy: given })).toEqual({ proxy: sent });
  });

  it("an Object.prototype name is not an option", () => {
    for (const name of ["constructor", "toString", "hasOwnProperty", "__defineGetter__"]) {
      expect(() => sessionBody({ [name]: "x" })).toThrow(`${name} is not available for cloud browsers`);
    }
  });

  it("refuses a proxy bypass list and junk", () => {
    expect(() => sessionBody({ proxy: { server: "http://p:1", bypass: "*.local" } })).toThrow("proxy.bypass is not available for cloud browsers");
    expect(() => sessionBody({ proxy: 8080 })).toThrow(/proxy must be/);
  });

  it("profile forms", () => {
    expect(sessionBody({ profile: "acct-1" })).toEqual({ profile: "acct-1" });
    expect(sessionBody({ profile: { name: "acct-1", persist: true } })).toEqual({ profile: { name: "acct-1", persist: true } });
    expect(() => sessionBody({ profile: "auto" })).toThrow(/profile "auto"/);
    expect(() => sessionBody({ profile: new Profile("p", { fingerprint: "x" }) })).toThrow(/saved local Profile/);
    expect(() => sessionBody({ profile: { name: "a", seed: 1 } })).toThrow("profile.seed is not a cloud profile field");
  });

  it.each(["executablePath", "args", "extensions", "ignoreDefaultArgs", "gpuVendor", "hardwareConcurrency", "webrtcIp",
    "agentLlmKey", "licenseKey", "widevine", "ephemeralProfile", "slowmoTypo", "devtools", "env"])(
    "refuses the local-only option %s by name", (name) => {
      expect(() => sessionBody({ [name]: "x" })).toThrow(new RegExp(`^${name} is not available for cloud browsers$`));
    });

  it("userDataDir points at profile", () => {
    expect(() => sessionBody({ userDataDir: "/tmp/p" })).toThrow(/userDataDir is not available for cloud browsers.*profile/);
  });

  it("every local launch option is classified", () => {
    const known = new Set([...Object.keys(SESSION_FIELDS), ...LOCAL_ONLY_OPTIONS]);
    for (const k of [...(FINGERPRINT_KEYS as string[]), ...(AGENT_KEYS as string[])]) expect(known.has(k), k).toBe(true);
    for (const k of ["executablePath", "args", "userDataDir", "extensions", "ignoreDefaultArgs"]) expect(LOCAL_ONLY_OPTIONS).toContain(k);
    expect(Object.keys(SESSION_FIELDS).filter((k) => LOCAL_ONLY_OPTIONS.includes(k))).toEqual([]);
  });

  it("launch() refuses local options before any request", async () => {
    await expect(launch({ cloud: true, executablePath: "/opt/chrome" })).rejects.toThrow("executablePath is not available for cloud browsers");
    await expect(launchPersistentContext("/tmp/x", { cloud: true, profile: "p" } as never)).rejects.toThrow(/userDataDir/);
    await expect(launchPersistentContext({ cloud: true } as never)).rejects.toThrow(/needs profile: "name"/);
    expect(api.log).toEqual([]);
  });

  it("a local launchPersistentContext still needs a directory", async () => {
    await expect(launchPersistentContext(undefined as never)).rejects.toThrow(/needs a userDataDir/);
  });
});

// ── launch({ cloud: true }) with a stand-in Playwright ───────────────────────────────────────────

function fakeBrowser() {
  const ctx = { pages: () => [], on: () => {}, addInitScript: async () => {} };
  const b = {
    closed: false,
    contexts: () => [ctx],
    newPage: async (o: unknown) => o,
    newContext: async (o: unknown) => o,
    close: async () => { b.closed = true; },
    on: () => {},
  };
  return b;
}

describe("launch({ cloud: true })", () => {
  it("creates, connects, and closes", async () => {
    api.connectUrl = "wss://w1.example/v1/connect/bs_1?token=t";
    const fb = fakeBrowser();
    const connect = vi.spyOn(chromium, "connectOverCDP").mockResolvedValue(fb as never);
    const b = await launch({ cloud: true, country: "us", identity: "acct-1", timeout: 5000, slowMo: 10 });
    expect(connect).toHaveBeenCalledWith(api.connectUrl, { timeout: 5000, slowMo: 10 });
    expect(api.requests("POST", "/api/v1/browsers")[0].body).toEqual({ country: "us", identity: "acct-1" });
    expect(cloudSessionOf(b).id).toBe("bs_1");
    expect(cloudSessionOf(b).connectUrl).toBeUndefined();
    expect(await b.newPage()).toEqual({ viewport: null }); // no emulated viewport, as a local launch
    await b.close();
    expect(fb.closed).toBe(true);
    expect(api.requests("DELETE", "/api/v1/browsers/bs_1")).toHaveLength(1);
  });

  it("sends the key and the SDK user agent", async () => {
    vi.spyOn(chromium, "connectOverCDP").mockResolvedValue(fakeBrowser() as never);
    await launch({ cloud: true, apiKey: API_KEY, apiUrl: api.url });
    const h = api.requests("POST", "/api/v1/browsers")[0].headers;
    const version = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version;
    expect(h.authorization).toBe(`Bearer ${API_KEY}`);
    expect(h["user-agent"]).toBe(`clearcote-sdk-node/${version}`);
    expect(h["content-type"]).toBe("application/json");
  });

  it("a failed connect ends the session", async () => {
    vi.spyOn(chromium, "connectOverCDP").mockRejectedValue(new Error("connect refused"));
    await expect(launch({ cloud: true })).rejects.toThrow("connect refused");
    expect(api.requests("DELETE", "/api/v1/browsers/bs_1")).toHaveLength(1);
  });

  it("a failure after connecting disconnects and ends the session (keepAlive or not)", async () => {
    const fb = fakeBrowser();
    (fb as { contexts: unknown }).contexts = () => { throw new Error("target crashed"); };
    vi.spyOn(chromium, "connectOverCDP").mockResolvedValue(fb as never);
    await expect(launch({ cloud: true, humanize: true, keepAlive: true })).rejects.toThrow("target crashed");
    expect(fb.closed).toBe(true);
    expect(api.requests("DELETE", "/api/v1/browsers/bs_1")).toHaveLength(1);
  });

  it("a keep-alive session is left running on close", async () => {
    vi.spyOn(chromium, "connectOverCDP").mockResolvedValue(fakeBrowser() as never);
    const b = await launch({ cloud: true, keepAlive: true });
    await b.close();
    expect(api.requests("DELETE")).toEqual([]);
  });

  it("a persistent cloud context loads and saves the profile", async () => {
    const fb = fakeBrowser();
    vi.spyOn(chromium, "connectOverCDP").mockResolvedValue(fb as never);
    const ctx = await launchPersistentContext({ cloud: true, profile: "acct-1" });
    expect(api.requests("POST", "/api/v1/browsers")[0].body.profile).toEqual({ name: "acct-1", persist: true });
    expect(ctx).toBe(fb.contexts()[0]);
    expect(cloudSessionOf(ctx).id).toBe("bs_1");
    await ctx.close();
    expect(fb.closed).toBe(true);
    expect(api.requests("DELETE", "/api/v1/browsers/bs_1")).toHaveLength(1);
    await launchPersistentContext({ cloud: true, profile: { name: "acct-2", persist: false } });
    expect(api.requests("POST", "/api/v1/browsers")[1].body.profile).toEqual({ name: "acct-2", persist: false });
  });
});

// ── errors ───────────────────────────────────────────────────────────────────────────────────────

const CRLF = String.fromCharCode(13, 10);

/** An HTTP answer with a Content-Length that may promise more than the body holds. */
function httpAnswer(statusLine: string, body: string, length = Buffer.byteLength(body)): string {
  return [statusLine, "Content-Type: application/json", `Content-Length: ${length}`].join(CRLF) + CRLF + CRLF + body;
}

/** A raw TCP server: reads a request, writes `answer`, hangs up. */
async function oneShotServer(answer: string): Promise<{ url: string; close: () => Promise<void> }> {
  const srv = createServer((sock) => {
    sock.once("data", () => sock.end(answer));
    sock.on("error", () => {});
  });
  await new Promise<void>((r) => srv.listen(0, "127.0.0.1", () => r()));
  const port = (srv.address() as { port: number }).port;
  return { url: `http://127.0.0.1:${port}`, close: () => new Promise<void>((r) => srv.close(() => r())) };
}

describe("CloudError", () => {
  it("carries status, code and the server's message", async () => {
    const e1 = await new Cloud({ apiKey: "wrong" }).browsers.get("bs_1").catch((e) => e);
    expect(e1).toBeInstanceOf(CloudError);
    expect([e1.status, e1.code, e1.message]).toEqual([401, null, "Missing or invalid API key."]);
    const e2 = await new Cloud().profiles.importCookies("busy", [{ name: "a", value: "b", domain: "x.com" }]).catch((e) => e);
    expect([e2.status, e2.code, e2.message]).toEqual([409, "PROFILE_IN_USE", "A live session is saving to this profile."]);
  });

  it("for a non-JSON answer", async () => {
    api.flaky = 99;
    const e = await new Cloud().runs.get("bs_run1").catch((x) => x);
    expect([e.status, e.message]).toEqual([503, "upstream restarting"]);
  });

  it("a connection cut while the answer is read is a NETWORK error (which waits retry)", async () => {
    const { url, close } = await oneShotServer(httpAnswer("HTTP/1.1 200 OK", '{"id":', 500));
    try {
      const e = await new Cloud({ apiKey: "k", baseUrl: url }).runs.get("bs_1").catch((x) => x);
      expect(e).toBeInstanceOf(CloudError);
      expect([e.status, e.code]).toEqual([0, "NETWORK"]);
    } finally {
      await close();
    }
  });

  it("reads a nested { error: { message, code } } too", async () => {
    const body = JSON.stringify({ error: { message: "Balance too low.", code: "INSUFFICIENT_BALANCE" } });
    const { url, close } = await oneShotServer(httpAnswer("HTTP/1.1 402 Payment Required", body));
    try {
      const e = await new Cloud({ apiKey: "k", baseUrl: url }).browsers.create().catch((x) => x);
      expect([e.status, e.code, e.message]).toEqual([402, "INSUFFICIENT_BALANCE", "Balance too low."]);
    } finally {
      await close();
    }
  });

  it("when unreachable", async () => {
    const port = await new Promise<number>((r) => { const s = createServer(); s.listen(0, "127.0.0.1", () => { const p = (s.address() as { port: number }).port; s.close(() => r(p)); }); });
    const e = await new Cloud({ apiKey: "k", baseUrl: `http://127.0.0.1:${port}` }).browsers.list().catch((x) => x);
    expect([e.status, e.code]).toEqual([0, "NETWORK"]);
  });
});

// ── browsers ─────────────────────────────────────────────────────────────────────────────────────

describe("browsers", () => {
  it("create, get, list, stop, live, share", async () => {
    const c = new Cloud();
    const created = await c.browsers.create({ country: "us", record: true });
    expect(created.id).toBe("bs_1");
    expect(api.log.at(-1)!.body).toEqual({ country: "us", record: true });
    expect((await c.browsers.get("bs_1")).status).toBe("active");
    expect((await c.browsers.list({ status: ["active", "ended"], limit: 5 })).balanceEur).toBe(12.5);
    expect(api.log.at(-1)!.query).toBe("status=active%2Cended&limit=5");
    expect((await c.browsers.stop("bs_1")).status).toBe("ended");
    expect((await c.browsers.live("bs_1", { control: true })).interactive).toBe(true);
    expect(api.log.at(-1)!.query).toBe("control=1");
    expect((await c.browsers.live("bs_1")).interactive).toBe(false);
    const share = await c.browsers.share("bs_1", { recording: true, minutes: 30 });
    expect(share.url).toContain("/replay/");
    expect(api.log.at(-1)!.body).toEqual({ minutes: 30, recording: true });
    expect(() => c.browsers.create({ humanize: true } as never)).toThrow("humanize is not available for cloud browsers");
  });

  it("hand-off: request, wait, done", async () => {
    const c = new Cloud();
    const h = await c.browsers.handoff("bs_1", { reason: "solve the captcha", timeoutSec: 300 });
    expect(h).toEqual({ state: "waiting", reason: "solve the captcha", since: "2026-10-02T10:00:00.000Z", expiresAt: "2026-10-02T10:10:00.000Z", liveUrl: LIVE });
    expect(api.log.at(-1)!.body).toEqual({ reason: "solve the captcha", timeoutSec: 300 });
    expect((await c.browsers.waitHandoff("bs_1", { poll: 0.01 })).handoff.state).toBe("done");
    expect(api.requests("GET", "/api/v1/browsers/bs_1")).toHaveLength(3);
    expect((await c.browsers.handoffDone("bs_1")).state).toBe("done");
    expect(api.log.at(-1)!.path).toBe("/api/v1/browsers/bs_1/handoff/done");
  });

  it("waitHandoff times out with the last view", async () => {
    api.handoffPolls = 10_000;
    const c = new Cloud();
    await c.browsers.handoff("bs_1");
    const e = await c.browsers.waitHandoff("bs_1", { timeout: 0.05, poll: 0.01 }).catch((x) => x);
    expect(e).toBeInstanceOf(CloudTimeoutError);
    expect(e.last.handoff.state).toBe("waiting");
  });

  it("waitHandoff on a session with no hand-off rejects with NO_HANDOFF", async () => {
    // handoff: null reads as "not waiting"; resolving at once would look like a finished hand-off
    const c = new Cloud();
    const e = await c.browsers.waitHandoff("bs_7", { poll: 0.01 }).catch((x) => x);
    expect(e).toBeInstanceOf(CloudError);
    expect([e.status, e.code]).toEqual([200, "NO_HANDOFF"]);
    expect(e.message).toContain("no hand-off was requested for session bs_7");
    expect(api.requests("GET", "/api/v1/browsers/bs_7")).toHaveLength(1);
    // a hand-off that is already over (done, or timed out) still resolves at once
    api.handoffPolls = 0;
    await c.browsers.handoff("bs_8");
    expect((await c.browsers.waitHandoff("bs_8", { poll: 0.01 })).handoff.state).toBe("done");
  });

  it("events are paged with next", async () => {
    const c = new Cloud();
    let page = await c.browsers.events("bs_1");
    expect([page.events.map((e: { seq: number }) => e.seq), page.next]).toEqual([[1, 2], 2]);
    page = await c.browsers.events("bs_1", { after: page.next, limit: 10 });
    expect([page.events.map((e: { seq: number }) => e.seq), page.next]).toEqual([[3, 4], null]);
    expect(api.log.at(-1)!.query).toBe("after=2&limit=10");
  });

  it("recording: url, download without the key, errors", async () => {
    const c = new Cloud();
    expect(await c.browsers.recordingUrl("bs_1")).toBe(`${api.url}/dev/blob/rec.mp4?sig=abc`);
    const out = await c.browsers.downloadRecording("bs_1", join(tmp, "r.mp4"));
    expect(readFileSync(out)).toEqual(RECORDING);
    expect(api.requests("GET", "/dev/blob/rec.mp4").at(-1)!.headers.authorization).toBeUndefined();
    api.recordingState = "processing";
    const e = await c.browsers.recordingUrl("bs_1").catch((x) => x);
    expect([e.status, e.code]).toEqual([409, "NOT_READY"]);
    const e404 = await c.browsers.downloadRecording("bs_unrecorded", join(tmp, "x.mp4")).catch((x) => x);
    expect(e404.status).toBe(404);
    expect(existsSync(join(tmp, "x.mp4"))).toBe(false);
  });
});

describe("recording URL", () => {
  it("must be a web URL", async () => {
    api.recordingLocation = "file:///etc/passwd";
    const e = await new Cloud().browsers.recordingUrl("bs_1").catch((x) => x);
    expect(e).toBeInstanceOf(CloudError);
    expect(e.message).toBe("the API did not answer with a recording URL");
  });
});

describe("recording on another host", () => {
  it("never gets the API key, and the API redirect is not followed", async () => {
    const storage = await startFakeCloud();
    try {
      api.recordingLocation = `${storage.url}/dev/blob/rec.mp4?sig=abc`;
      const c = new Cloud();
      expect(await c.browsers.recordingUrl("bs_1")).toBe(api.recordingLocation);
      expect(storage.log).toEqual([]);
      const out = await c.browsers.downloadRecording("bs_1", join(tmp, "r.mp4"));
      expect(readFileSync(out)).toEqual(RECORDING);
      expect(storage.log).toHaveLength(1);
      expect([storage.log[0].path, storage.log[0].query]).toEqual(["/dev/blob/rec.mp4", "sig=abc"]);
      expect(storage.log[0].headers.authorization).toBeUndefined();
      expect(api.log.every((r) => r.headers.authorization === `Bearer ${API_KEY}`)).toBe(true);
    } finally {
      await storage.close();
    }
  });
});

// ── runs ─────────────────────────────────────────────────────────────────────────────────────────

describe("runs", () => {
  it("create maps the options and polls to the end", async () => {
    const updates: string[] = [];
    const schema = { type: "object", properties: { price: { type: "string" } } };
    const run = await new Cloud().runs.create("Find the price", {
      url: "https://example.com/", schema, secrets: { pw: { value: "hunter2", domains: ["example.com"] } },
      maxSteps: 20, handoff: true, handoffTimeoutSec: 120, record: true, country: "us", poll: 0.01,
      onUpdate: (v) => { updates.push(v.status); },
    });
    expect(api.requests("POST", "/api/v1/runs")[0].body).toEqual({
      task: "Find the price", url: "https://example.com/", schema, secrets: { pw: { value: "hunter2", domains: ["example.com"] } },
      maxSteps: 20, handoff: true, handoffTimeoutSec: 120, record: true, country: "us" });
    expect(run.status).toBe("succeeded");
    expect(run.result.output).toEqual({ plan: "Starter", price: "9.99" });
    expect(updates).toEqual(["queued", "running", "waiting_for_human", "running", "succeeded"]);
  });

  it("reports waiting_for_human on stderr without a callback", async () => {
    let err = "";
    vi.spyOn(process.stderr, "write").mockImplementation(((c: string) => { err += String(c); return true; }) as never);
    await new Cloud().runs.create("t", { poll: 0.01 });
    expect(err).toContain(`run bs_run1 is waiting for a human (needs a login): ${LIVE}`);
  });

  it("wait: false resolves to the create answer", async () => {
    expect((await new Cloud().runs.create("t", { wait: false })).status).toBe("queued");
    expect(api.requests("GET")).toEqual([]);
  });

  it("times out with the last view", async () => {
    api.runStatuses = ["running"];
    const e = await new Cloud().runs.wait("bs_run1", { timeout: 0.05, poll: 0.01 }).catch((x) => x);
    expect(e).toBeInstanceOf(CloudTimeoutError);
    expect(e.last.status).toBe("running");
    expect(e.message).toContain("bs_run1");
  });

  it("retries transient errors", async () => {
    api.runStatuses = ["succeeded"];
    api.flaky = 2;
    expect((await new Cloud().runs.wait("bs_run1", { poll: 0.01 })).status).toBe("succeeded");
  });

  it("rejects local options, and the server's own rules come back as CloudError", async () => {
    await expect(new Cloud().runs.create("t", { args: ["--x"] } as never)).rejects.toThrow("args is not available for cloud runs");
    await expect(new Cloud().runs.create("t", { keepAlive: true })).rejects.toThrow("keepAlive does not apply to runs");
  });

  it("get, list, cancel", async () => {
    const c = new Cloud();
    expect((await c.runs.get("bs_run1")).status).toBe("queued");
    expect((await c.runs.list({ limit: 5 })).runs[0].id).toBe("bs_run1");
    expect(api.log.at(-1)!.query).toBe("limit=5");
    expect((await c.runs.cancel("bs_run1")).status).toBe("cancelled");
    expect(api.log.at(-1)!.method).toBe("DELETE");
  });
});

// ── profiles ─────────────────────────────────────────────────────────────────────────────────────

const STATE = {
  cookies: [
    { name: "sid", value: "1", domain: ".example.com", path: "/", expires: -1, httpOnly: true, secure: true, sameSite: "Lax" },
    { name: "pref", value: "2", domain: "www.example.com", path: "/", expires: 1893456000, httpOnly: false, secure: false, sameSite: "None" },
    { name: "x", value: "3", domain: "badexample.com", path: "/", expires: -1, httpOnly: false, secure: false, sameSite: "Lax" },
    { name: "t", value: "4", domain: ".tracker.net", path: "/", expires: -1, httpOnly: false, secure: false, sameSite: "Lax", size: 5, priority: "Medium", sourceScheme: "Secure" },
  ],
  origins: [],
};

describe("profiles", () => {
  it("filters cookies by domain", () => {
    const names = (d: string[]) => filterCookies(STATE.cookies, d).map((c) => c.name);
    expect(names(["example.com"])).toEqual(["sid", "pref"]);
    expect(names([".EXAMPLE.com "])).toEqual(["sid", "pref"]);
    // www.example.com also gets the .example.com cookie: the browser sends it there
    expect(names(["www.example.com"])).toEqual(["sid", "pref"]);
    expect(names(["tracker.net", "nope.org"])).toEqual(["t"]);
    expect(names([])).toEqual([]);
  });

  it("keeps the parent-domain and subdomain cookies of a host, never a bare suffix", () => {
    const jar = ["www.example.com", ".example.com", "example.com", "a.www.example.com", "other.example.com",
      ".com", "com", "badexample.com", "www.badexample.com", "example.com.evil.net", ".co.uk", "localhost"];
    const cookies = jar.map((d, i) => ({ name: `c${i}`, value: "v", domain: d }));
    const domains = (allowed: string[]) => filterCookies(cookies, allowed).map((c) => c.domain);
    expect(domains(["www.example.com"])).toEqual(["www.example.com", ".example.com", "example.com", "a.www.example.com"]);
    expect(domains(["example.com"])).toEqual(["www.example.com", ".example.com", "example.com", "a.www.example.com", "other.example.com"]);
    expect(domains([".WWW.Example.com"])).toEqual(domains(["www.example.com"]));
    expect(domains(["shop.example.co.uk"])).toEqual([".co.uk"]); // no PSL: a two-label parent is kept
    expect(domains(["localhost"])).toEqual(["localhost"]);
    expect(domains(["evil.net"])).toEqual(["example.com.evil.net"]);
    expect(domains(["net"])).toEqual(["example.com.evil.net"]); // an explicit choice of a whole TLD
  });

  it("parses a storage state, a cookie array, and refuses anything else", () => {
    expect(cookiesFromState(STATE)).toEqual(STATE.cookies);
    expect(cookiesFromState(STATE.cookies)).toEqual(STATE.cookies);
    expect(cookiesFromState({ cookies: [] })).toEqual([]);
    expect(() => cookiesFromState({ origins: [] })).toThrow(/storage state/);
    expect(() => cookiesFromState([{ value: "x" }])).toThrow(/name and a domain/);
  });

  it("list, import, get, delete", async () => {
    const c = new Cloud();
    expect((await c.profiles.list()).profiles[0].name).toBe("acct-1");
    const r = await c.profiles.importCookies("acct 1/x", [{ name: "a", value: "1", domain: "a.com" }]);
    expect(api.log.at(-1)!.path).toBe("/api/v1/browsers/profiles/acct%201%2Fx/cookies");
    expect(api.log.at(-1)!.body).toEqual({ cookies: [{ name: "a", value: "1", domain: "a.com" }], mode: "merge" });
    expect(r.imported).toBe(1);
    expect((await c.profiles.get("acct 1/x")).cookies).toBe(1);
    expect(await c.profiles.delete("acct-1")).toEqual({ ok: true });
  });

  it("sync from a file uploads only the chosen domains", async () => {
    const f = join(tmp, "state.json");
    writeFileSync(f, JSON.stringify(STATE));
    const c = new Cloud();
    const res = await c.profiles.sync("acct-1", { fromFile: f, domains: ["example.com"], replace: true });
    const sent = api.requests("PUT").at(-1)!.body;
    expect(sent.mode).toBe("replace");
    expect(sent.cookies).toEqual([STATE.cookies[0], STATE.cookies[1]]);
    expect(res).toEqual({ name: "acct-1", cookies: 2, imported: 2, domains: ["example.com", "www.example.com"], bytes: 1234, updatedAt: "2026-10-02T10:00:00.000Z" });
    await c.profiles.sync("acct-1", { fromFile: f, allDomains: true });
    const all = api.requests("PUT").at(-1)!.body;
    expect(all.mode).toBe("merge");
    expect(all.cookies).toHaveLength(4);
    expect(all.cookies[3].size).toBeUndefined();
  });

  it("sync refuses without a domain choice, and reads nothing", async () => {
    const f = join(tmp, "state.json");
    writeFileSync(f, JSON.stringify(STATE));
    const c = new Cloud();
    await expect(c.profiles.sync("acct-1", { fromFile: f })).rejects.toThrow(/allDomains: true/);
    await expect(c.profiles.sync("acct-1", { fromFile: f, domains: ["a.com"], allDomains: true })).rejects.toThrow(/not both/);
    await expect(c.profiles.sync("acct-1", { fromFile: f, fromCdp: "http://127.0.0.1:1", domains: ["a.com"] })).rejects.toThrow(/exactly one/);
    await expect(c.profiles.sync("acct-1", { fromFile: f, domains: ["nothing.example"] })).rejects.toThrow("no cookies found for nothing.example");
    expect(api.log).toEqual([]);
  });
});

// ── webhooks ─────────────────────────────────────────────────────────────────────────────────────

describe("webhooks", () => {
  it("create, list, delete, test", async () => {
    const c = new Cloud();
    const hook = await c.webhooks.create("https://hooks.example.com/x", { events: ["run.finished"], description: "d" });
    expect(hook.secret).toBe("whsec_test_secret");
    expect(api.log.at(-1)!.body).toEqual({ url: "https://hooks.example.com/x", events: ["run.finished"], description: "d" });
    await c.webhooks.create("https://hooks.example.com/y");
    expect(api.log.at(-1)!.body).toEqual({ url: "https://hooks.example.com/y" });
    expect((await c.webhooks.list()).webhooks.map((h: { id: string }) => h.id)).toEqual(["wh_1", "wh_2"]);
    expect(await c.webhooks.delete("wh_1")).toEqual({ ok: true });
    expect((await c.webhooks.test("wh_2")).ok).toBe(true);
    expect(api.log.at(-1)!.path).toBe("/api/v1/webhooks/wh_2/test");
  });
});

// ── verifyWebhook ────────────────────────────────────────────────────────────────────────────────

const SECRET = "whsec_test_secret";
const BODY = '{"id":"evt_1","type":"run.finished","createdAt":"2026-10-02T10:00:00.000Z","data":{"id":"bs_run1","status":"succeeded"}}';
const NOW = 1_790_000_000;
const sign = (body: string, t = NOW, secret = SECRET) => `t=${t},v1=${createHmac("sha256", secret).update(`${t}.${body}`).digest("hex")}`;

describe("verifyWebhook", () => {
  it("accepts the fixed vector the Python tests use too", () => {
    const vector = "t=1790000000,v1=e7f51b8aaf2b3c2682ae9fa9dcf7d2c08b4c0f624cf5e55250685f75bdcaf419";
    expect(sign(BODY)).toBe(vector);
    expect(verifyWebhook(BODY, vector, SECRET, 300, NOW).data.status).toBe("succeeded");
    expect(verifyWebhook(Buffer.from(BODY), sign(BODY), SECRET, 300, NOW + 299).id).toBe("evt_1");
  });

  it("refuses a tampered body, a wrong secret and a changed timestamp", () => {
    expect(() => verifyWebhook(BODY.replace("succeeded", "failed"), sign(BODY), SECRET, 300, NOW)).toThrow(/does not match/);
    expect(() => verifyWebhook(BODY, sign(BODY, NOW, "whsec_other"), SECRET, 300, NOW)).toThrow(/does not match/);
    expect(() => verifyWebhook(BODY, sign(BODY).replace(`t=${NOW}`, `t=${NOW + 1}`), SECRET, 300, NOW)).toThrow(/does not match/);
  });

  it("refuses an old or future timestamp, unless the window is off", () => {
    expect(() => verifyWebhook(BODY, sign(BODY), SECRET, 300, NOW + 301)).toThrow(/tolerance/);
    expect(() => verifyWebhook(BODY, sign(BODY), SECRET, 300, NOW - 301)).toThrow(/tolerance/);
    expect(verifyWebhook(BODY, sign(BODY), SECRET, null, NOW + 1e6).id).toBe("evt_1");
    expect(verifyWebhook(BODY, sign(BODY), SECRET, 600, NOW + 500).id).toBe("evt_1");
  });

  it("any one of several v1 signatures is enough", () => {
    const good = sign(BODY).split("v1=")[1];
    expect(verifyWebhook(BODY, `t=${NOW}, v1=${"0".repeat(64)}, v1=${good.toUpperCase()}`, SECRET, 300, NOW).type).toBe("run.finished");
    expect(() => verifyWebhook(BODY, `t=${NOW},v1=${"0".repeat(64)},v1=abc`, SECRET, 300, NOW)).toThrow(/does not match/);
  });

  it.each(["", "v1=abc", `t=${NOW}`, `t=abc,v1=${"0".repeat(64)}`, "garbage", null])("refuses the malformed header %j", (header) => {
    expect(() => verifyWebhook(BODY, header, SECRET, 300, NOW)).toThrow(/invalid Clearcote-Signature/);
  });

  it.each([undefined, null, "", "  "])("needs a secret (got %j)", (secret) => {
    // String(undefined) is "undefined": without this check an unset secret verifies what anyone signs
    for (const key of ["undefined", "null", "", "  "]) {
      expect(() => verifyWebhook(BODY, sign(BODY, NOW, key), secret as never, 300, NOW)).toThrow(/signing secret/);
    }
  });

  it("refuses an oversized header before any work", () => {
    const many = `${sign(BODY)},v1=${Array(200).fill("0".repeat(64)).join(",v1=")}`;
    expect(() => verifyWebhook(BODY, many, SECRET, 300, NOW)).toThrow(/invalid Clearcote-Signature/);
    expect(() => verifyWebhook(BODY, `t=${"9".repeat(100000)},v1=${"0".repeat(64)}`, SECRET, 300, NOW)).toThrow(/invalid Clearcote-Signature/);
  });
});
