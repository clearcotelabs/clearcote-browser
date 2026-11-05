// CR-2VFYCD: a computer clock far off makes the engine refuse every token; the SDK says so before launching.
import { describe, expect, it } from "vitest";
import { ClockSkewError, assertClockFor, clockSkewProblem, formatDuration } from "../src/clockcheck.js";
import { LicenseError } from "../src/license.js";
import { preparePersonaLaunch } from "../src/persona.js";

const T = 1_800_000_000; // server time when the token was minted
const tok = (p: Record<string, unknown>) => `${Buffer.from(JSON.stringify(p)).toString("base64url")}.sig`;
const run = tok({ lic: "l", lease: "L1", plan: "pro", nbf: T - 900, iat: T, exp: T + 180 });

describe("clockSkewProblem", () => {
  it("is quiet for a clock inside the window (15 min behind .. until exp)", () => {
    expect(clockSkewProblem(run, T)).toBeNull();
    expect(clockSkewProblem(run, T - 900)).toBeNull();
    expect(clockSkewProblem(run, T + 179)).toBeNull();
  });

  it("names a clock that runs behind, with at least how far", () => {
    const p = clockSkewProblem(run, T - 1200)!;
    expect(p.skewSec).toBe(-1200);
    expect(p.message).toMatch(/clock is at least 20 min behind/);
    expect(p.message).toMatch(/it reads 2027-01-15 07:40:00 UTC; the server issued this licence token at 2027-01-15 08:00:00 UTC/);
    expect(p.message).toMatch(/Set time automatically/);
  });

  it("names a clock that runs ahead past exp", () => {
    const p = clockSkewProblem(run, T + 600, "persona token")!;
    expect(p.skewSec).toBe(600);
    expect(p.message).toMatch(/clock looks wrong: it reads .* after the moment this persona token expires/);
  });

  it("skips tokens it cannot read or that carry no times", () => {
    expect(clockSkewProblem("hdr.sig", T)).toBeNull();
    expect(clockSkewProblem(tok({ lic: "l" }), T)).toBeNull();
    expect(() => assertClockFor(undefined)).not.toThrow();
  });

  it("formats durations", () => {
    expect(formatDuration(42)).toBe("42 s");
    expect(formatDuration(192)).toBe("3 min 12 s");
    expect(formatDuration(7500)).toBe("2 h 5 min");
    expect(formatDuration(100800)).toBe("1 day 4 h");
  });
});

describe("preparePersonaLaunch", () => {
  it("refuses with ClockSkewError before running the engine when the clock is too far behind", async () => {
    let ran = 0;
    const err = await preparePersonaLaunch("chrome", ["--fingerprint=1"], { token: run, leaseId: "L1" }, undefined, {
      supports: () => true, runner: () => { ran++; return '{"gpu_vendor":"none"}'; }, fetch: async () => { throw new Error("no fetch"); },
      now: () => T - 3600,
    }).catch((e) => e);
    expect(err).toBeInstanceOf(ClockSkewError);
    expect(err).toBeInstanceOf(LicenseError);
    expect(err.code).toBe("CLOCK_SKEW");
    expect(err.message).toMatch(/at least 1 h behind/);
    expect(ran).toBe(0);
  });
});
