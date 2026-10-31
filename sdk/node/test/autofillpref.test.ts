// src/autofillpref.ts — Chrome's autofill defaults go into a profile's Preferences only where missing.
// Mirrors the Profile Manager's tests/autofillpref.test.ts, sdk/python/tests/test_autofillpref.py and
// sdk/dotnet/tests/Clearcote.Tests/AutofillPrefsTests.cs.
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { applyAutofillPrefs, seedAutofillPrefs } from "../src/autofillpref.js";

let dir: string;
const file = () => join(dir, "Default", "Preferences");
const prefs = () => JSON.parse(readFileSync(file(), "utf8"));
const write = (v: unknown) => {
  mkdirSync(join(dir, "Default"), { recursive: true });
  writeFileSync(file(), typeof v === "string" ? v : JSON.stringify(v));
};
beforeEach(() => { dir = mkdtempSync(join(tmpdir(), "cc-autofill-")); });
afterEach(() => rmSync(dir, { recursive: true, force: true }));

describe("seedAutofillPrefs", () => {
  it("a new profile gets both on (Default/ and the file are created)", () => {
    const udd = join(dir, "not-yet");
    expect(seedAutofillPrefs(udd)).toEqual(["profile_enabled", "credit_card_enabled"]);
    expect(JSON.parse(readFileSync(join(udd, "Default", "Preferences"), "utf8"))).toEqual(
      { autofill: { profile_enabled: true, credit_card_enabled: true } });
  });

  it("keeps an explicit off and every other preference", () => {
    write({ autofill: { profile_enabled: false, other: 1 }, browser: { x: 2 } });
    expect(seedAutofillPrefs(dir)).toEqual(["credit_card_enabled"]);
    expect(prefs()).toEqual({ autofill: { profile_enabled: false, other: 1, credit_card_enabled: true }, browser: { x: 2 } });
  });

  it("does nothing when both are set: the file is not rewritten", () => {
    const text = JSON.stringify({ autofill: { profile_enabled: false, credit_card_enabled: false } }, null, 1);
    write(text);
    expect(seedAutofillPrefs(dir)).toEqual([]);
    expect(readFileSync(file(), "utf8")).toBe(text);
  });

  it("never replaces a file it cannot read", () => {
    for (const bad of ["{not json", "[1,2]", "null", ""]) {
      write(bad);
      expect(seedAutofillPrefs(dir)).toEqual([]);
      expect(readFileSync(file(), "utf8")).toBe(bad);
    }
  });

  it("applyAutofillPrefs never throws (a profile path that is a file)", () => {
    const notADir = join(dir, "file");
    writeFileSync(notADir, "x");
    expect(() => applyAutofillPrefs(notADir, true)).not.toThrow();
    expect(existsSync(join(notADir, "Default"))).toBe(false);
  });
});
