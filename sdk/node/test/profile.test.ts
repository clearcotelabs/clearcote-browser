import { describe, it, expect } from "vitest";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Profile, resolveProfileOptions } from "../src/profile.js";
import { fingerprintArgs } from "../src/fingerprint.js";

describe("Profile", () => {
  it("round-trips name + options through save/load", () => {
    const dir = mkdtempSync(join(tmpdir(), "cc-prof-"));
    try {
      const path = join(dir, "acct-1.json");
      new Profile("acct-1", {
        fingerprint: "acct-1",
        gpuRenderer: "ANGLE (Intel)",
        canvasBridge: { url: "ws://127.0.0.1:9099", auth: "user:secret" },
      }).save(path);
      const loaded = Profile.load(path);
      expect(loaded.name).toBe("acct-1");
      expect(loaded.options).toEqual({
        fingerprint: "acct-1",
        gpuRenderer: "ANGLE (Intel)",
        canvasBridge: { url: "ws://127.0.0.1:9099", auth: "user:secret" },
      });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("set() merges and chains", () => {
    const prof = new Profile("p").set({ fingerprint: "s" }).set({ brand: "Edge" });
    expect(prof.options).toEqual({ fingerprint: "s", brand: "Edge" });
  });

  it("resolveProfileOptions accepts a Profile instance or a path", () => {
    const prof = new Profile("p", { gpuVendor: "X" });
    expect(resolveProfileOptions(prof)).toEqual({ gpuVendor: "X" });
    const dir = mkdtempSync(join(tmpdir(), "cc-prof-"));
    try {
      const path = join(dir, "p.json");
      prof.save(path);
      expect(resolveProfileOptions(path)).toEqual({ gpuVendor: "X" });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("loads a snake_case profile from the Python SDK (key normalization)", () => {
    const dir = mkdtempSync(join(tmpdir(), "cc-prof-"));
    try {
      const path = join(dir, "py.json");
      writeFileSync(
        path,
        JSON.stringify({
          name: "py",
          options: { fingerprint: "s", gpu_renderer: "ANGLE (Intel)", canvas_bridge: { url: "ws://h:1" } },
        })
      );
      const prof = Profile.load(path);
      expect(prof.options).toEqual({
        fingerprint: "s",
        gpuRenderer: "ANGLE (Intel)",
        canvasBridge: { url: "ws://h:1" },
      });
      const args = fingerprintArgs(prof.options);
      expect(args).toContain("--fingerprint-gpu-renderer=ANGLE (Intel)");
      expect(args).toContain("--canvas-bridge-url=ws://h:1");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("rejects an unsafe profile name", () => {
    expect(() => new Profile("..").save()).toThrow();
  });
});
