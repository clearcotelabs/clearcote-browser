// Server-side personas (r51): the launcher-side half, against a fake persona service.
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createHash } from "node:crypto";
import { createServer, type Server } from "node:http";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { gunzipSync, gzipSync } from "node:zlib";
import {
  API_ENV, PersonaServiceError, TOKEN_ENV, TOKEN_GZ_ENV, clearPersonaCaches, fetchPersonaToken, injectPersonaToken,
  preparePersonaLaunch, selectorFromArgs, staleHostVendor,
} from "../src/persona.js";

const R51 = (_exe: string | undefined, name: string) => name === "cc-persona-host-facts";
const OLD = () => false;

class FakeService {
  requests: Array<{ path: string; body: Record<string, unknown> }> = [];
  server: Server;
  base = "";
  constructor(public status = 200) {
    this.server = createServer((req, res) => {
      let data = "";
      req.on("data", (c) => { data += c; });
      req.on("end", () => {
        const body = JSON.parse(data);
        this.requests.push({ path: req.url ?? "", body });
        const out = this.status === 200
          ? { persona_token: "tok-" + createHash("sha256").update(JSON.stringify(body.sel)).digest("hex").slice(0, 12), exp: Date.now() / 1000 + 3600 }
          : { error: `refused ${this.status}` };
        res.writeHead(this.status, { "content-type": "application/json" });
        res.end(JSON.stringify(out));
      });
    });
  }
  listen(): Promise<void> {
    return new Promise((r) => this.server.listen(0, "127.0.0.1", () => {
      const a = this.server.address();
      this.base = `http://127.0.0.1:${typeof a === "object" && a ? a.port : 0}`;
      r();
    }));
  }
  close(): void { this.server.close(); }
}

describe("server-side personas (r51)", () => {
  let svc: FakeService;
  let dir: string;
  let exe: string;
  let runnerCalls: Array<{ cmd: string[]; env: Record<string, string | undefined> }>;
  const runner = (cmd: string[], env: Record<string, string | undefined>) => { runnerCalls.push({ cmd, env }); return 'ignored\n{"gpu_vendor":"nvidia"}\n'; };
  const lease = { token: "hdr.sig", leaseId: "L1" };

  beforeEach(async () => {
    svc = new FakeService();
    await svc.listen();
    process.env[API_ENV] = svc.base;
    dir = mkdtempSync(join(tmpdir(), "cc-persona-"));
    exe = join(dir, "chrome");
    writeFileSync(exe, "x");
    runnerCalls = [];
    clearPersonaCaches();
  });
  afterEach(() => {
    svc.close();
    delete process.env[API_ENV];
    rmSync(dir, { recursive: true, force: true });
    clearPersonaCaches();
  });
  const deps = () => ({ supports: R51, runner, env: { HOME: dir, LOCALAPPDATA: dir } });

  it("keeps only the generator's switches, last value wins, canvas-bridge-url is presence", () => {
    const { sel, profileJson, args } = selectorFromArgs(["--headless", "--fingerprint=1", "--fingerprint=2", "--lang=en-US",
      "--fingerprint-platform=windows", "--canvas-bridge-url=ws://x:1?auth=s", "--disable-gpu-string-spoof", "--user-data-dir=/tmp/u"]);
    expect(sel).toEqual({ fingerprint: "2", "fingerprint-platform": "windows", "canvas-bridge-url": "1", "disable-gpu-string-spoof": "" });
    expect(profileJson).toBeNull();
    expect(args.at(-1)).toBe("--user-data-dir=/tmp/u");
  });

  it("moves a captured profile off argv and names it by sha256", () => {
    const text = JSON.stringify({ screen: { width: 1920 } });
    const b64 = gzipSync(Buffer.from(text)).toString("base64");
    const { sel, profileJson, args } = selectorFromArgs(["--fingerprint=7", `--fingerprint-profile=${b64}`]);
    const sha = createHash("sha256").update(text).digest("hex");
    expect(profileJson).toBe(text);
    expect(sel["fingerprint-profile-sha256"]).toBe(sha);
    expect(args).toEqual(["--fingerprint=7", `--fingerprint-profile-sha256=${sha}`]);
  });

  it("leaves an old engine and a free launch alone", async () => {
    const a = await preparePersonaLaunch(exe, ["--fingerprint=1"], lease, undefined, { supports: OLD });
    expect(a.args).toEqual(["--fingerprint=1"]);
    expect(a.env({ A: "1" })).toEqual({ A: "1" });
    const b = await preparePersonaLaunch(exe, ["--fingerprint=1"], null, undefined, { supports: R51 });
    expect(b.env(undefined)).toBeUndefined();
    expect(svc.requests).toHaveLength(0);
  });

  it("fetches with the selector and host facts and injects the token per attempt", async () => {
    const p = await preparePersonaLaunch(exe, ["--headless", "--fingerprint=42", "--fingerprint-platform=linux"], lease, undefined, deps());
    expect(p.args).toEqual(["--headless", "--fingerprint=42", "--fingerprint-platform=linux"]);
    expect(p.env({ PATH: "/bin" })).toMatchObject({ PATH: "/bin", [TOKEN_ENV]: expect.stringMatching(/^tok-/) });
    expect(svc.requests[0]).toEqual({ path: "/v1/persona", body: { token: "hdr.sig", sel: { fingerprint: "42", "fingerprint-platform": "linux" }, host: { gpu_vendor: "nvidia" } } });
    expect(runnerCalls[0].cmd).toEqual([exe, "--cc-persona-host-facts", "--headless"]);
    expect(runnerCalls[0].env.CLEARCOTE_RUN_TOKEN).toBe("hdr.sig");
  });

  it("a caller's own CLEARCOTE_RUN_TOKEN counts as the lease (the hosted gateway path)", async () => {
    const payload = Buffer.from(JSON.stringify({ lic: "lic_g", lease: "gw-lease-7" })).toString("base64url");
    const env = { HOME: dir, LOCALAPPDATA: dir, CLEARCOTE_RUN_TOKEN: `${payload}.sig` };
    const p = await preparePersonaLaunch(exe, ["--fingerprint=9"], null, undefined, { supports: R51, runner, env });
    expect(p.env({})?.[TOKEN_ENV]).toMatch(/^tok-/);
    expect(svc.requests[0].body.token).toBe(`${payload}.sig`);
    expect(runnerCalls[0].env.CLEARCOTE_RUN_TOKEN).toBe(`${payload}.sig`);
  });

  it("fetches a default persona for a seedless launch", async () => {
    const p = await preparePersonaLaunch(exe, ["--fingerprint-platform=windows", "--window-size=1,1"], lease, undefined, deps());
    expect(svc.requests[0].body.sel).toEqual({ "fingerprint-platform": "windows" });
    expect(p.env({})?.[TOKEN_ENV]).toMatch(/^tok-/);
  });

  it("caches tokens per lease + selector and host facts per binary", async () => {
    for (let i = 0; i < 3; i++) await preparePersonaLaunch(exe, ["--fingerprint=5"], lease, undefined, deps());
    await preparePersonaLaunch(exe, ["--fingerprint=6"], lease, undefined, deps());
    await preparePersonaLaunch(exe, ["--fingerprint=5"], { token: "hdr.sig", leaseId: "L2" }, undefined, deps());
    expect(svc.requests).toHaveLength(3);
    expect(runnerCalls).toHaveLength(1);
  });

  it("uploads a profile as its exact text", async () => {
    const text = '{"screen":{"width":1366},"b":1.0}';
    const b64 = gzipSync(Buffer.from(text)).toString("base64");
    const p = await preparePersonaLaunch(exe, [`--fingerprint-profile=${b64}`], lease, undefined, deps());
    const sha = createHash("sha256").update(text).digest("hex");
    expect(svc.requests[0].body.profile_json).toBe(text);
    expect(svc.requests[0].body.sel).toEqual({ "fingerprint-profile-sha256": sha });
    expect(p.args).toEqual([`--fingerprint-profile-sha256=${sha}`]);
  });

  it("raises clear errors on refusal, on an unreachable service and on broken host facts", async () => {
    svc.status = 429;
    await expect(preparePersonaLaunch(exe, ["--fingerprint=1"], lease, undefined, deps())).rejects.toThrow(/refused this launch: refused 429/);
    await expect(fetchPersonaToken("t", { fingerprint: "1" }, { gpu_vendor: "none" }, null, undefined, ["http://127.0.0.1:9"], 500)).rejects.toThrow(PersonaServiceError);
  });

  it("raises when the engine does not report its host facts", async () => {
    await expect(preparePersonaLaunch(exe, ["--fingerprint=1"], lease, undefined, { ...deps(), runner: () => "not json" })).rejects.toThrow(/host facts/);
    expect(svc.requests).toHaveLength(0);
  });

  it("gzips a long token", () => {
    const env = injectPersonaToken({}, "a".repeat(40000));
    expect(env[TOKEN_ENV]).toBeUndefined();
    expect(gunzipSync(Buffer.from(env[TOKEN_GZ_ENV]!, "base64")).toString()).toBe("a".repeat(40000));
    expect(injectPersonaToken(env, "short")).toEqual({ [TOKEN_ENV]: "short" });
  });

  // --- stale host GPU vendor (CR-ZQJ4SX) ---
  const STALE = "BrowserType.launch: Target page, context or browser has been closed\n[pid=14076][err] [clearcote] no usable "
    + "persona for this run (generated for another host GPU vendor (none, this machine: nvidia)). This engine needs a "
    + "Clearcote SDK, Profile Manager or Helper that fetches personas (r51+).";

  it("does not keep host facts on disk: a saved answer from another time is not used", async () => {
    // Up to 0.70.0 a "none" saved before the NVIDIA driver (or in a Remote Desktop session) failed every launch.
    const old = join(dir, ".clearcote", "persona-host-facts.json");
    mkdirSync(join(dir, ".clearcote"), { recursive: true });
    const st = statSync(exe);
    const key = JSON.stringify([exe, st.size, Math.floor(st.mtimeMs / 1000), []]);
    writeFileSync(old, JSON.stringify({ [key]: { gpu_vendor: "none" } }));
    await preparePersonaLaunch(exe, ["--fingerprint=1"], lease, undefined, deps());
    expect(runnerCalls).toHaveLength(1);
    expect(svc.requests[0].body.host).toEqual({ gpu_vendor: "nvidia" });
    expect(JSON.parse(readFileSync(old, "utf8"))).toEqual({ [key]: { gpu_vendor: "none" } });
    expect(readdirSync(join(dir, ".clearcote"))).toEqual(["persona-host-facts.json"]);
  });

  it("reads the engine's stale-vendor refusal and nothing else", () => {
    expect(staleHostVendor(new Error(STALE))).toEqual(["none", "nvidia"]);
    expect(staleHostVendor("x (generated for another host GPU vendor (?, this machine: amd))")).toEqual(["?", "amd"]);
    expect(staleHostVendor(new Error("no usable persona for this run (not valid yet)"))).toBeNull();
    expect(staleHostVendor(undefined)).toBeNull();
  });

  it("heals with the vendor the engine named, without probing again", async () => {
    const probes: string[][] = [];
    const noneRunner = (cmd: string[]) => { probes.push(cmd); return '{"gpu_vendor":"none"}'; };
    const p = await preparePersonaLaunch(exe, ["--fingerprint=3", "--headless"], lease, undefined, { ...deps(), runner: noneRunner });
    expect(svc.requests[0].body.host).toEqual({ gpu_vendor: "none" });
    expect(await p.heal(new Error("Target page, context or browser has been closed"))).toBe(false);
    expect(svc.requests).toHaveLength(1);
    expect(await p.heal(new Error(STALE))).toBe(true);
    expect(svc.requests).toHaveLength(2);
    expect(svc.requests[1].body.host).toEqual({ gpu_vendor: "nvidia" });
    expect(p.env({ PATH: "/bin" })?.[TOKEN_ENV]).toMatch(/^tok-/);
    expect(probes).toHaveLength(1);
    // later launches in this process start from the corrected answer
    await preparePersonaLaunch(exe, ["--fingerprint=4", "--headless"], lease, undefined, { ...deps(), runner: noneRunner });
    expect(svc.requests[2].body.host).toEqual({ gpu_vendor: "nvidia" });
    expect(probes).toHaveLength(1);
  });

  it("retries a launch once on a stale host vendor, and only on that", async () => {
    const { retryOnStaleHostVendor } = await import("../src/index.js");
    const persona = { heal: async (e: unknown) => staleHostVendor(e) !== null };
    let n = 0;
    expect(await retryOnStaleHostVendor(persona, async () => { if (++n === 1) throw new Error(STALE); return "browser"; })).toBe("browser");
    expect(n).toBe(2);
    n = 0;
    await expect(retryOnStaleHostVendor(persona, async () => { n++; throw new Error(STALE); })).rejects.toThrow(/another host GPU vendor/);
    expect(n).toBe(2);
    n = 0;
    await expect(retryOnStaleHostVendor(persona, async () => { n++; throw new Error("boom"); })).rejects.toThrow("boom");
    expect(n).toBe(1);
  });
});
