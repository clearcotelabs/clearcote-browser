// `clearcote cloud ...` against the fake API: exact output, exit codes and the refusal to upload every
// cookie. The golden outputs are byte-identical to sdk/python/tests/test_parity_cloud_cli.py, which also
// runs this CLI (dist/) and the Python one side by side and compares their output and requests.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CLOUD_USAGE, USAGE, cloudMain, main } from "../src/cli-commands.js";
import { API_KEY, RECORDING, startFakeCloud, type FakeCloud } from "./helpers/fake-cloud.js";

const STATE = {
  cookies: [
    { name: "sid", value: "1", domain: ".example.com", path: "/", expires: -1, httpOnly: true, secure: true, sameSite: "Lax" },
    { name: "pref", value: "2", domain: "www.example.com", path: "/", expires: 1893456000, httpOnly: false, secure: false, sameSite: "None" },
    { name: "x", value: "3", domain: "badexample.com", path: "/", expires: -1, httpOnly: false, secure: false, sameSite: "Lax" },
  ],
  origins: [],
};

const GOLDEN = {
  run: [
    "run bs_run1 succeeded",
    "result  done",
    "url     https://example.com/pricing",
    "title   Pricing",
    "output  {",
    '  "plan": "Starter",',
    '  "price": "9.99"',
    "}",
    "cost    €0.0019",
  ],
  sessions: [
    "bs_a1  active  2026-10-02T09:00:00.000Z  €0.0123  crawler",
    "bs_b2  ended  2026-10-01T09:00:00.000Z  €0.5000",
    "balance €12.50",
  ],
  stop: ["stopped bs_a1 (ended)"],
  events: [
    '1  2026-10-02T10:00:00.000Z  session.started  {"kind":"run"}',
    '2  2026-10-02T10:00:01.000Z  navigation  {"url":"https://example.com/","title":"Example Domain"}',
    "3  2026-10-02T10:00:02.000Z  tab.closed",
    '4  2026-10-02T10:00:03.000Z  session.ended  {"reason":"run_finished"}',
  ],
  profile: [
    "imported 2 cookies into profile acct-1 (2 in total)",
    "domains  example.com, www.example.com",
  ],
  webhooksAdd: [
    "webhook wh_1 -> https://hooks.example.com/x",
    "events  run.finished",
    "secret  whsec_test_secret",
    "store the secret now: it is not shown again",
  ],
  webhooksList: [
    "wh_9  https://hooks.example.com/cc  run.finished  last 200 2026-10-02T10:00:00.000Z",
    "wh_8  https://hooks.example.com/all  all",
  ],
};

const ENV = ["CLEARCOTE_API_KEY", "CLEARCOTE_API_URL", "CLEARCOTE_CLOUD"];
const saved: Record<string, string | undefined> = {};
let api: FakeCloud;
let dir: string;

beforeEach(async () => {
  for (const k of ENV) saved[k] = process.env[k];
  api = await startFakeCloud();
  api.runStatuses = ["succeeded"];
  process.env.CLEARCOTE_API_KEY = API_KEY;
  process.env.CLEARCOTE_API_URL = api.url;
  delete process.env.CLEARCOTE_CLOUD;
  dir = mkdtempSync(join(tmpdir(), "cc-cloud-cli-"));
  writeFileSync(join(dir, "state.json"), JSON.stringify(STATE));
});

afterEach(async () => {
  vi.restoreAllMocks();
  await api.close();
  for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
  rmSync(dir, { recursive: true, force: true });
});

async function cli(...argv: string[]): Promise<{ code: number; out: string[]; err: string }> {
  let out = "";
  let err = "";
  const o = vi.spyOn(process.stdout, "write").mockImplementation(((c: string) => { out += String(c); return true; }) as never);
  const e = vi.spyOn(process.stderr, "write").mockImplementation(((c: string) => { err += String(c); return true; }) as never);
  try {
    const code = await cloudMain(argv);
    return { code, out: out ? out.replace(/\n$/, "").split("\n") : [], err };
  } finally {
    o.mockRestore();
    e.mockRestore();
  }
}

describe("clearcote cloud", () => {
  it("is documented", () => {
    expect(USAGE).toContain("clearcote cloud run <task>");
    expect(USAGE).toContain("CLEARCOTE_API_KEY");
    for (const flag of ["--url", "--schema", "--secret", "--secret-domain", "--handoff", "--record", "--json", "sessions", "stop <id>",
      "events <id>", "recording <id>", "-o <file.mp4>", "profile sync", "--from-profile", "--from-cdp", "--from-file", "--login",
      "--domain", "--all-domains", "--replace", "webhooks add", "--event", "webhooks list", "webhooks rm", "webhooks test",
      "--country", "--state", "--city", "--proxy", "--profile", "--persist-profile", "--fingerprint", "--timeout-sec",
      "--max-steps", "--note"]) {
      expect(CLOUD_USAGE).toContain(flag);
    }
  });

  it("--help", async () => {
    const r = await cli("--help");
    expect(r.code).toBe(0);
    expect(r.out.join("\n")).toBe(CLOUD_USAGE);
  });

  it("main() routes `cloud` and sets the exit code", async () => {
    api.runStatuses = ["failed"];
    vi.spyOn(process.stdout, "write").mockImplementation((() => true) as never);
    vi.spyOn(process.stderr, "write").mockImplementation((() => true) as never);
    const before = process.exitCode;
    await main(["cloud", "run", "t"]);
    expect(process.exitCode).toBe(1);
    process.exitCode = before;
  });

  it("run prints the result and exits on its status", async () => {
    writeFileSync(join(dir, "schema.json"), '{"type": "object"}');
    const r = await cli("run", "Find", "the", "price", "--url", "https://example.com/", "--schema", join(dir, "schema.json"),
      "--secret", "pw=hunter2", "--secret", "otp=x=y", "--secret-domain", "pw=Example.com", "--handoff", "--record");
    expect([r.code, r.out]).toEqual([0, GOLDEN.run]);
    expect(r.err).toContain("[clearcote] run bs_run1: succeeded");
    expect(api.requests("POST", "/api/v1/runs")[0].body).toEqual({
      task: "Find the price", url: "https://example.com/", schema: { type: "object" },
      secrets: { pw: { value: "hunter2", domains: ["example.com"] }, otp: "x=y" }, handoff: true, record: true });
    api.runStatuses = ["failed"];
    const f = await cli("run", "t");
    expect([f.code, f.out]).toEqual([1, ["run bs_run1 failed", "cost    €0.0019"]]);
  });

  it("run: the browser options map to the API fields", async () => {
    const opts = ["--country", "us", "--state", "ca", "--city", "los angeles", "--proxy", "http://u:p@proxy.example:8080",
      "--profile", "acct-1", "--persist-profile", "--fingerprint", "seed-7", "--timeout-sec", "600", "--max-steps", "12",
      "--note", "nightly"];
    expect((await cli("run", "t", ...opts)).code).toBe(0);
    expect(api.requests("POST", "/api/v1/runs")[0].body).toEqual({
      task: "t", country: "us", state: "ca", city: "los angeles",
      proxy: { server: "http://proxy.example:8080", username: "u", password: "p" },
      profile: { name: "acct-1", persist: true }, fingerprint: "seed-7", timeoutSec: 600, maxSteps: 12, note: "nightly" });
    expect((await cli("run", "t", "--proxy", "managed", "--profile", "acct-2")).code).toBe(0);
    expect(api.requests("POST", "/api/v1/runs")[1].body).toEqual({ task: "t", proxy: "managed", profile: "acct-2" });
    const sent = api.log.length;
    const p = await cli("run", "t", "--persist-profile");
    expect([p.code, p.err.includes("--persist-profile needs --profile <name>")]).toEqual([2, true]);
    const m = await cli("run", "t", "--max-steps", "ten");
    expect([m.code, m.err.includes("--max-steps wants a whole number")]).toEqual([2, true]);
    expect((await cli("run", "t", "--timeout-sec", "1.5")).code).toBe(2);
    expect((await cli("run", "t", "--coun", "us")).code).toBe(2);
    expect(api.log.length).toBe(sent);
  });

  it("run --json", async () => {
    const r = await cli("run", "t", "--json");
    const run = JSON.parse(r.out.join("\n"));
    expect([r.code, run.status, run.result.output.price]).toEqual([0, "succeeded", "9.99"]);
  });

  it("run argument errors exit 2 before any request", async () => {
    // a --secret argument is never echoed, not even a malformed one: it may be the bare secret
    for (const args of [["--secret", "hunter2"], ["--secret", "=hunter2"], ["--secret", "pw", "hunter2"],
      ["--secret", "pw=x", "--secret", "pw2", "-hunter2"]]) {
      const r = await cli("run", "t", ...args);
      expect(r.code).toBe(2);
      expect(r.err).not.toContain("hunter2");
    }
    expect((await cli("run", "t", "--secret", "hunter2")).err).toContain("--secret wants <name>=<value>");
    expect((await cli("run", "t", "--secret-domain", "pw=a.com")).code).toBe(2);
    expect((await cli("run", "t", "--schema", join(dir, "missing.json"))).code).toBe(2);
    expect((await cli("run")).code).toBe(2);
    expect(api.log).toEqual([]);
  });

  it("run: the task words may sit between the flags", async () => {
    expect((await cli("run", "Find", "--url", "https://example.com/", "the", "--record", "price")).code).toBe(0);
    expect(api.requests("POST", "/api/v1/runs")[0].body).toEqual({ task: "Find the price", url: "https://example.com/", record: true });
  });

  it("sessions, stop, events", async () => {
    expect(await cli("sessions")).toMatchObject({ code: 0, out: GOLDEN.sessions });
    expect(await cli("stop", "bs_a1")).toMatchObject({ code: 0, out: GOLDEN.stop });
    expect(await cli("events", "bs_a1")).toMatchObject({ code: 0, out: GOLDEN.events });
    expect(api.requests("GET", "/api/v1/browsers/bs_a1/events").map((r) => r.query)).toEqual(["after=0", "after=2"]);
    const j = await cli("events", "bs_a1", "--json");
    expect(JSON.parse(j.out.join("\n")).events.map((e: { seq: number }) => e.seq)).toEqual([1, 2, 3, 4]);
  });

  it("recording", async () => {
    const path = join(dir, "rec.mp4");
    expect(await cli("recording", "bs_a1", "-o", path)).toMatchObject({ code: 0, out: [`saved ${path} (${RECORDING.length} bytes)`] });
    expect(readFileSync(path)).toEqual(RECORDING);
    api.recordingState = "processing";
    const r = await cli("recording", "bs_a1", "-o", path);
    expect(r.code).toBe(1);
    expect(r.err).toContain("The recording is still processing. (HTTP 409, NOT_READY)");
  });

  it("profile sync", async () => {
    const file = join(dir, "state.json");
    expect(await cli("profile", "sync", "acct-1", "--from-file", file, "--domain", "example.com")).toMatchObject({ code: 0, out: GOLDEN.profile });
    expect(api.requests("PUT").at(-1)!.body.mode).toBe("merge");
    await cli("profile", "sync", "acct-1", "--from-file", file, "--all-domains", "--replace");
    expect(api.requests("PUT").at(-1)!.body.mode).toBe("replace");
    expect(api.requests("PUT").at(-1)!.body.cookies).toHaveLength(3);
  });

  it("profile sync refuses to upload every cookie by accident", async () => {
    const file = join(dir, "state.json");
    const r = await cli("profile", "sync", "acct-1", "--from-file", file);
    expect([r.code, r.out]).toEqual([2, []]);
    expect(r.err).toContain("refusing to upload every cookie");
    expect((await cli("profile", "sync", "acct-1", "--domain", "a.com")).code).toBe(2);
    expect((await cli("profile", "sync", "acct-1", "--from-file", file, "--from-cdp", "http://x", "--domain", "a.com")).code).toBe(2);
    expect((await cli("profile", "sync", "acct-1", "--from-file", file, "--domain", "a.com", "--all-domains")).code).toBe(2);
    expect((await cli("profile")).code).toBe(2);
    expect(api.log).toEqual([]);
  });

  it("webhooks", async () => {
    expect(await cli("webhooks", "list")).toMatchObject({ code: 0, out: GOLDEN.webhooksList });
    expect(await cli("webhooks", "add", "https://hooks.example.com/x", "--event", "run.finished")).toMatchObject({ code: 0, out: GOLDEN.webhooksAdd });
    expect(await cli("webhooks", "rm", "wh_9")).toMatchObject({ code: 0, out: ["removed wh_9"] });
    expect(await cli("webhooks", "test", "wh_9")).toMatchObject({ code: 0, out: ["sent a ping to wh_9"] });
    expect((await cli("webhooks", "frob", "x")).code).toBe(2);
  });

  it("errors", async () => {
    expect((await cli("frobnicate")).code).toBe(2);
    process.env.CLEARCOTE_API_KEY = "wrong";
    const r = await cli("sessions");
    expect(r.code).toBe(1);
    expect(r.err).toContain("clearcote: Missing or invalid API key. (HTTP 401)");
    delete process.env.CLEARCOTE_API_KEY;
    const k = await cli("sessions");
    expect(k.code).toBe(1);
    expect(k.err).toContain("CLEARCOTE_API_KEY");
  });
});
