// The device id added to an old capture's WebGL renderer before it goes to the engine.
import { describe, it, expect } from "vitest";
import { gunzipSync } from "node:zlib";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { withAngleDeviceId, fixCaptureRenderers, captureForEngine } from "../src/angledeviceid.js";
import { BY_ARCH, BY_NAME } from "../src/angledeviceids.generated.js";
import { encodeProfile } from "../src/fingerprint.js";

const OLD = "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)";
const NEW = "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Ti (0x00002782) Direct3D11 vs_5_0 ps_5_0, D3D11)";
const capture = () => ({
  webgl: {
    webgl1: { debug: { UNMASKED_VENDOR_WEBGL: "Google Inc. (NVIDIA)", UNMASKED_RENDERER_WEBGL: OLD } },
    webgl2: { debug: { UNMASKED_VENDOR_WEBGL: "Google Inc. (NVIDIA)", UNMASKED_RENDERER_WEBGL: OLD } },
  },
  webgpu: { high_performance: { info: { vendor: "nvidia", architecture: "lovelace" } } },
  hardware_concurrency: 16,
});
const decode = (b64: string) => JSON.parse(gunzipSync(Buffer.from(b64, "base64")).toString("utf8"));

describe("withAngleDeviceId", () => {
  it("adds the id real captures of the card print", () => {
    expect(withAngleDeviceId(OLD)).toBe(NEW);
    expect(withAngleDeviceId(OLD, "lovelace")).toBe(NEW);
    expect(withAngleDeviceId(OLD.replace("Direct3D11 ", "Direct3D11on12 "))).toBe(NEW.replace("Direct3D11 ", "Direct3D11on12 "));
  });
  it("leaves alone: an id already there, a non-D3D11 renderer, an unknown card", () => {
    expect(withAngleDeviceId(NEW)).toBe(NEW);
    const gl = "ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 4070 Ti/PCIe/SSE2, OpenGL 4.5.0)";
    expect(withAngleDeviceId(gl)).toBe(gl);
    const odd = "ANGLE (NVIDIA, NVIDIA GeForce Imaginary 9999 Direct3D11 vs_5_0 ps_5_0, D3D11)";
    expect(withAngleDeviceId(odd)).toBe(odd);
  });
  it("picks the capture's chip generation for a generic name, else the name's most common id", () => {
    const amd = "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)";
    expect(withAngleDeviceId(amd, "gcn-5")).toContain(`(${BY_ARCH["AMD|AMD Radeon(TM) Graphics|gcn-5"]}) Direct3D11`);
    expect(withAngleDeviceId(amd, "none")).toContain(`(${BY_NAME["AMD|AMD Radeon(TM) Graphics"]}) Direct3D11`);
  });
  it("every id has ANGLE's 8-digit upper-case form", () => {
    for (const id of [...Object.values(BY_ARCH), ...Object.values(BY_NAME)]) expect(id).toMatch(/^0x[0-9A-F]{8}$/);
  });
});

describe("encodeProfile gives the engine the fixed renderer", () => {
  it("from an object, without touching the caller's object", () => {
    const c = capture();
    const out = decode(encodeProfile(c));
    expect(out.webgl.webgl1.debug.UNMASKED_RENDERER_WEBGL).toBe(NEW);
    expect(out.webgl.webgl2.debug.UNMASKED_RENDERER_WEBGL).toBe(NEW);
    expect(out.hardware_concurrency).toBe(16);
    expect(c.webgl.webgl1.debug.UNMASKED_RENDERER_WEBGL).toBe(OLD);
  });
  it("from a file and from a JSON string", () => {
    const dir = mkdtempSync(join(tmpdir(), "cc-angle-"));
    try {
      const f = join(dir, "cap.json");
      writeFileSync(f, JSON.stringify(capture(), null, 2));
      expect(decode(encodeProfile(f)).webgl.webgl2.debug.UNMASKED_RENDERER_WEBGL).toBe(NEW);
      expect(decode(encodeProfile(JSON.stringify(capture()))).webgl.webgl1.debug.UNMASKED_RENDERER_WEBGL).toBe(NEW);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
  it("an already-fine capture and non-JSON go through byte for byte", () => {
    const good = capture();
    fixCaptureRenderers(good);
    const text = JSON.stringify(good, null, 2);
    expect(gunzipSync(Buffer.from(encodeProfile(text), "base64")).toString("utf8")).toBe(text);
    const junk = Buffer.from("not json");
    expect(captureForEngine(junk)).toBe(junk);
  });
});
