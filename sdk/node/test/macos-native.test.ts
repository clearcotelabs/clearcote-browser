// The native macOS PRO build (Apple Silicon, a Chromium.app bundle).
//
// There is no Mac in CI, so every darwin path here runs with process.platform and the CPU faked
// (hostArch.get). Covered: an explicit executablePath is honoured on any OS, darwin maps to the "macos"
// PRO download key (arm64 only), the bundle's own executable is found inside the .app (never the
// framework or a helper), the zip is unpacked with ditto and un-quarantined, and launch() on a licensed
// Apple Silicon Mac runs the native build instead of the Docker image.
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import extract from "extract-zip";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { dockerRequested, TEST_ONLY_ASSUME_MACOS } from "../src/docker.js";
import {
  ensureBinary, ensureRunnableHere, extractMacBundle, listCachedBuilds, cachedBinary, macExtractHooks,
  proEnsureBinary, resolveVersion, writeManifest,
} from "../src/download.js";
import { engineSupportsSwitch, macosKeychainArgs } from "../src/launchopts.js";
import { hostArch, platformRelease, proPlatformKey } from "../src/release.js";
import { coherenceWarnings } from "../src/warnings.js";
import { storedZip } from "./helpers/fake-build.js";

const APP_EXE = path.join("Chromium.app", "Contents", "MacOS", "Chromium");
const FRAMEWORK = path.join("Chromium.app", "Contents", "Frameworks", "Chromium Framework.framework");

let tmp: string;
let platformDesc: PropertyDescriptor;
const savedEnv: Record<string, string | undefined> = {};
const ENV_KEYS = ["CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_DOCKER", TEST_ONLY_ASSUME_MACOS, "HOME", "USERPROFILE"];

function on(platform: string, arch = "arm64"): void {
  Object.defineProperty(process, "platform", { ...platformDesc, value: platform });
  vi.spyOn(hostArch, "get").mockReturnValue(arch);
}

beforeEach(() => {
  tmp = mkdtempSync(path.join(os.tmpdir(), "cc-macos-"));
  platformDesc = Object.getOwnPropertyDescriptor(process, "platform")!;
  for (const k of ENV_KEYS) savedEnv[k] = process.env[k];
  for (const k of ENV_KEYS.slice(0, 4)) delete process.env[k];
  process.env.HOME = process.env.USERPROFILE = path.join(tmp, "home"); // no saved licence key is read
});

afterEach(() => {
  Object.defineProperty(process, "platform", platformDesc);
  vi.restoreAllMocks();
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
  rmSync(tmp, { recursive: true, force: true });
});

function put(file: string, data: Buffer | string = Buffer.alloc(64, "x")): void {
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, data);
}

/** A Chromium.app laid out like the real one, with decoys a name search could land on: a helper app
 * inside the framework and a resource file that is also called `Chromium`. */
function fakeApp(root: string): Record<string, Buffer> {
  const files: Record<string, Buffer> = {
    [APP_EXE]: Buffer.from("launcher-without-switch-strings"),
    [path.join("Chromium.app", "Contents", "Info.plist")]: Buffer.from("<plist/>"),
    [path.join(FRAMEWORK, "Versions", "Current", "Chromium Framework")]: Buffer.concat([Buffer.alloc(32, "x"), Buffer.from("\0proxy-auth\0")]),
    [path.join(FRAMEWORK, "Versions", "Current", "Resources", "Chromium")]: Buffer.from("decoy"),
    [path.join(FRAMEWORK, "Versions", "Current", "Helpers", "Chromium Helper.app", "Contents", "MacOS", "Chromium Helper")]: Buffer.from("helper"),
  };
  for (const [rel, data] of Object.entries(files)) put(path.join(root, rel), data);
  return files;
}

describe("ensureRunnableHere (the _guard equivalent)", () => {
  it("honours an existing executablePath on darwin, whatever the CPU", () => {
    fakeApp(tmp);
    const exe = path.join(tmp, APP_EXE);
    on("darwin", "arm64");
    expect(() => ensureRunnableHere(exe)).not.toThrow();
    on("darwin", "x64");
    expect(() => ensureRunnableHere(exe)).not.toThrow();
  });

  it("still refuses an OS with no build", () => {
    on("freebsd");
    expect(() => ensureRunnableHere(path.join(tmp, "missing", "chrome"))).toThrow(/no build for/);
  });
});

describe("platform key", () => {
  it("maps darwin to the macos PRO key on Apple Silicon", async () => {
    expect(proPlatformKey("darwin", "arm64")).toBe("macos");
    expect(proPlatformKey("win32", "x64")).toBe("windows");
    expect(proPlatformKey("linux", "x64")).toBe("linux");
    expect(proPlatformKey("freebsd", "x64")).toBeNull();
    expect(platformRelease("darwin", "arm64")?.assetGlob).toBe("macos-arm64"); // the open macOS pin
    expect(platformRelease("darwin", "x64")).toBeUndefined(); // arm64 only, like the PRO build
    on("darwin", "arm64");
    // the catalog lookup uses the macos key (no PRO 155 in the bundled snapshot, so it names the key)
    await expect(resolveVersion("155", true, true, { schema: 1, builds: [] })).rejects.toThrow(/for macos/);
  });

  it("tells an Intel Mac the build is Apple Silicon only", async () => {
    expect(() => proPlatformKey("darwin", "x64")).toThrow(/Apple Silicon/);
    on("darwin", "x64");
    await expect(proEnsureBinary("cc_lic_x", { quiet: true })).rejects.toThrow(/Apple Silicon/);
  });

  it("fails the free build on an Intel Mac before downloading anything", async () => {
    on("darwin", "x64");
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    await expect(ensureBinary({ cacheDir: tmp, quiet: true })).rejects.toThrow(/Apple Silicon only/);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("downloads the open macOS pin on Apple Silicon without a key", async () => {
    on("darwin", "arm64");
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("stop after choosing the asset"));
    await expect(ensureBinary({ cacheDir: tmp, quiet: true })).rejects.toThrow(/stop after choosing the asset/);
    expect(String(fetchSpy.mock.calls[0]?.[0])).toBe(platformRelease("darwin", "arm64")!.url);
  });
});

describe("the bundle", () => {
  it("finds the bundle executable and lists the cached build on darwin", () => {
    on("darwin", "arm64");
    const base = path.join(tmp, "cache", "pro-155");
    fakeApp(path.join(base, "browser"));
    writeManifest(base, path.join(base, "browser"));
    writeFileSync(path.join(base, ".verified"), "x\n");
    const exe = path.join(base, "browser", APP_EXE);
    expect(cachedBinary(base, "Chromium", true, false)).toBe(exe);
    expect(listCachedBuilds(path.join(tmp, "cache"))).toEqual([{ tag: "pro-155", path: exe }]);
    // the darwin integrity list is the bundle's: a missing framework binary is caught
    unlinkSync(path.join(base, "browser", FRAMEWORK, "Versions", "Current", "Chromium Framework"));
    expect(cachedBinary(path.join(tmp, "cache", "pro-155"), "Chromium", true, false, true)).toBeNull();
  });

  it("probes switches in the framework binary, not the launcher", () => {
    fakeApp(tmp);
    on("darwin", "arm64");
    expect(engineSupportsSwitch(path.join(tmp, APP_EXE), "proxy-auth")).toBe(true);
    expect(engineSupportsSwitch(path.join(tmp, APP_EXE), "no-such-switch")).toBe(false);
  });

  it("adds --use-mock-keychain on macOS only", () => {
    expect(macosKeychainArgs("darwin")).toEqual(["--use-mock-keychain"]);
    expect(macosKeychainArgs("darwin", ["--use-mock-keychain"])).toEqual([]);
    expect(macosKeychainArgs("linux")).toEqual([]);
    expect(macosKeychainArgs("win32")).toEqual([]);
  });
});

describe("PRO download on a Mac", () => {
  it("asks for platform=macos, unpacks with ditto and clears the quarantine flag", async () => {
    on("darwin", "arm64");
    const files = fakeApp(path.join(tmp, "src"));
    const zip = storedZip(Object.fromEntries(Object.entries(files).map(([k, v]) => [k.split(path.sep).join("/"), v])));
    const sha = createHash("sha256").update(zip).digest("hex");
    const exeSha = createHash("sha256").update(files[APP_EXE]).digest("hex");
    const urls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((async (url: unknown) => {
      urls.push(String(url));
      if (String(url).includes("/api/v1/download/pro")) {
        // the route's reply for macOS, minus "binary": the SDK's macOS default must be the bundle's
        return new Response(JSON.stringify({
          version: "155.0.8059.39", tag: "pro-155.0.8059.39-r42", platform: "macos",
          asset: "clearcote-pro-155.0.8059.39-macos-arm64.zip", archive: "zip", url: "https://blob.test/mac.zip",
          sha256: sha, exe_sha256: exeSha, size: zip.length,
        }), { status: 200 });
      }
      return new Response(zip, { status: 200, headers: { "content-length": String(zip.length) } });
    }) as typeof fetch);
    const calls: string[][] = [];
    vi.spyOn(macExtractHooks, "run").mockImplementation(async (file: string, args: string[]) => {
      calls.push([file, ...args]);
      if (file === "/usr/bin/ditto") await extract(args[2], { dir: path.resolve(args[3]) }); // minus symlinks/modes
      return undefined;
    });
    const exe = await proEnsureBinary("cc_lic_mac", { apiBase: "https://site.test", cacheDir: path.join(tmp, "cache"), quiet: true });
    expect(urls[0]).toContain("platform=macos");
    expect(exe.endsWith(APP_EXE)).toBe(true);
    expect(readFileSync(exe, "utf8")).toBe("launcher-without-switch-strings");
    expect(calls[0].slice(0, 3)).toEqual(["/usr/bin/ditto", "-x", "-k"]);
    const xattr = calls.find((c) => c[0] === "/usr/bin/xattr");
    expect(xattr?.slice(1, 3)).toEqual(["-dr", "com.apple.quarantine"]);
    expect(xattr?.[3].endsWith("Chromium.app")).toBe(true);
  });

  it("falls back to unzip when ditto is missing", async () => {
    const calls: string[] = [];
    vi.spyOn(macExtractHooks, "run").mockImplementation(async (file: string) => {
      calls.push(file);
      if (file === "/usr/bin/ditto") throw Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" });
      return undefined;
    });
    await extractMacBundle("x.zip", path.join(tmp, "x.zip"), path.join(tmp, "out"));
    expect(calls.slice(0, 2)).toEqual(["/usr/bin/ditto", "/usr/bin/unzip"]);
  });
});

describe("persona on the macOS build", () => {
  it("warns that only platform='macos' is coherent there", () => {
    const hit = coherenceWarnings({ platform: "windows" }, "darwin").find((w) => w.code === "platform-host-fonts");
    expect(hit?.message).toContain("only platform='macos' is coherent");
    expect(coherenceWarnings({ platform: "macos" }, "darwin").map((w) => w.code)).not.toContain("platform-host-fonts");
    // a profile the caller captured on Windows is the documented workaround
    expect(coherenceWarnings({ platform: "windows", fingerprintProfile: { fonts: {} } }, "darwin").map((w) => w.code))
      .not.toContain("platform-host-fonts");
  });
});

describe("launch default on a Mac", () => {
  it("runs a native build on Apple Silicon (PRO or the open pin), Docker on an Intel Mac", () => {
    const decide = (arch: string, docker?: unknown, opts: Record<string, unknown> = {}) => {
      on("darwin", arch);
      return dockerRequested(docker, opts);
    };
    expect(decide("arm64", undefined, { licenseKey: "cc_lic_x" })).toBe(false); // native PRO build
    expect(decide("arm64")).toBe(false); // no key: the open macOS pin, also native
    expect(decide("x64", undefined, { licenseKey: "cc_lic_x" })).toBe(true); // Intel: both builds are arm64 only
    expect(decide("x64")).toBe(true);
    expect(decide("arm64", true, { licenseKey: "cc_lic_x" })).toBe(true); // Docker stays reachable
    // the test-only "assume macOS" switch exists to exercise the Docker path: it stays Docker
    on("linux", "arm64");
    process.env[TEST_ONLY_ASSUME_MACOS] = "1";
    expect(dockerRequested(undefined, { licenseKey: "cc_lic_x" })).toBe(true);
    expect(existsSync(tmp)).toBe(true);
  });
});
