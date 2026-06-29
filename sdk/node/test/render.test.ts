import { describe, it, expect } from "vitest";
import { evaluateRenderInfo, gpuFamily } from "../src/render.js";

describe("gpuFamily", () => {
  it("maps common renderer/vendor substrings to a family", () => {
    expect(gpuFamily("ANGLE (NVIDIA, NVIDIA GeForce RTX 3080, D3D11)")).toBe("nvidia");
    expect(gpuFamily("ANGLE (Intel, Intel(R) UHD Graphics 770, D3D11)")).toBe("intel");
    expect(gpuFamily("ANGLE (AMD, AMD Radeon RX 6800, D3D11)")).toBe("amd");
    expect(gpuFamily("Apple M2")).toBe("apple");
    expect(gpuFamily("")).toBe("");
  });
});

describe("evaluateRenderInfo", () => {
  it("accepts a coherent NVIDIA persona", () => {
    const v = evaluateRenderInfo({
      webgl: true, webgl2: true,
      vendor: "Google Inc. (NVIDIA)",
      renderer: "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)",
      maxTextureSize: 16384,
    });
    expect(v.coherent).toBe(true);
    expect(v.softwareSuspected).toBe(false);
    expect(v.warnings).toEqual([]);
  });

  it("flags a software rasterizer as a fatal tell", () => {
    const v = evaluateRenderInfo({
      webgl: true,
      vendor: "Google Inc. (Google)",
      renderer: "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (LLVM 16.0.0)), SwiftShader driver)",
    });
    expect(v.softwareSuspected).toBe(true);
    expect(v.coherent).toBe(false);
    expect(v.warnings.some((w) => w.includes("software rasterizer"))).toBe(true);
  });

  it("flags an incoherent vendor/renderer family", () => {
    const v = evaluateRenderInfo({
      webgl: true,
      vendor: "Google Inc. (Apple)",
      renderer: "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080, D3D11)",
    });
    expect(v.coherent).toBe(false);
    expect(v.warnings.some((w) => w.includes("disagree on GPU family"))).toBe(true);
  });

  it("flags missing WebGL", () => {
    const v = evaluateRenderInfo({ webgl: false });
    expect(v.coherent).toBe(false);
    expect(v.warnings.some((w) => w.includes("WebGL is unavailable"))).toBe(true);
  });

  it("flags a claimed-GPU mismatch", () => {
    const v = evaluateRenderInfo(
      { webgl: true, vendor: "Google Inc. (Intel)", renderer: "ANGLE (Intel, Intel(R) UHD Graphics 770, D3D11)" },
      "NVIDIA GeForce RTX 4090"
    );
    expect(v.coherent).toBe(false);
    expect(v.warnings.some((w) => w.includes("does not match"))).toBe(true);
  });

  it("prefers the unmasked pair over the masked one", () => {
    const v = evaluateRenderInfo({
      webgl: true,
      vendor: "WebKit", renderer: "WebKit WebGL",
      unmaskedVendor: "Google Inc. (Intel)",
      unmaskedRenderer: "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, D3D11)",
    });
    expect(v.renderer.startsWith("ANGLE (Intel")).toBe(true);
    expect(v.coherent).toBe(true);
  });
});
