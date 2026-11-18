import { describe, it, expect, vi } from "vitest";
import { coherenceWarnings, emitCoherenceWarnings } from "../src/warnings.js";

const codes = (o: Record<string, unknown>, host = "win32", build = "149") =>
  new Set(coherenceWarnings(o, host, build).map((w) => w.code));

describe("coherenceWarnings", () => {
  it("is silent for a coherent default persona", () => {
    expect(coherenceWarnings({ platform: "windows", fingerprint: "s", headless: false }, "win32", "149")).toEqual([]);
  });

  it("flags a proxy without geo coherence, silent with geoip or manual geo", () => {
    expect(codes({ proxy: { server: "http://h:8080" }, headless: false }).has("proxy-no-geo")).toBe(true);
    expect(codes({ proxy: { server: "http://h:8080" }, geoip: true, headless: false }).has("proxy-no-geo")).toBe(false);
    // pass-through keeps the host's timezone and language behind a proxy: the warning says so, and how to change it
    const off = coherenceWarnings({ proxy: { server: "http://h:8080" }, fingerprint: "off", headless: false }, "linux").find((w) => w.code === "proxy-no-geo");
    expect(off?.message).toContain("pass-through");
    expect(off?.message).toContain("geoip:true");
    expect(off?.message).not.toContain("without geoip");
    expect(codes({ proxy: { server: "http://h:8080" }, fingerprint: "off", geoip: true, headless: false }).has("proxy-no-geo")).toBe(false);
    expect(codes({ proxy: "http://h:8080", timezone: "America/New_York", acceptLanguage: "en-US,en", headless: false }).has("proxy-no-geo")).toBe(false);
  });

  it("does not flag SOCKS + geoip: geoip resolves through socks5 (with auth) since 0.29.0", () => {
    // This warned "geoip cannot resolve a SOCKS proxy's exit IP" on every SOCKS launch, long after
    // the lookup learned to tunnel through SOCKS5.
    expect(codes({ proxy: "socks5://u:p@h:1", geoip: true, headless: false }).has("socks-geoip")).toBe(false);
    expect(codes({ proxy: { server: "socks5://h:1" }, geoip: true, headless: false })).toEqual(new Set());
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

  it("emits engine-behaviour notes once per process from the emitter, never from coherenceWarnings", () => {
    // The coherent default is asserted silent above, so the console-events note must come from
    // emitCoherenceWarnings() only, and fire a single time across repeated launches.
    const writes: string[] = [];
    const spy = vi.spyOn(process.stderr, "write").mockImplementation(((chunk: unknown) => {
      writes.push(String(chunk));
      return true;
    }) as never);
    const prev = process.env.CLEARCOTE_NO_WARN;
    delete process.env.CLEARCOTE_NO_WARN;
    try {
      const opts = { platform: "windows", fingerprint: "s", headless: false };
      emitCoherenceWarnings(opts, false, "win32", "149");
      emitCoherenceWarnings(opts, false, "win32", "149");
      expect(writes.filter((w) => w.includes("page.on('console')")).length).toBe(1);
    } finally {
      spy.mockRestore();
      if (prev !== undefined) process.env.CLEARCOTE_NO_WARN = prev;
    }
  });
});
