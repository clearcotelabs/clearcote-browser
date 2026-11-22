import { describe, it, expect, afterEach, vi } from "vitest";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { resolveNoisePerProfile, readNoiseMode, hasChromeData, PROFILE_STATE_FILE } from "../src/noisemode.js";

const dirs: string[] = [];
const fresh = () => { const d = mkdtempSync(join(tmpdir(), "cc-noise-")); dirs.push(d); return d; };
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }); vi.restoreAllMocks(); });
const r60 = { supports: () => true, quiet: true };
const old = { supports: () => false, quiet: true };
const state = (d: string) => JSON.parse(readFileSync(join(d, PROFILE_STATE_FILE), "utf8"));
const chromeProfile = () => { const d = fresh(); writeFileSync(join(d, "Local State"), "{}"); mkdirSync(join(d, "Default")); return d; };

describe("resolveNoisePerProfile", () => {
  it("a new empty folder gets one noise per profile on r60+ and remembers it", () => {
    const d = fresh();
    expect(resolveNoisePerProfile(undefined, d, r60)).toBe(true);
    expect(state(d)).toEqual({ noise: "per-profile" });
  });

  it("a folder that does not exist yet is created and remembered, so its first run does not make it look old", () => {
    const d = join(fresh(), "new-profile");
    expect(resolveNoisePerProfile(undefined, d, r60)).toBe(true);
    expect(readNoiseMode(d)).toBe("per-profile");
    writeFileSync(join(d, "Local State"), "{}");  // the browser's first run
    expect(resolveNoisePerProfile(undefined, d, r60)).toBe(true);
  });

  it("a folder made before this release (Chrome data, nothing remembered) keeps per-site noise, remembered", () => {
    const d = chromeProfile();
    expect(resolveNoisePerProfile(undefined, d, r60)).toBe(false);
    expect(state(d)).toEqual({ noise: "per-site" });
    expect(resolveNoisePerProfile(undefined, d, r60)).toBe(false);
  });

  it("only 'Default' or only 'Local State' already counts as Chrome data", () => {
    const a = fresh(); mkdirSync(join(a, "Default"));
    const b = fresh(); writeFileSync(join(b, "Local State"), "{}");
    expect(hasChromeData(a)).toBe(true);
    expect(hasChromeData(b)).toBe(true);
    expect(hasChromeData(fresh())).toBe(false);
    expect(hasChromeData(join(fresh(), "missing"))).toBe(false);
  });

  it("an engine before r60 gives a new folder per-site noise, remembered, so an upgrade does not change it later", () => {
    const d = fresh();
    expect(resolveNoisePerProfile(undefined, d, old)).toBe(false);
    expect(readNoiseMode(d)).toBe("per-site");
    expect(resolveNoisePerProfile(undefined, d, r60)).toBe(false);
  });

  it("an explicit value wins and becomes the folder's mode", () => {
    const legacy = chromeProfile();
    expect(resolveNoisePerProfile(true, legacy, r60)).toBe(true);
    expect(resolveNoisePerProfile(undefined, legacy, r60)).toBe(true);
    const d = fresh();
    expect(resolveNoisePerProfile(false, d, r60)).toBe(false);
    expect(resolveNoisePerProfile(undefined, d, r60)).toBe(false);
  });

  it("a launch without a folder (incognito) is new: per profile on r60+, nothing written", () => {
    expect(resolveNoisePerProfile(undefined, null, r60)).toBe(true);
    expect(resolveNoisePerProfile(undefined, null, old)).toBe(false);
    expect(resolveNoisePerProfile(false, null, r60)).toBe(false);
  });

  it("pass-through is left alone: nothing decided, nothing written", () => {
    const d = fresh();
    expect(resolveNoisePerProfile(undefined, d, { ...r60, passthrough: true })).toBeUndefined();
    expect(existsSync(join(d, PROFILE_STATE_FILE))).toBe(false);
  });

  it("keeps other keys in clearcote.json and ignores a file it cannot read", () => {
    const d = fresh();
    writeFileSync(join(d, PROFILE_STATE_FILE), JSON.stringify({ other: 1 }));
    expect(resolveNoisePerProfile(undefined, d, r60)).toBe(true);
    expect(state(d)).toEqual({ other: 1, noise: "per-profile" });
    const bad = chromeProfile();
    writeFileSync(join(bad, PROFILE_STATE_FILE), "not json");
    expect(resolveNoisePerProfile(undefined, bad, r60)).toBe(false);
    expect(state(bad)).toEqual({ noise: "per-site" });
  });

  it("an unwritable folder never fails the launch and says so unless quiet", () => {
    const d = fresh();
    mkdirSync(join(d, PROFILE_STATE_FILE));  // a directory where the file should go: the write throws
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    expect(resolveNoisePerProfile(undefined, d, { supports: () => true })).toBe(true);
    expect(String(err.mock.calls[0]?.[0])).toMatch(/could not remember the noise mode/);
    err.mockClear();
    expect(resolveNoisePerProfile(undefined, d, r60)).toBe(true);
    expect(err).not.toHaveBeenCalled();
  });
});
