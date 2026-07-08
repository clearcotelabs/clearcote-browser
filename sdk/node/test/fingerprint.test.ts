import { describe, it, expect } from "vitest";
import { gunzipSync } from "node:zlib";
import {
  fingerprintArgs,
  cleanAcceptLanguage,
  splitFingerprintOptions,
  encodeProfile,
  resolveTlsProfile,
  defaultTimezone,
} from "../src/fingerprint.js";

describe("fingerprintArgs", () => {
  const withPlatform = (plat: string, fn: () => void) => {
    const orig = Object.getOwnPropertyDescriptor(process, "platform");
    Object.defineProperty(process, "platform", { value: plat, configurable: true });
    try {
      fn();
    } finally {
      if (orig) Object.defineProperty(process, "platform", orig);
    }
  };

  it("defaults the persona to the HOST OS + Chrome, with a coherent Accept-Language + UI locale", () => {
    // On a Windows host the default persona is Windows + Chrome; defaults keep navigator.platform
    // and the UA-CH brand coherent, always emit en-US,en, and pin --lang so Intl matches.
    withPlatform("win32", () => {
      expect(fingerprintArgs({})).toEqual([
        "--fingerprint-platform=windows",
        "--fingerprint-brand=chrome",
        "--accept-lang=en-US,en",
        "--lang=en-US",
        "--timezone=America/New_York",
      ]);
    });
  });

  it("defaults the persona platform to linux on a Linux host (coherent with the Linux binary)", () => {
    withPlatform("linux", () => {
      expect(fingerprintArgs({})).toEqual([
        "--fingerprint-platform=linux",
        "--fingerprint-brand=chrome",
        "--accept-lang=en-US,en",
        "--lang=en-US",
        "--timezone=America/New_York",
      ]);
    });
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
    expect(args.some((a) => a.startsWith("--fingerprint-gpu-vendor="))).toBe(false);
    // timezone is special-cased: unset/empty falls back to the locale default (no host-UTC leak).
    expect(args).toContain("--timezone=America/New_York");
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

describe("tlsProfile (TLS network persona)", () => {
  it("match-persona (default) follows brandVersion's major", () => {
    expect(fingerprintArgs({ brandVersion: "120.0.6099.109" })).toContain(
      "--fingerprint-tls-profile=chrome-120"
    );
  });
  it("emits nothing with no brandVersion (native TLS)", () => {
    expect(fingerprintArgs({}).some((a) => a.startsWith("--fingerprint-tls-profile"))).toBe(false);
  });
  it("explicit chrome-<major> / number pins it; native/off disables", () => {
    expect(fingerprintArgs({ tlsProfile: "chrome-124" })).toContain("--fingerprint-tls-profile=chrome-124");
    expect(fingerprintArgs({ tlsProfile: 118 })).toContain("--fingerprint-tls-profile=chrome-118");
    for (const off of ["native", "off"] as const) {
      expect(
        fingerprintArgs({ tlsProfile: off, brandVersion: "120" }).some((a) =>
          a.startsWith("--fingerprint-tls-profile")
        )
      ).toBe(false);
    }
  });
  it("resolveTlsProfile handles every shape; unrecognized -> native", () => {
    expect(resolveTlsProfile("match-persona", { brandVersion: "131.0.1" })).toBe("chrome-131");
    expect(resolveTlsProfile("auto", {})).toBeUndefined();
    expect(resolveTlsProfile(undefined, {})).toBeUndefined();
    expect(resolveTlsProfile("chrome-120", {})).toBe("chrome-120");
    expect(resolveTlsProfile(125, {})).toBe("chrome-125");
    expect(resolveTlsProfile("off", { brandVersion: "120" })).toBeUndefined();
    expect(resolveTlsProfile("garbage", {})).toBeUndefined();
  });
});

describe("default timezone (locale-coherent, no UTC leak)", () => {
  it("derives a plausible timezone from the persona locale", () => {
    expect(defaultTimezone("en-US")).toBe("America/New_York");
    expect(defaultTimezone("de-DE")).toBe("Europe/Berlin");
    expect(defaultTimezone("ja-JP")).toBe("Asia/Tokyo");
    expect(defaultTimezone("en-ZA")).toBe("America/New_York"); // en-* subtag fallback -> the en default
    expect(defaultTimezone("xx-YY")).toBe("America/New_York"); // ultimate fallback
  });
  it("emits a locale-coherent --timezone by default; explicit + geoip win", () => {
    expect(fingerprintArgs({ acceptLanguage: "fr-FR,fr" })).toContain("--timezone=Europe/Paris");
    expect(fingerprintArgs({ timezone: "Asia/Dubai" }).filter((a) => a.startsWith("--timezone="))).toEqual([
      "--timezone=Asia/Dubai",
    ]);
  });
});

describe("android persona (best-effort mobile)", () => {
  it("emits the android platform + a phone window-size", () => {
    const args = fingerprintArgs({ platform: "android" });
    expect(args).toContain("--fingerprint-platform=android");
    expect(args).toContain("--window-size=412,915");
  });
  it("never auto-adds a window-size for desktop platforms", () => {
    for (const plat of ["windows", "linux", "macos"] as const) {
      expect(fingerprintArgs({ platform: plat }).some((a) => a.startsWith("--window-size"))).toBe(false);
    }
  });
});
