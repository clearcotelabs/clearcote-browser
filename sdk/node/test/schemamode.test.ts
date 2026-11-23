import { describe, it, expect, afterEach, vi } from "vitest";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { resolvePersonaSchema, readSchema, schemaInArgs } from "../src/schemamode.js";
import { resolveNoisePerProfile, PROFILE_STATE_FILE } from "../src/noisemode.js";

const dirs: string[] = [];
const fresh = () => { const d = mkdtempSync(join(tmpdir(), "cc-schema-")); dirs.push(d); return d; };
afterEach(() => { for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true }); vi.restoreAllMocks(); });
const r51 = { supports: () => true, quiet: true };
const old = { supports: () => false, quiet: true };
const state = (d: string) => JSON.parse(readFileSync(join(d, PROFILE_STATE_FILE), "utf8"));
const chromeProfile = (d = fresh()) => { writeFileSync(join(d, "Local State"), "{}"); mkdirSync(join(d, "Default")); return d; };

describe("resolvePersonaSchema", () => {
  it("a new empty folder gets schema 8 on an r51+ engine and keeps it after the browser's first run", () => {
    const d = fresh();
    expect(resolvePersonaSchema(undefined, d, r51)).toBe(8);
    expect(state(d)).toEqual({ schema: "8" });
    chromeProfile(d);
    expect(resolvePersonaSchema(undefined, d, r51)).toBe(8);
  });

  it("a folder that does not exist yet is created and remembered", () => {
    const d = join(fresh(), "new-profile");
    expect(resolvePersonaSchema(undefined, d, r51)).toBe(8);
    expect(readSchema(d)).toBe("8");
  });

  it("a folder made before this release keeps 7 and adds no switch", () => {
    const d = chromeProfile();
    expect(resolvePersonaSchema(undefined, d, r51)).toBeUndefined();
    expect(state(d)).toEqual({ schema: "7" });
    expect(resolvePersonaSchema(undefined, d, r51)).toBeUndefined();
  });

  it("an engine without the persona service gets 7, remembered", () => {
    const d = fresh();
    expect(resolvePersonaSchema(undefined, d, old)).toBeUndefined();
    expect(readSchema(d)).toBe("7");
    expect(resolvePersonaSchema(undefined, d, r51)).toBeUndefined();
  });

  it("an explicit value wins, is returned as given and becomes the folder's schema", () => {
    const d = chromeProfile();
    expect(resolvePersonaSchema(8, d, r51)).toBe(8);
    expect(readSchema(d)).toBe("8");
    expect(resolvePersonaSchema(undefined, d, r51)).toBe(8);
    expect(resolvePersonaSchema(5, d, r51)).toBe(5);
    expect(readSchema(d)).toBe("5");
  });

  it("a schema pinned in args wins, adds nothing and is remembered", () => {
    const d = fresh();
    expect(resolvePersonaSchema(undefined, d, { ...r51, args: ["--x", "--fingerprint-schema=4"] })).toBeUndefined();
    expect(readSchema(d)).toBe("4");
    expect(schemaInArgs(["--fingerprint-schema=5", "--fingerprint-schema=7"])).toBe("7");
    expect(schemaInArgs(["--fingerprint-schema=bad"])).toBeNull();
  });

  it("no folder is new and writes nothing; pass-through is left alone", () => {
    expect(resolvePersonaSchema(undefined, null, r51)).toBe(8);
    expect(resolvePersonaSchema(undefined, null, old)).toBeUndefined();
    const d = fresh();
    expect(resolvePersonaSchema(undefined, d, { ...r51, passthrough: true })).toBeUndefined();
    expect(existsSync(join(d, PROFILE_STATE_FILE))).toBe(false);
  });

  it("shares clearcote.json with the noise mode and ignores a bad value", () => {
    const d = fresh();
    resolveNoisePerProfile(undefined, d, { supports: () => true, quiet: true });
    resolvePersonaSchema(undefined, d, r51);
    expect(state(d)).toEqual({ noise: "per-profile", schema: "8" });
    writeFileSync(join(d, PROFILE_STATE_FILE), JSON.stringify({ noise: "per-site", schema: "x" }));
    chromeProfile(d);
    expect(resolvePersonaSchema(undefined, d, r51)).toBeUndefined();
    expect(state(d)).toEqual({ noise: "per-site", schema: "7" });
  });

  it("an unwritable folder never fails and says so unless quiet", () => {
    const d = fresh();
    mkdirSync(join(d, PROFILE_STATE_FILE));  // a directory where the file should go
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    expect(resolvePersonaSchema(undefined, d, { supports: () => true })).toBe(8);
    expect(String(err.mock.calls[0]?.[0])).toContain("could not remember the persona schema");
    err.mockClear();
    expect(resolvePersonaSchema(undefined, d, r51)).toBe(8);
    expect(err).not.toHaveBeenCalled();
  });
});

