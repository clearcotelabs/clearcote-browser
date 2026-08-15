// A cached browser tree can be complete at install time and damaged later (antivirus quarantine, a
// full disk, an interrupted copy out of a packaged app). Chromium does not report that usefully —
// it CHECK-crashes during startup ("Invalid file descriptor to ICU data received") before Playwright
// can attach — so the SDK detects it itself: self-heal for trees we installed, clear error for trees
// the caller supplied.

import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { MANIFEST, brokenInstallError, cachedBinary, checkInstall, verifyInstall, writeManifest } from "../src/download.js";

const BINARY = process.platform === "win32" ? "chrome.exe" : "chrome";
const PAYLOAD = ["chrome.dll", "chrome_elf.dll", "icudtl.dat", "snapshot_blob.bin", "resources.pak"];

let root: string;

/** A minimal but complete install base: <root>/browser + .verified + a manifest. */
function tree(): { base: string; browser: string } {
  const base = path.join(root, "v-test");
  const browser = path.join(base, "browser");
  mkdirSync(path.join(browser, "locales"), { recursive: true });
  for (const name of [BINARY, ...PAYLOAD]) writeFileSync(path.join(browser, name), Buffer.alloc(128, "x"));
  writeFileSync(path.join(browser, "locales", "en-US.pak"), Buffer.alloc(64, "x"));
  writeManifest(base, browser);
  writeFileSync(path.join(base, ".verified"), "deadbeef\n");
  return { base, browser };
}

beforeEach(() => {
  root = mkdtempSync(path.join(os.tmpdir(), "cc-integrity-"));
});
afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

describe("install integrity", () => {
  it("verifies a healthy tree and returns it from cache", () => {
    const { base, browser } = tree();
    expect(verifyInstall(browser, base, true)).toEqual([]);
    expect(cachedBinary(base, BINARY, true)).toBe(path.join(browser, BINARY));
  });

  it("records every file in the manifest", () => {
    const { base, browser } = tree();
    const files = JSON.parse(readFileSync(path.join(base, MANIFEST), "utf8")).files as Record<string, number>;
    expect(files["locales/en-US.pak"]).toBe(64); // nested entries use forward slashes
    expect(files["icudtl.dat"]).toBe(128);
    expect(browser).toContain("browser");
  });

  it("detects missing ICU data — the exact field failure", () => {
    const { base, browser } = tree();
    rmSync(path.join(browser, "icudtl.dat"));
    const problems = verifyInstall(browser, base, true);
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain("icudtl.dat");
    expect(problems[0]).toContain("missing");
  });

  it("detects a truncated payload file", () => {
    const { base, browser } = tree();
    writeFileSync(path.join(browser, "icudtl.dat"), Buffer.alloc(12, "x")); // a copy that stopped part-way
    const problems = verifyInstall(browser, base, true);
    expect(problems[0]).toContain("expected 128");
  });

  it("catches a non-critical file only the manifest knows about", () => {
    const { base, browser } = tree();
    rmSync(path.join(browser, "locales", "en-US.pak"));
    expect(verifyInstall(browser, base, true)).toEqual(["locales/en-US.pak — missing"]);
  });

  it("wipes a damaged cache so the caller re-downloads", () => {
    const { base, browser } = tree();
    rmSync(path.join(browser, "icudtl.dat"));
    expect(cachedBinary(base, BINARY, true)).toBeNull();
    expect(existsSync(browser)).toBe(false); // wiped, so the next resolve re-downloads
    expect(existsSync(path.join(base, ".verified"))).toBe(false); // and cannot short-circuit again
  });

  it("memoises the full scan per process but re-runs it on demand", () => {
    const { base, browser } = tree();
    expect(verifyInstall(browser, base)).toEqual([]);
    rmSync(path.join(browser, "locales", "en-US.pak"));
    expect(verifyInstall(browser, base)).toEqual([]); // memoised: manifest-only damage not re-checked
    expect(verifyInstall(browser, base, true)).toEqual(["locales/en-US.pak — missing"]);
  });

  it("rejects a damaged caller-supplied tree", () => {
    const { browser } = tree();
    rmSync(path.join(browser, "icudtl.dat"));
    expect(() => checkInstall(path.join(browser, BINARY))).toThrow(/incomplete or corrupted/);
  });

  it("accepts a healthy caller-supplied tree", () => {
    const { browser } = tree();
    expect(() => checkInstall(path.join(browser, BINARY))).not.toThrow();
  });

  it("ignores a non-flat Chromium layout (installed Google Chrome)", () => {
    const app = path.join(root, "Application");
    mkdirSync(path.join(app, "151.0.7922.108"), { recursive: true });
    writeFileSync(path.join(app, BINARY), "x");
    writeFileSync(path.join(app, "151.0.7922.108", "chrome.dll"), "x");
    expect(() => checkInstall(path.join(app, BINARY))).not.toThrow();
  });

  it("ignores a missing binary — the launcher reports that better", () => {
    expect(() => checkInstall(path.join(root, "nope", BINARY))).not.toThrow();
  });

  it("names the file and the fix in the error", () => {
    const { base, browser } = tree();
    const text = brokenInstallError(browser, ["icudtl.dat — missing"], true).message;
    expect(text).toContain("icudtl.dat");
    expect(text).toContain(base); // the folder to delete
    expect(text).toContain("antivirus");
  });

  it("does not promise a re-download for a caller-supplied tree", () => {
    const { browser } = tree();
    const text = brokenInstallError(browser, ["icudtl.dat — missing"], false).message;
    expect(text).not.toContain("re-download it");
    expect(text).toContain("CLEARCOTE_BINARY");
  });

  it("truncates a long problem list", () => {
    const { browser } = tree();
    const many = Array.from({ length: 20 }, (_, i) => `f${i}.pak — missing`);
    expect(brokenInstallError(browser, many).message).toContain("... and 12 more");
  });
});
