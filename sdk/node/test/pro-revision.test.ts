// PRO revision pinning ("r7" / "150.0.7871.114-r7"): the selector must be recognised, routed to
// the authenticated PRO download verbatim, and rejected without a license — all BEFORE the public
// version catalog is consulted (revisions aren't in it). Hermetic: fetch is mocked, no real network.

import { describe, it, expect, vi, afterEach } from "vitest";
import { isProRevisionSelector, resolvedEngineVersion, ensureVersion } from "../src/download.js";
import { RELEASE } from "../src/release.js";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  vi.restoreAllMocks();
});

describe("isProRevisionSelector", () => {
  it("detects revision selectors, case-insensitively, with or without a version prefix", () => {
    for (const s of ["r7", "r3", "r99", "R7", "150.0.7871.114-r7", "150.0.7871.114-R9", "  r7  "]) {
      expect(isProRevisionSelector(s), s).toBe(true);
    }
  });
  it("rejects plain versions, majors, latest, and near-misses", () => {
    for (const s of ["150", "150.0.7871.114", "latest", "", undefined, "r", "r7x", "149.0.7827.114"]) {
      expect(isProRevisionSelector(s as string), String(s)).toBe(false);
    }
  });
});

describe("resolvedEngineVersion (telemetry, no network for revisions)", () => {
  it("maps a version-qualified revision to its version and a bare revision to the baseline", async () => {
    expect(await resolvedEngineVersion("150.0.7871.114-r7", true)).toBe("150.0.7871.114");
    expect(await resolvedEngineVersion("r7", true)).toBe(String(RELEASE.version));
  });
});

describe("ensureVersion routing", () => {
  it("throws a PRO-revision error without a license — before any network", async () => {
    const spy = vi.fn();
    globalThis.fetch = spy as unknown as typeof fetch;
    await expect(ensureVersion("150.0.7871.114-r7", {})).rejects.toThrow(/PRO revision/);
    expect(spy).not.toHaveBeenCalled(); // catalog was never even fetched
  });

  it("routes a licensed revision straight to /download/pro with the selector verbatim", async () => {
    let seen = "";
    globalThis.fetch = vi.fn(async (url: string | URL | Request) => {
      seen = String(url);
      // Fail after capture so we never attempt a real blob download.
      return new Response("nope", { status: 503 });
    }) as unknown as typeof fetch;

    await expect(
      ensureVersion("150.0.7871.114-r7", { licenseKey: "cc_lic_fake" }),
    ).rejects.toThrow(); // 503 -> "not authorized"; the routing already happened
    expect(seen).toContain("/api/v1/download/pro");
    expect(decodeURIComponent(seen)).toContain("version=150.0.7871.114-r7");
  });
});
