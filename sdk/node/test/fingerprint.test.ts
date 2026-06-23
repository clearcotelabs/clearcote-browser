import { describe, it, expect } from "vitest";
import { gunzipSync } from "node:zlib";
import {
  fingerprintArgs,
  cleanAcceptLanguage,
  splitFingerprintOptions,
  encodeProfile,
} from "../src/fingerprint.js";

describe("fingerprintArgs", () => {
  it("defaults the persona to Windows + Chrome and emits a coherent Accept-Language + UI locale", () => {
    // clearcote presents as Windows + Google Chrome; the defaults keep navigator.platform and the
    // UA-CH brand coherent (no seed-derived OS drift, no Chromium/Chrome UA-CH mismatch).
    // A coherent Accept-Language is also always emitted (defaults to en-US,en) so the language
    // never falls back to the build/OS locale and mismatches the proxy geo. --lang pins the UI/ICU
    // locale to the primary tag so Intl (main thread + workers) matches navigator.language.
    expect(fingerprintArgs({})).toEqual([
      "--fingerprint-platform=windows",
      "--fingerprint-brand=chrome",
      "--accept-lang=en-US,en",
      "--lang=en-US",
    ]);
  });

  it("derives --lang from the primary Accept-Language tag (Intl/locale coherence)", () => {
    expect(fingerprintArgs({ acceptLanguage: "fr-FR,fr" })).toContain("--lang=fr-FR");
    expect(fingerprintArgs({ acceptLanguage: "de-DE,de;q=0.7,en;q=0.3" })).toContain("--lang=de-DE");
  });

  it("maps every fingerprint option to its Chromium switch", () => {
    const args = fingerprintArgs({
      fingerprint: "seed-1",
      platform: "windows",
      platformVersion: "10.0.0",
      brand: "Edge",
      brandVersion: "149",
      gpuVendor: "Google Inc.",
      gpuRenderer: "ANGLE (Intel)",
      hardwareConcurrency: 8,
      location: "40.7,-74.0",
      timezone: "America/New_York",
      webrtcIp: "1.2.3.4",
    });
    expect(args).toEqual(
      expect.arrayContaining([
        "--fingerprint=seed-1",
        "--fingerprint-platform=windows",
        "--fingerprint-platform-version=10.0.0",
        "--fingerprint-brand=Edge",
        "--fingerprint-brand-version=149",
        "--fingerprint-gpu-vendor=Google Inc.",
        "--fingerprint-gpu-renderer=ANGLE (Intel)",
        "--fingerprint-hardware-concurrency=8",
        "--fingerprint-location=40.7,-74.0",
        "--timezone=America/New_York",
        "--webrtc-ip=1.2.3.4",
      ]),
    );
  });

  it("cleans accept-language (strips ;q= weights) for --accept-lang", () => {
    expect(fingerprintArgs({ acceptLanguage: "en-US,en;q=0.9" })).toContain("--accept-lang=en-US,en");
  });

  it("emits --disable-gpu-fingerprint only when disableGpuFingerprint is true", () => {
    expect(fingerprintArgs({ disableGpuFingerprint: true })).toContain("--disable-gpu-fingerprint");
    expect(fingerprintArgs({ disableGpuFingerprint: false })).not.toContain("--disable-gpu-fingerprint");
  });

  it("disables farble noise only when fingerprintNoise === false", () => {
    expect(fingerprintArgs({ fingerprintNoise: false })).toContain("--disable-fingerprint-noise");
    expect(fingerprintArgs({ fingerprintNoise: true })).not.toContain("--disable-fingerprint-noise");
    expect(fingerprintArgs({})).not.toContain("--disable-fingerprint-noise");
  });

  it("skips empty / undefined / null values", () => {
    const args = fingerprintArgs({ fingerprint: "", timezone: undefined, gpuVendor: null as unknown as string });
    expect(args.some((a) => a.startsWith("--fingerprint="))).toBe(false);
    expect(args.some((a) => a.startsWith("--timezone="))).toBe(false);
    expect(args.some((a) => a.startsWith("--fingerprint-gpu-vendor="))).toBe(false);
  });

  it("encodes a fingerprint profile (gzip+base64, lossless round-trip)", () => {
    const profile = { navigator: { userAgent: "Mozilla/5.0 …" }, screen: { width: 1920 } };
    const flag = fingerprintArgs({ fingerprintProfile: profile }).find((a) =>
      a.startsWith("--fingerprint-profile="),
    );
    expect(flag).toBeDefined();
    const b64 = flag!.slice("--fingerprint-profile=".length);
    expect(JSON.parse(gunzipSync(Buffer.from(b64, "base64")).toString())).toEqual(profile);
  });
});

describe("cleanAcceptLanguage", () => {
  it("strips q-weights, trims spaces, drops empties", () => {
    expect(cleanAcceptLanguage("en-US, en ;q=0.8, , fr")).toBe("en-US,en,fr");
    expect(cleanAcceptLanguage("de-DE,de;q=0.7,en;q=0.3")).toBe("de-DE,de,en");
    expect(cleanAcceptLanguage("")).toBe("");
  });
});

describe("splitFingerprintOptions", () => {
  it("separates fingerprint options from pass-through Playwright options", () => {
    const { fingerprint, rest } = splitFingerprintOptions({
      fingerprint: "s",
      platform: "windows",
      headless: true,
      proxy: { server: "http://x" },
    } as never);
    expect(fingerprint).toEqual({ fingerprint: "s", platform: "windows" });
    expect(rest).toEqual({ headless: true, proxy: { server: "http://x" } });
  });
});

describe("encodeProfile", () => {
  it("accepts a JSON string and round-trips", () => {
    const json = '{"a":1}';
    const b64 = encodeProfile(json);
    expect(gunzipSync(Buffer.from(b64, "base64")).toString()).toBe(json);
  });
});
