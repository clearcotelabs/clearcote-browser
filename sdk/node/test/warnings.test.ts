import { describe, it, expect } from "vitest";
import { coherenceWarnings } from "../src/warnings.js";

const codes = (o: Record<string, unknown>, host = "win32", build = "149") =>
  new Set(coherenceWarnings(o, host, build).map((w) => w.code));

describe("coherenceWarnings", () => {
  it("is silent for a coherent default persona", () => {
    expect(coherenceWarnings({ platform: "windows", fingerprint: "s", headless: false }, "win32", "149")).toEqual([]);
  });

  it("flags a proxy without geo coherence, silent with geoip or manual geo", () => {
    expect(codes({ proxy: { server: "http://h:8080" }, headless: false }).has("proxy-no-geo")).toBe(true);
    expect(codes({ proxy: { server: "http://h:8080" }, geoip: true, headless: false }).has("proxy-no-geo")).toBe(false);
    expect(codes({ proxy: "http://h:8080", timezone: "America/New_York", acceptLanguage: "en-US,en", headless: false }).has("proxy-no-geo")).toBe(false);
  });

  it("flags SOCKS + geoip (can't resolve)", () => {
    expect(codes({ proxy: "socks5://u:p@h:1", geoip: true, headless: false }).has("socks-geoip")).toBe(true);
    expect(codes({ proxy: "http://h:1", geoip: true, headless: false }).has("socks-geoip")).toBe(false);
  });

  it("flags platform spoofed away from the host without a profile", () => {
    expect(codes({ platform: "macos", headless: false }).has("platform-host-fonts")).toBe(true);
    expect(codes({ platform: "windows", headless: false }).has("platform-host-fonts")).toBe(false);
    expect(codes({ platform: "macos", fingerprintProfile: "p.json", headless: false }).has("platform-host-fonts")).toBe(false);
  });

  it("flags GPU string incoherent with platform, and software renderers", () => {
    expect(codes({ platform: "macos", gpuRenderer: "ANGLE (Apple, Direct3D11)", headless: false }, "darwin").has("gpu-platform")).toBe(true);
    expect(codes({ gpuRenderer: "ANGLE (Google, Vulkan SwiftShader Device)", headless: false }).has("gpu-software")).toBe(true);
  });

  it("flags non-Chrome brand and a version that differs from the build", () => {
    expect(codes({ brand: "Edge", headless: false }).has("brand-mismatch")).toBe(true);
    expect(codes({ brand: "Chrome", headless: false }).has("brand-mismatch")).toBe(false);
    expect(codes({ brandVersion: "146", headless: false }).has("version-mismatch")).toBe(true);
    expect(codes({ brandVersion: "149.0.1", headless: false }).has("version-mismatch")).toBe(false);
  });

  it("flags disableGpuFingerprint without fingerprintNoise:false", () => {
    expect(codes({ disableGpuFingerprint: true, headless: false }).has("gpu-noise")).toBe(true);
    expect(codes({ disableGpuFingerprint: true, fingerprintNoise: false, headless: false }).has("gpu-noise")).toBe(false);
  });

  it("notes headless render coherence + unpinned bridge GPU", () => {
    expect(codes({ headless: true }).has("headless-render")).toBe(true);
    expect(codes({ headless: false }).has("headless-render")).toBe(false);
    expect(codes({ headless: true, canvasBridge: { url: "ws://h" } }).has("headless-render")).toBe(false);
    expect(codes({ canvasBridge: { url: "ws://h" }, headless: false }).has("bridge-no-gpu")).toBe(true);
    expect(codes({ canvasBridge: { url: "ws://h" }, gpuRenderer: "ANGLE (Intel)", headless: false }).has("bridge-no-gpu")).toBe(false);
  });

  it("flags re-added automation args", () => {
    expect(codes({ _userArgs: ["--enable-automation"], headless: false }).has("automation-arg")).toBe(true);
    expect(codes({ _userArgs: ["--remote-debugging-port=9222"], headless: false }).has("automation-arg")).toBe(true);
    expect(codes({ _userArgs: ["--no-sandbox"], headless: false }).has("automation-arg")).toBe(false);
  });
});
