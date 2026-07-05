import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  WIDEVINE_APP_ID,
  OMAHA_URL,
  HINT_FILE,
  omahaRequestBody,
  parseUpdate,
  crx3ToZip,
  widevineArgs,
} from "../src/widevine.js";

/** Force process.platform for a block of tests (widevineArgs reads it live), restoring after. */
function withPlatform(plat: NodeJS.Platform) {
  const original = Object.getOwnPropertyDescriptor(process, "platform");
  beforeEach(() => {
    Object.defineProperty(process, "platform", { value: plat, configurable: true });
  });
  afterEach(() => {
    if (original) Object.defineProperty(process, "platform", original);
  });
}

describe("widevine constants", () => {
  it("pin the Widevine component + Omaha endpoint", () => {
    expect(WIDEVINE_APP_ID).toBe("oimompecagnajdejgnnjijobebaeigek");
    expect(OMAHA_URL.startsWith("https://update.googleapis.com/")).toBe(true);
    expect(HINT_FILE).toBe("latest-component-updated-widevine-cdm");
  });
});

describe("omahaRequestBody", () => {
  it("targets the current-OS x64 CDM + the latest version", () => {
    const req = (omahaRequestBody() as any).request;
    // platform-aware: "win" on a Windows dev host, "Linux" on the Linux CI
    expect(["win", "Linux"]).toContain(req["@os"]);
    expect(req.arch).toBe("x64");
    expect(req.acceptformat).toBe("crx3");
    expect(req.app[0].appid).toBe(WIDEVINE_APP_ID);
    expect(req.app[0].version).toBe("0.0.0.0");
  });
});

describe("parseUpdate", () => {
  it("reads the pipelines shape", () => {
    const resp = {
      response: { app: [{
        nextversion: "4.10.3050.0",
        updatecheck: { status: "ok", pipelines: [{ operations: [
          { type: "download", urls: [{ url: "https://x/cdm.crx3" }], out: { sha256: "abcd" } },
        ] }] },
      }] },
    };
    expect(parseUpdate(resp)).toEqual(["https://x/cdm.crx3", "abcd", "4.10.3050.0"]);
  });

  it("reads the classic shape", () => {
    const resp = {
      response: { app: [{
        updatecheck: { status: "ok",
          urls: { url: [{ codebase: "https://x/dl/" }] },
          manifest: { version: "4.10.3050.0", packages: { package: [{ name: "cdm.crx3", hash_sha256: "beef" }] } } },
      }] },
    };
    expect(parseUpdate(resp)).toEqual(["https://x/dl/cdm.crx3", "beef", "4.10.3050.0"]);
  });

  it("rejects a non-ok status", () => {
    expect(() => parseUpdate({ response: { app: [{ updatecheck: { status: "noupdate" } }] } })).toThrow();
  });
});

describe("crx3ToZip", () => {
  it("strips the CRX3 header", () => {
    const header = Buffer.from([0x10, 0x20, 0x30, 0x40]);
    const zip = Buffer.from("PK the zip", "latin1");
    const crx = Buffer.concat([
      Buffer.from("Cr24", "latin1"),
      (() => { const b = Buffer.alloc(4); b.writeUInt32LE(3, 0); return b; })(),
      (() => { const b = Buffer.alloc(4); b.writeUInt32LE(header.length, 0); return b; })(),
      header, zip,
    ]);
    expect(crx3ToZip(crx).equals(zip)).toBe(true);
  });

  it("passes a plain zip through unchanged", () => {
    const plain = Buffer.from("PK already a zip", "latin1");
    expect(crx3ToZip(plain).equals(plain)).toBe(true);
  });

  it("throws on a malformed CRX3 (header overruns buffer)", () => {
    const bad = Buffer.concat([
      Buffer.from("Cr24", "latin1"),
      (() => { const b = Buffer.alloc(4); b.writeUInt32LE(3, 0); return b; })(),
      (() => { const b = Buffer.alloc(4); b.writeUInt32LE(9999, 0); return b; })(),
    ]);
    expect(() => crx3ToZip(bad)).toThrow();
  });
});

// The fast-update scan is Windows-only, so pin the platform to win32 for these arg assertions.
describe("widevineArgs (Windows: un-suppress updater + force the scan)", () => {
  withPlatform("win32");

  it("un-suppresses the component updater + forces the scan", () => {
    const { ignoreDefaultArgs, args } = widevineArgs(["--enable-automation"], []);
    expect(ignoreDefaultArgs).toContain("--disable-component-update");
    expect(ignoreDefaultArgs).toContain("--enable-automation");
    expect(args).toContain("--component-updater=fast-update");
  });

  it("seeds --disable-component-update when ignoreDefaultArgs is unset", () => {
    const { ignoreDefaultArgs, args } = widevineArgs(undefined, []);
    expect(ignoreDefaultArgs).toEqual(["--disable-component-update"]);
    expect(args).toContain("--component-updater=fast-update");
  });

  it("leaves the boolean form untouched (no crash) but still forces the scan", () => {
    const t = widevineArgs(true, []);
    expect(t.ignoreDefaultArgs).toBe(true); // already ignores ALL defaults
    expect(t.args).toContain("--component-updater=fast-update");
  });

  it("preserves user values + is idempotent", () => {
    const a = widevineArgs(["--enable-automation"], ["--foo"]);
    const b = widevineArgs(a.ignoreDefaultArgs, a.args);
    const ida = b.ignoreDefaultArgs as string[];
    expect(b.args.filter((x) => x === "--component-updater=fast-update")).toHaveLength(1);
    expect(ida.filter((x) => x === "--disable-component-update")).toHaveLength(1);
    expect(b.args).toContain("--foo");
  });

  it("respects a user-chosen component-updater mode", () => {
    const { args } = widevineArgs([], ["--component-updater=test-request"]);
    expect(args).toContain("--component-updater=test-request");
    expect(args).not.toContain("--component-updater=fast-update");
  });
});

describe("widevineArgs (Linux: un-suppress updater, but NO fast-update scan)", () => {
  withPlatform("linux");

  it("un-suppresses --disable-component-update but adds no component-updater flag", () => {
    const { ignoreDefaultArgs, args } = widevineArgs(["--enable-automation"], []);
    // The seeded hint file registers the CDM on Linux — the updater is un-suppressed, no scan flag.
    expect(ignoreDefaultArgs).toContain("--disable-component-update");
    expect(args.some((a) => a.includes("component-updater"))).toBe(false);
  });
});
