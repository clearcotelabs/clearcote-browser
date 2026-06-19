import { describe, it, expect } from "vitest";
import { RELEASE, REPO, SIGNING_KEY_FPR } from "../src/release.js";

describe("RELEASE pin (static shape — the live drift check runs in CI)", () => {
  it("has a complete, well-formed shape", () => {
    for (const k of ["tag", "version", "asset", "url", "sha256", "exeSha256", "size", "os"] as const) {
      expect(RELEASE[k], `RELEASE.${k}`).toBeDefined();
      expect(RELEASE[k], `RELEASE.${k}`).not.toBe("");
    }
    expect(RELEASE.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(RELEASE.exeSha256).toMatch(/^[0-9a-f]{64}$/);
    expect(RELEASE.size).toBeGreaterThan(0);
    expect(RELEASE.os).toBe("win32");
  });

  it("is internally consistent (url ↔ repo ↔ tag ↔ asset ↔ version)", () => {
    expect(RELEASE.asset).toContain(RELEASE.version);
    expect(RELEASE.asset.endsWith(".zip")).toBe(true);
    expect(RELEASE.url).toBe(
      `https://github.com/${REPO}/releases/download/${RELEASE.tag}/${RELEASE.asset}`,
    );
    expect(RELEASE.tag).toMatch(/^v\d+\.\d+\.\d+/);
    expect(RELEASE.version).toMatch(/^\d+\.\d+\.\d+\.\d+$/);
  });

  it("pins the signing-key fingerprint (40 hex, no spaces)", () => {
    expect(SIGNING_KEY_FPR).toMatch(/^[0-9A-F]{40}$/);
  });
});
