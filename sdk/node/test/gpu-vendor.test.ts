// gpuVendor: a short name went out verbatim ("intel" as UNMASKED_VENDOR_WEBGL, next to the persona's AMD renderer;
// measured 2026-10-28, SC 295). It is now written the way Chrome reports it, and a vendor/renderer pair naming two
// GPU makers is refused.
import { describe, it, expect } from "vitest";
import { fingerprintArgs, gpuMaker, gpuVendorFor } from "../src/fingerprint.js";
import { coherenceWarnings } from "../src/warnings.js";

const AMD = "ANGLE (AMD, AMD Radeon(TM) Graphics (0x00001506) Direct3D11 vs_5_0 ps_5_0, D3D11)";
const INTEL = "ANGLE (Intel, Intel(R) UHD Graphics 620 (0x00005917) Direct3D11 vs_5_0 ps_5_0, D3D11)";

describe("gpuVendor", () => {
  it.each([
    ["intel", "Google Inc. (Intel)"], ["Intel Inc.", "Google Inc. (Intel)"], ["Intel Corporation", "Google Inc. (Intel)"],
    ["nvidia", "Google Inc. (NVIDIA)"], ["NVIDIA Corporation", "Google Inc. (NVIDIA)"], ["amd", "Google Inc. (AMD)"],
    ["ATI Technologies Inc.", "Google Inc. (AMD)"], ["Apple", "Google Inc. (Apple)"],
  ])("writes %s as Chrome reports it", (given, want) => {
    expect(gpuVendorFor(given, undefined, "windows")).toBe(want);
  });

  it("keeps Chrome's own form, an Intel Mac's, and strings that name no maker", () => {
    expect(gpuVendorFor("Google Inc. (NVIDIA)")).toBe("Google Inc. (NVIDIA)");
    expect(gpuVendorFor("intel", undefined, "macos")).toBe("Google Inc. (Intel Inc.)");
    expect(gpuVendorFor("Google Inc.", INTEL)).toBe("Google Inc.");
    expect(gpuVendorFor("Some Vendor")).toBe("Some Vendor");
  });

  it("refuses a vendor and a renderer from two makers", () => {
    expect(() => gpuVendorFor("intel", AMD)).toThrow(/names Intel but gpuRenderer names AMD/);
    expect(() => fingerprintArgs({ fingerprint: "1", gpuVendor: "intel", gpuRenderer: AMD })).toThrow(/matching pair/);
    expect(gpuVendorFor("intel", INTEL)).toBe("Google Inc. (Intel)");
  });

  it("reads the maker of ANGLE renderers and Chrome vendors", () => {
    expect(gpuMaker(AMD)).toBe("amd");
    expect(gpuMaker("Google Inc. (Intel)")).toBe("intel");
    expect(gpuMaker("ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)")).toBe("google");
    expect(gpuMaker("WebKit")).toBeNull();
  });

  it("goes out normalized on the command line", () => {
    expect(fingerprintArgs({ fingerprint: "1", platform: "windows", gpuVendor: "intel" })).toContain("--fingerprint-gpu-vendor=Google Inc. (Intel)");
  });

  it("warns when the vendor is pinned without the renderer", () => {
    const codes = (o: Record<string, unknown>) => coherenceWarnings(o, "win32").map((w) => w.code);
    expect(codes({ gpuVendor: "intel" })).toContain("gpu-vendor-alone");
    expect(codes({ gpuVendor: "intel", gpuRenderer: INTEL })).not.toContain("gpu-vendor-alone");
  });
});
