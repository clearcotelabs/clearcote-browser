// The built package (dist/) is what npm ships, and what the Python parity test runs as "the Node CLI". A
// dist built before a src change ships the old behaviour while every src test passes, so the behaviours
// that matter for security are checked against dist itself here. Skips when nothing is built yet
// (npm run build); a STALE build fails.
import { describe, it, expect } from "vitest";
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

const DIST = new URL("../dist/index.js", import.meta.url);
const CLI = fileURLToPath(new URL("../dist/clearcote-cli.js", import.meta.url));

describe.skipIf(!existsSync(DIST))("the built package (dist/) is current", () => {
  it("verifyWebhook refuses a missing secret and an oversized header", async () => {
    const d = await import(DIST.href);
    const header = `t=1790000000,v1=${"0".repeat(64)}`;
    for (const secret of [undefined, null, ""]) {
      expect(() => d.verifyWebhook("{}", header, secret, null)).toThrow(/signing secret/);
    }
    expect(() => d.verifyWebhook("{}", `${header},v1=${"0".repeat(5000)}`, "whsec_x", null)).toThrow(/invalid Clearcote-Signature/);
  });

  it("Cloud refuses plain http off this machine", async () => {
    const d = await import(DIST.href);
    expect(() => new d.Cloud({ apiKey: "k", baseUrl: "http://www.clearcotelabs.com" })).toThrow(/unencrypted/);
    expect(new d.Cloud({ apiKey: "k", baseUrl: "http://127.0.0.1:8480" }).baseUrl).toBe("http://127.0.0.1:8480");
  });

  it("filterCookies keeps the parent-domain cookies of a host", async () => {
    const d = await import(DIST.href);
    const jar = [".example.com", "www.example.com", ".com"].map((domain) => ({ name: "n", value: "v", domain }));
    expect(d.filterCookies(jar, ["www.example.com"]).map((c: { domain: string }) => c.domain)).toEqual([".example.com", "www.example.com"]);
  });

  it("the CLI never echoes a --secret argument and knows the run options", () => {
    const env = { ...process.env, CLEARCOTE_API_KEY: "cc_live_x", CLEARCOTE_API_URL: "http://127.0.0.1:9" };
    const r = spawnSync(process.execPath, [CLI, "cloud", "run", "t", "--secret", "hunter2"], { env, encoding: "utf8" });
    expect(r.status).toBe(2);
    expect(r.stderr).not.toContain("hunter2");
    const help = spawnSync(process.execPath, [CLI, "cloud", "--help"], { env, encoding: "utf8" });
    expect(help.stdout).toContain("--persist-profile");
  });
});
