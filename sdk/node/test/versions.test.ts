// Version selector: launch({version:"150"}) resolves against the public catalog, VALIDATING that the
// build exists (and is reachable for its tier) BEFORE downloading — so a bad request fails fast with a
// helpful message instead of getting stuck. Hermetic: the catalog fetch is mocked, no real network.

import { describe, it, expect, vi, afterEach } from "vitest";
import { resolveVersion } from "../src/download.js";
import { CATALOG_FALLBACK } from "../src/release.js";
import { executablePath } from "../src/index.js";

const CATALOG = {
  schema: 1,
  builds: [
    {
      major: 149, version: "149.0.7827.114", tier: "free", tag: "v0.1.0-pre.22",
      platforms: {
        windows: { asset: "cc-149-win.zip", url: "https://x/cc-149-win.zip", sha256: "a".repeat(64), archive: "zip", binary: "chrome.exe" },
        linux: { asset: "cc-149-linux.tar.xz", url: "https://x/cc-149-linux.tar.xz", sha256: "b".repeat(64), archive: "tar.xz", binary: "chrome" },
      },
    },
    {
      major: 150, version: "150.0.7871.115", tier: "pro", tag: "pro-150.0.7871.115",
      platforms: { windows: { archive: "zip", binary: "chrome.exe" }, linux: { archive: "tar.xz", binary: "chrome" } },
    },
  ],
};

const realFetch = globalThis.fetch;
function mockCatalog() {
  globalThis.fetch = vi.fn(async () => new Response(JSON.stringify(CATALOG), { status: 200 })) as unknown as typeof fetch;
}

describe("resolveVersion (validate-first, tier-aware)", () => {
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it("resolves a FREE major without a license", async () => {
    mockCatalog();
    const plan = await resolveVersion("149", false);
    expect(plan.kind).toBe("free");
    if (plan.kind === "free") {
      expect(plan.rel.version).toBe("149.0.7827.114");
      expect(plan.rel.url).toBeTruthy();
      expect(plan.rel.sha256).toBeTruthy();
    }
  });

  it("resolves an exact free version", async () => {
    mockCatalog();
    const plan = await resolveVersion("149.0.7827.114", false);
    expect(plan.kind === "free" && plan.rel.version).toBe("149.0.7827.114");
  });

  it("errors clearly when a PRO version is requested without a license (no silent downgrade)", async () => {
    mockCatalog();
    await expect(resolveVersion("150", false)).rejects.toThrow(/PRO build.*license/s);
  });

  it("routes a PRO version to the licensed route when a license is present", async () => {
    mockCatalog();
    const plan = await resolveVersion("150", true);
    expect(plan).toEqual({ kind: "pro", version: "150.0.7871.115" });
  });

  it("lists what's available when the version doesn't exist", async () => {
    mockCatalog();
    await expect(resolveVersion("151", true)).rejects.toThrow(/No Clearcote build matches version '151'.*Available/s);
  });

  it("'latest' resolves to the newest ACCESSIBLE build", async () => {
    mockCatalog();
    const noLic = await resolveVersion("latest", false); // newest free = 149
    expect(noLic.kind === "free" && noLic.rel.version).toBe("149.0.7827.114");
    const lic = await resolveVersion("latest", true); // newest overall = pro 150
    expect(lic).toEqual({ kind: "pro", version: "150.0.7871.115" });
  });

  it("bundled fallback lists only downloadable builds (149 free; 150 not advertised until live)", () => {
    const byVer = Object.fromEntries(CATALOG_FALLBACK.builds.map((b) => [b.version, b]));
    expect(byVer["149.0.7827.114"].tier).toBe("free");
    expect(byVer["149.0.7827.114"].platforms.linux?.url).toBeTruthy();
    // 150 PRO is NOT advertised until its binary is live (else a licensed version="150" would 404).
    expect(byVer["150.0.7871.115"]).toBeUndefined();
    // every listed build must actually be downloadable (have a url per platform).
    for (const b of CATALOG_FALLBACK.builds) {
      for (const p of Object.values(b.platforms)) expect(p?.url).toBeTruthy();
    }
  });
});

describe("backwards compatibility: version must not change existing precedence", () => {
  it("an explicit executablePath wins over a version selector", async () => {
    expect(await executablePath({ executablePath: "/opt/x/chrome", version: "150" })).toBe("/opt/x/chrome");
  });

  it("CLEARCOTE_BINARY wins over a version selector (legacy env path unaffected)", async () => {
    const OLD = process.env.CLEARCOTE_BINARY;
    process.env.CLEARCOTE_BINARY = "/opt/env/chrome";
    try {
      expect(await executablePath({ version: "150" })).toBe("/opt/env/chrome");
    } finally {
      if (OLD === undefined) delete process.env.CLEARCOTE_BINARY;
      else process.env.CLEARCOTE_BINARY = OLD;
    }
  });
});
