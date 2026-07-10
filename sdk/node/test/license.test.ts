// Free-vs-PRO selection: with a license key the SDK pulls the license-gated PRO binary via the
// site's authenticated route; with no key it is fully inert (free binary from GitHub, no backend
// call). These tests are hermetic — the only network is a mocked `fetch`.

import { describe, it, expect, vi, afterEach } from "vitest";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { resolveLicenseKey, resolveInstanceId } from "../src/license.js";
import { proEnsureBinary } from "../src/download.js";
import { executablePath } from "../src/index.js";

describe("resolveInstanceId (stable per-machine id: env > file > generated+persisted)", () => {
  const OLD = {
    id: process.env.CLEARCOTE_INSTANCE_ID,
    home: process.env.HOME,
    profile: process.env.USERPROFILE,
  };
  afterEach(() => {
    for (const [k, v] of [["CLEARCOTE_INSTANCE_ID", OLD.id], ["HOME", OLD.home], ["USERPROFILE", OLD.profile]] as const) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  });

  it("prefers CLEARCOTE_INSTANCE_ID and trims it", () => {
    process.env.CLEARCOTE_INSTANCE_ID = "  machine-7  ";
    expect(resolveInstanceId()).toBe("machine-7");
  });

  it("persists to ~/.clearcote/instance_id and is stable across calls (a restart reuses its slot)", () => {
    delete process.env.CLEARCOTE_INSTANCE_ID;
    const home = mkdtempSync(join(tmpdir(), "cc-home-"));
    process.env.HOME = home; // POSIX
    process.env.USERPROFILE = home; // Windows
    const a = resolveInstanceId();
    const b = resolveInstanceId();
    expect(a).toBe(b);
    expect(a.length).toBeGreaterThanOrEqual(8);
    expect(readFileSync(join(home, ".clearcote", "instance_id"), "utf8").trim()).toBe(a);
  });
});

describe("resolveLicenseKey (explicit > env > file)", () => {
  const OLD = process.env.CLEARCOTE_LICENSE_KEY;
  afterEach(() => {
    if (OLD === undefined) delete process.env.CLEARCOTE_LICENSE_KEY;
    else process.env.CLEARCOTE_LICENSE_KEY = OLD;
  });

  it("prefers an explicit key and trims surrounding whitespace", () => {
    process.env.CLEARCOTE_LICENSE_KEY = "cc_lic_from_env";
    expect(resolveLicenseKey("  cc_lic_explicit  ")).toBe("cc_lic_explicit");
  });

  it("falls back to CLEARCOTE_LICENSE_KEY when no (or a blank) explicit key is given", () => {
    process.env.CLEARCOTE_LICENSE_KEY = "cc_lic_from_env";
    expect(resolveLicenseKey()).toBe("cc_lic_from_env");
    expect(resolveLicenseKey("   ")).toBe("cc_lic_from_env"); // blank explicit is ignored
  });
});

describe("executablePath precedence (an explicit binary always wins over pro/free)", () => {
  it("returns an explicit executablePath with no download", async () => {
    expect(await executablePath({ executablePath: "/opt/custom/chrome" })).toBe("/opt/custom/chrome");
  });

  it("returns CLEARCOTE_BINARY before selecting the PRO or free binary", async () => {
    const OLD = process.env.CLEARCOTE_BINARY;
    process.env.CLEARCOTE_BINARY = "/opt/env/chrome";
    try {
      // Even WITH a pro selector present, the explicit env binary short-circuits (no fetch).
      expect(await executablePath({ pro: { licenseKey: "cc_lic_x" } })).toBe("/opt/env/chrome");
    } finally {
      if (OLD === undefined) delete process.env.CLEARCOTE_BINARY;
      else process.env.CLEARCOTE_BINARY = OLD;
    }
  });
});

describe("proEnsureBinary (license-gated download)", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it("surfaces an auth failure instead of silently falling back to the free binary", async () => {
    globalThis.fetch = vi.fn(async () => new Response('{"error":"Invalid license key."}', { status: 401 })) as unknown as typeof fetch;
    await expect(proEnsureBinary("cc_lic_bad", { quiet: true })).rejects.toThrow(/not authorized \(HTTP 401\)/);
  });

  it("throws when the server returns no download URL (no PRO build published)", async () => {
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ version: "149.0.0.0" }), { status: 200 })) as unknown as typeof fetch;
    await expect(proEnsureBinary("cc_lic_ok", { quiet: true })).rejects.toThrow(/not currently available/);
  });

  it("calls the authenticated /api/v1/download/pro route with the license as a Bearer token", async () => {
    const spy = vi.fn(async () => new Response(JSON.stringify({}), { status: 200 }));
    globalThis.fetch = spy as unknown as typeof fetch;
    // Empty JSON -> it will throw ("no download"), but the request was still made — assert on it.
    await expect(proEnsureBinary("cc_lic_probe", { apiBase: "https://example.test", quiet: true })).rejects.toThrow();
    const [url, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toMatch(/^https:\/\/example\.test\/api\/v1\/download\/pro\?platform=(windows|linux)$/);
    expect((init.headers as Record<string, string>).authorization).toBe("Bearer cc_lic_probe");
  });
});
