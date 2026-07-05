import { describe, it, expect } from "vitest";
import { PLATFORMS, RELEASE, REPO, SIGNING_KEY_FPR, platformRelease, type ReleaseInfo } from "../src/release.js";

/** Shared well-formed check for any per-platform pin. */
function checkPin(rel: ReleaseInfo): void {
  for (const k of ["tag", "version", "asset", "url", "sha256", "exeSha256", "size", "os", "archive", "binary", "assetGlob"] as const) {
    expect(rel[k], `pin.${k}`).toBeDefined();
    expect(rel[k], `pin.${k}`).not.toBe("");
  }
  expect(rel.sha256).toMatch(/^[0-9a-f]{64}$/);
  expect(rel.exeSha256).toMatch(/^[0-9a-f]{64}$/);
  expect(rel.size).toBeGreaterThan(0);
  expect(rel.asset).toContain(rel.version);
  expect(rel.asset).toContain(rel.assetGlob);
  expect(rel.asset.endsWith(".zip") || rel.asset.endsWith(".tar.xz")).toBe(true);
  expect(rel.url).toBe(`https://github.com/${REPO}/releases/download/${rel.tag}/${rel.asset}`);
  expect(rel.tag).toMatch(/^v\d+\.\d+\.\d+/);
  expect(rel.version).toMatch(/^\d+\.\d+\.\d+\.\d+$/);
}

describe("per-platform pins (static shape — the live drift check runs in CI)", () => {
  it("exposes exactly win32 + linux, both well-formed", () => {
    expect(new Set(Object.keys(PLATFORMS))).toEqual(new Set(["win32", "linux"]));
    for (const rel of Object.values(PLATFORMS)) checkPin(rel);
  });

  it("pins Windows as a win32 zip of chrome.exe", () => {
    const w = PLATFORMS.win32;
    expect(w.os).toBe("win32");
    expect(w.archive).toBe("zip");
    expect(w.binary).toBe("chrome.exe");
    expect(w.asset.endsWith("-windows-x64.zip")).toBe(true);
  });

  it("pins Linux as a linux tar.xz of chrome", () => {
    const ln = PLATFORMS.linux;
    expect(ln.os).toBe("linux");
    expect(ln.archive).toBe("tar.xz");
    expect(ln.binary).toBe("chrome");
    expect(ln.asset.endsWith("-linux-x64.tar.xz")).toBe(true);
  });

  it("selects the pin by OS (and RELEASE is the current platform's pin)", () => {
    expect(platformRelease("win32")).toBe(PLATFORMS.win32);
    expect(platformRelease("linux")).toBe(PLATFORMS.linux);
    expect(platformRelease("darwin")).toBeUndefined();
    // RELEASE is the current platform's pin (Windows fallback on an unsupported OS).
    expect(RELEASE).toBe(platformRelease(process.platform) ?? PLATFORMS.win32);
  });

  it("pins the signing-key fingerprint (40 hex, no spaces)", () => {
    expect(SIGNING_KEY_FPR).toMatch(/^[0-9A-F]{40}$/);
  });
});
