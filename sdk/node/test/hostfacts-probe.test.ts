// The persona host-facts probe says why it got no answer, instead of "Unexpected end of JSON input" (CR-2VFYCD).
import { describe, expect, it } from "vitest";
import { describeProbe } from "../src/persona.js";

const REFUSAL = "[clearcote] no valid license lease for this run. Set a license key (CLEARCOTE_LICENSE_KEY) via the SDK and check your plan's concurrency limit.\r\n";

describe("describeProbe", () => {
  it("returns stdout of a clean run", () => {
    expect(describeProbe({ stdout: '{"gpu_vendor":"nvidia"}\n', stderr: "", status: 0 })).toContain("nvidia");
  });
  it("names the engine's refusal from stderr", () => {
    expect(() => describeProbe({ stdout: "", stderr: REFUSAL, status: 1 })).toThrow("it exited with code 1: no valid license lease for this run.");
  });
  it("names a binary that could not start", () => {
    const error = Object.assign(new Error("spawnSync /opt/x/chrome UNKNOWN"), { code: "UNKNOWN" });
    expect(() => describeProbe({ stdout: null, stderr: null, status: null, error })).toThrow("it could not be started: spawnSync /opt/x/chrome UNKNOWN");
  });
  it("names a timeout", () => {
    const error = Object.assign(new Error("spawnSync chrome.exe ETIMEDOUT"), { code: "ETIMEDOUT" });
    expect(() => describeProbe({ stdout: "", stderr: "", status: null, signal: "SIGTERM", error })).toThrow("did not answer within 30 s");
  });
  it("names a clean exit with no output", () => {
    expect(() => describeProbe({ stdout: " \n", stderr: "", status: 0 })).toThrow("it exited without output");
  });
});
