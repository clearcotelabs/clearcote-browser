// The GPU vendor guard: the GPU a persona declares against the one this machine renders on (persona.ts), and the
// vendor filter of profile: "auto" (profilelib.ts sameGpuVendor). Mirrored by sdk/python/tests/test_gpu_host_guard.py
// and sdk/dotnet/tests/Clearcote.Tests/GpuHostGuardTests.cs.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { gzipSync } from "node:zlib";
import {
  GPU_CHECK_ENV, clearPersonaCaches, gpuHostMismatch, gpuSource, personaGpuFromToken, preparePersonaLaunch,
  warnGpuHostMismatch, type HostFacts,
} from "../src/persona.js";
import { sameGpuVendor, selectProfile, type ProfileIndexEntry } from "../src/profilelib.js";
import { resetSeenWarnings } from "../src/warnings.js";

const AMD = { vendor: "Google Inc. (AMD)", renderer: "ANGLE (AMD, AMD Radeon RX 6600 (0x000073FF) Direct3D11 vs_5_0 ps_5_0, D3D11)" };
const NV = { vendor: "Google Inc. (NVIDIA)", renderer: "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 (0x00002184) Direct3D11 vs_5_0 ps_5_0, D3D11)" };
const INTEL = { vendor: "Google Inc. (Intel)", renderer: "ANGLE (Intel, Intel(R) UHD Graphics 770 (0x00004680) Direct3D11 vs_5_0 ps_5_0, D3D11)" };
const SWIFT = { vendor: "Google Inc. (Google)", renderer: "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)" };
const host = (v: string): HostFacts => ({ gpu_vendor: v });

/** A persona token shaped like the service's: base64url(payload JSON) + "." + signature; payload.p = codec JSON. */
function token(gpu: { vendor: string; renderer: string } | null): string {
  const now = Math.floor(Date.now() / 1000);
  const payload = { v: 1, iat: now, nbf: now - 30, exp: now + 3600, p: gpu ? { codec: 1, persona: { webgl_vendor: gpu.vendor, webgl_renderer: gpu.renderer } } : {} };
  return `${Buffer.from(JSON.stringify(payload)).toString("base64url")}.c2ln`;
}

describe("personaGpuFromToken", () => {
  it("reads the WebGL vendor and renderer from the token's payload", () => {
    expect(personaGpuFromToken(token(AMD))).toEqual(AMD);
  });
  it("is null for a token with no persona or one it cannot read", () => {
    expect(personaGpuFromToken(token(null))).toBeNull();
    expect(personaGpuFromToken("tok-abc")).toBeNull();
    expect(personaGpuFromToken("")).toBeNull();
  });
});

describe("gpuSource", () => {
  it("names the caller's options, an imported profile with WebGL strings, or the engine", () => {
    expect(gpuSource({ "fingerprint-gpu-vendor": "Google Inc. (AMD)" }, null)).toBe("options");
    expect(gpuSource({ "fingerprint-gpu-renderer": "x" }, null)).toBe("options");
    expect(gpuSource({ "fingerprint-profile-id": "p1" }, null)).toBe("profile");
    expect(gpuSource({}, JSON.stringify({ webgl: { webgl1: { debug: { UNMASKED_RENDERER_WEBGL: AMD.renderer } } } }))).toBe("profile");
    // a profile without GPU strings (fonts and screen only) leaves the GPU to the engine, as the engine does
    expect(gpuSource({}, JSON.stringify({ screen: { width: 1920 } }))).toBe("engine");
    expect(gpuSource({ fingerprint: "7" }, null)).toBe("engine");
  });
});

describe("gpuHostMismatch", () => {
  it("flags a declared vendor that is not the host's, naming the source and how to fix it", () => {
    const m = gpuHostMismatch(AMD, host("nvidia"), ["--fingerprint=1"], "options");
    expect(m?.code).toBe("gpu-vendor-host:amd:nvidia");
    expect(m?.message).toContain("gpuVendor / gpuRenderer declare a AMD GPU");
    expect(m?.message).toContain("this machine renders on NVIDIA");
    expect(m?.message).toContain("card that computes");
    expect(m?.message).toContain("Remove gpuVendor / gpuRenderer");
    expect(m?.message).toContain(`${GPU_CHECK_ENV}=1`);
    expect(gpuHostMismatch(INTEL, host("nvidia"), [], "profile")?.message).toContain("Use a profile captured on a machine with the same GPU vendor");
    // the engine's own choice on an AMD host (no AMD row in its pool for that schema)
    const auto = gpuHostMismatch(INTEL, host("amd"), [], "engine");
    expect(auto?.code).toBe("gpu-vendor-host:intel:amd");
    expect(auto?.message).toContain("gpuStringSpoof: false");
  });

  it("passes the same vendor, whatever the model", () => {
    expect(gpuHostMismatch(NV, host("nvidia"), [], "options")).toBeNull();
    expect(gpuHostMismatch(INTEL, host("intel"), [], "engine")).toBeNull();
    expect(gpuHostMismatch(AMD, host("amd"), [], "profile")).toBeNull();
  });

  it("flags a graphics card's name on a software-rendering host, and not a software renderer", () => {
    const m = gpuHostMismatch(INTEL, host("none"), [], "engine");
    expect(m?.code).toBe("gpu-software-host:intel");
    expect(m?.message).toContain("renders WebGL in software");
    expect(m?.message).toContain("gpuStringSpoof: false");
    expect(gpuHostMismatch(NV, host("none"), [], "options")?.message).toContain("gpuVendor / gpuRenderer declare a NVIDIA GPU");
    expect(gpuHostMismatch(SWIFT, host("none"), [], "engine")).toBeNull();
    expect(gpuHostMismatch(SWIFT, host("nvidia"), [], "options")).toBeNull();
  });

  it("skips launches that show the real GPU or render somewhere else", () => {
    for (const sw of ["--canvas-bridge-url=ws://x:1", "--use-adapter-luid=1,2", "--disable-gpu-string-spoof", "--disable-gpu-fingerprint", "--fingerprint-passthrough"]) {
      expect(gpuHostMismatch(AMD, host("nvidia"), [sw], "options")).toBeNull();
      expect(gpuHostMismatch(INTEL, host("none"), [sw], "engine")).toBeNull();
    }
  });

  it("says nothing for an unknown host vendor or a persona it cannot read", () => {
    expect(gpuHostMismatch(AMD, host(""), [], "options")).toBeNull();
    expect(gpuHostMismatch(AMD, host("virtio"), [], "options")).toBeNull();
    expect(gpuHostMismatch(null, host("nvidia"), [], "options")).toBeNull();
  });
});

describe("warnGpuHostMismatch", () => {
  beforeEach(() => { resetSeenWarnings(); delete process.env.CLEARCOTE_NO_WARN; delete process.env[GPU_CHECK_ENV]; });
  afterEach(() => { vi.restoreAllMocks(); delete process.env.CLEARCOTE_NO_WARN; delete process.env[GPU_CHECK_ENV]; resetSeenWarnings(); });
  const sel = { "fingerprint-gpu-vendor": AMD.vendor, "fingerprint-gpu-renderer": AMD.renderer };

  it("warns once per process", () => {
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    warnGpuHostMismatch(token(AMD), host("nvidia"), [], sel, null);
    warnGpuHostMismatch(token(AMD), host("nvidia"), [], sel, null);
    const out = err.mock.calls.map((c) => String(c[0])).join("");
    expect(out.match(/clearcote: warning: gpuVendor \/ gpuRenderer declare a AMD GPU/g)).toHaveLength(1);
  });

  it("is silenced by quiet, CLEARCOTE_NO_WARN and CLEARCOTE_NO_GPU_HOST_CHECK", () => {
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    warnGpuHostMismatch(token(AMD), host("nvidia"), [], sel, null, true);
    process.env[GPU_CHECK_ENV] = "1";
    warnGpuHostMismatch(token(AMD), host("nvidia"), [], sel, null);
    delete process.env[GPU_CHECK_ENV];
    process.env.CLEARCOTE_NO_WARN = "1";
    warnGpuHostMismatch(token(AMD), host("nvidia"), [], sel, null);
    expect(err).not.toHaveBeenCalled();
  });
});

describe("preparePersonaLaunch runs the guard on the token it fetched, against the engine's host facts", () => {
  let dir: string;
  let exe: string;
  const lease = { token: "hdr.sig", leaseId: "L1" };
  const R51 = (_exe: string | undefined, name: string) => name === "cc-persona-host-facts";
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "cc-gpuguard-"));
    exe = join(dir, "chrome");
    writeFileSync(exe, "x");
    clearPersonaCaches();
    resetSeenWarnings();
    delete process.env.CLEARCOTE_NO_WARN;
    delete process.env[GPU_CHECK_ENV];
  });
  afterEach(() => { vi.restoreAllMocks(); rmSync(dir, { recursive: true, force: true }); clearPersonaCaches(); resetSeenWarnings(); });
  const deps = (vendor: string, gpu: typeof AMD, quiet = false) => ({
    supports: R51, quiet,
    runner: () => `{"gpu_vendor":"${vendor}"}\n`,
    fetch: async () => ({ token: token(gpu), exp: Date.now() / 1000 + 3600 }),
  });

  it("warns for gpuVendor AMD on an NVIDIA host, and not for the host's vendor", async () => {
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    await preparePersonaLaunch(exe, ["--fingerprint=1", `--fingerprint-gpu-vendor=${AMD.vendor}`, `--fingerprint-gpu-renderer=${AMD.renderer}`], lease, undefined, deps("nvidia", AMD));
    await preparePersonaLaunch(exe, ["--fingerprint=2"], lease, undefined, deps("nvidia", NV));
    const out = err.mock.calls.map((c) => String(c[0])).join("");
    expect(out).toContain("gpuVendor / gpuRenderer declare a AMD GPU");
    expect(out).toContain("this machine renders on NVIDIA");
    expect(out.match(/clearcote: warning:/g)).toHaveLength(1);
  });

  it("warns for an imported profile of another vendor", async () => {
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    const profile = gzipSync(Buffer.from(JSON.stringify({ webgl: { webgl1: { debug: { UNMASKED_VENDOR_WEBGL: INTEL.vendor, UNMASKED_RENDERER_WEBGL: INTEL.renderer } } } }))).toString("base64");
    await preparePersonaLaunch(exe, [`--fingerprint-profile=${profile}`], lease, undefined, deps("nvidia", INTEL));
    expect(err.mock.calls.map((c) => String(c[0])).join("")).toContain("the imported profile declares a Intel GPU");
  });

  it("says nothing when quiet", async () => {
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    await preparePersonaLaunch(exe, ["--fingerprint=1", `--fingerprint-gpu-vendor=${AMD.vendor}`], lease, undefined, deps("nvidia", AMD, true));
    expect(err).not.toHaveBeenCalled();
  });
});

describe("profile: \"auto\" picks the host's GPU vendor", () => {
  const entry = (id: string, gpu_vendor: string, screen = 1920): ProfileIndexEntry => ({
    id, os_family: "windows", browser_major: 158, gpu_vendor, screen_width: screen, screen_height: 1080,
  } as ProfileIndexEntry);
  const h = (gpu_vendor: string) => ({ os_family: "windows", browser_major: 158, gpu_vendor, screen_width: 1920, screen_height: 1080 }) as never;

  it("sameGpuVendor keeps only the host vendor's candidates when there are any", () => {
    const pool = [entry("a", "amd"), entry("n1", "nvidia"), entry("n2", "nvidia"), entry("i", "intel")];
    expect(sameGpuVendor(pool, h("nvidia")).map((e) => e.id)).toEqual(["n1", "n2"]);
    expect(sameGpuVendor(pool, h("amd")).map((e) => e.id)).toEqual(["a"]);
    // none of this vendor, or a host vendor no profile can match: the pool as it was (the launch warns)
    expect(sameGpuVendor([entry("i", "intel")], h("amd")).map((e) => e.id)).toEqual(["i"]);
    expect(sameGpuVendor(pool, h("software"))).toHaveLength(4);
    expect(sameGpuVendor(pool, h("unknown"))).toHaveLength(4);
  });

  it("selectProfile never picks another vendor when the index has the host's, however wide the pool", () => {
    // 30 Intel entries that fit the screen better than the one NVIDIA entry: scoring alone put Intel in the top 25
    const pool = [entry("n", "nvidia", 1280), ...Array.from({ length: 30 }, (_, i) => entry(`i${i}`, "intel"))];
    for (const key of ["k1", "k2", "k3", "k4", "k5"]) expect(selectProfile(pool, h("nvidia"), { key }).entry.id).toBe("n");
  });
});
