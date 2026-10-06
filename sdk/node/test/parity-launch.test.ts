// Launch behaviour: GPU defaults, new engine-switch gating, pass-through,
// voices, third-party cookies, transparent proxy, release channel.
import { describe, it, expect, afterEach, vi } from "vitest";
import { mkdirSync, mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import {
  DEFAULT_IGNORED_ARGS,
  gpuBlocklistArgs,
  gpuBackendArgs,
  xDisplayAvailable,
  mesaEglAvailable,
  gateEngineSwitches,
  engineExtrasArgs,
  GATED_ENGINE_SWITCHES,
} from "../src/launchopts.js";
import { fingerprintArgs, isFingerprintPassthrough, FINGERPRINT_KEYS, splitFingerprintOptions } from "../src/fingerprint.js";
import { resolveReleaseChannel, proDownloadUrl } from "../src/download.js";

const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
  vi.restoreAllMocks();
});

/** A fake engine binary containing exactly these NUL-delimited switch literals. */
function fakeEngine(switches: string[]): string {
  const d = mkdtempSync(join(tmpdir(), "cc-fake-engine-"));
  dirs.push(d);
  const exe = join(d, process.platform === "win32" ? "chrome.exe" : "chrome");
  const body = Buffer.concat([Buffer.from("MZ\0padding\0"), ...switches.map((s) => Buffer.from(`\0${s}\0`, "latin1")), Buffer.from("\0end")]);
  writeFileSync(exe, body);
  if (process.platform === "win32") writeFileSync(join(d, "chrome.dll"), body);
  return exe;
}

describe("GPU launch defaults (#1 + #2)", () => {
  it("strips Playwright's automation AND SwiftShader defaults", () => {
    expect(DEFAULT_IGNORED_ARGS).toEqual(["--enable-automation", "--enable-unsafe-swiftshader", "--hide-scrollbars"]);
  });

  it("adds --ignore-gpu-blocklist when headed, on any OS", () => {
    expect(gpuBlocklistArgs(true, "linux")).toEqual(["--ignore-gpu-blocklist"]);
    expect(gpuBlocklistArgs(true, "win32")).toEqual(["--ignore-gpu-blocklist"]);
  });

  it("adds it on Windows even when headless", () => {
    expect(gpuBlocklistArgs(false, "win32")).toEqual(["--ignore-gpu-blocklist"]);
  });

  it("adds nothing for headless Linux (SwiftShader WebGL works there regardless — measured)", () => {
    expect(gpuBlocklistArgs(false, "linux")).toEqual([]);
  });

  // A display socket that exists on every host the suite runs on would make these host-dependent,
  // so they use a remote-style DISPLAY (trusted without a socket probe) or none at all.
  const remote = { DISPLAY: "remotehost:0" } as NodeJS.ProcessEnv;

  it("gives a Windows claim on Linux SwiftShader, headed and headless (GL clamps VUV to 1024 under a D3D11 label)", () => {
    expect(gpuBackendArgs("windows", true, "linux")).toEqual(["--use-angle=swiftshader-webgl"]);
    expect(gpuBackendArgs("windows", false, "linux")).toEqual(["--use-angle=swiftshader-webgl"]);
  });

  it("gives a headless Linux claim Mesa over the X display, else over EGL when the host has it", () => {
    for (const egl of [true, false]) {
      expect(gpuBackendArgs("linux", false, "linux", [], remote, egl)).toEqual(["--use-angle=gl", "--ignore-gpu-blocklist"]);
    }
    for (const env of [{}, { DISPLAY: ":4242" }] as NodeJS.ProcessEnv[]) { // none, or no such socket
      expect(gpuBackendArgs("linux", false, "linux", [], env, true)).toEqual(["--use-angle=gl-egl", "--ignore-gpu-blocklist"]);
      expect(gpuBackendArgs("linux", false, "linux", [], env, false)).toEqual([]); // stays on SwiftShader
    }
    expect(gpuBackendArgs("linux", false, "linux", ["--ignore-gpu-blocklist"], {}, true)).toEqual(["--use-angle=gl-egl"]);
    expect(gpuBackendArgs("linux", false, "linux", ["--use-angle=swiftshader"], {}, true)).toEqual([]);
    expect(gpuBackendArgs("windows", false, "linux", [], {}, true)).toEqual(["--use-angle=swiftshader-webgl"]);
  });

  it("leaves a headed Linux claim on the default GL path", () => {
    expect(gpuBackendArgs("linux", true, "linux", [], remote, true)).toEqual([]);
  });

  it("mesaEglAvailable needs libEGL, Mesa's EGL vendor and a software rasterizer", () => {
    const root = mkdtempSync(join(tmpdir(), "cc-egl-"));
    dirs.push(root);
    const libs = (dir: string, ...names: string[]) => {
      for (const n of names) {
        mkdirSync(dirname(join(root, dir, n)), { recursive: true });
        writeFileSync(join(root, dir, n), "");
      }
      return [join(root, dir)];
    };
    expect(mesaEglAvailable(libs("debian", "libEGL.so.1", "libEGL_mesa.so.0", "dri/swrast_dri.so"))).toBe(true);
    expect(mesaEglAvailable(libs("kms", "libEGL.so.1", "libEGL_mesa.so.0", "dri/kms_swrast_dri.so"))).toBe(true);
    // Mesa 24.2+ keeps its drivers in libgallium-<version>.so
    expect(mesaEglAvailable(libs("gallium", "libEGL.so.1", "libEGL_mesa.so.0", "libgallium-24.2.8.so"))).toBe(true);
    // libEGL missing (no WebGL at all under gl-egl), no Mesa vendor, no rasterizer
    expect(mesaEglAvailable(libs("nolib", "libEGL_mesa.so.0", "dri/swrast_dri.so"))).toBe(false);
    expect(mesaEglAvailable(libs("novendor", "libEGL.so.1", "dri/swrast_dri.so"))).toBe(false);
    expect(mesaEglAvailable(libs("nodriver", "libEGL.so.1", "libEGL_mesa.so.0"))).toBe(false);
    // the pieces may sit in different directories (multiarch dir + /usr/lib)
    expect(mesaEglAvailable([...libs("a", "libEGL.so.1"), ...libs("b", "libEGL_mesa.so.0", "dri/swrast_dri.so")])).toBe(true);
    expect(mesaEglAvailable([join(root, "missing")])).toBe(false);
  });

  it("never overrides the caller's backend, other hosts, pass-through or android", () => {
    expect(gpuBackendArgs("windows", true, "linux", ["--use-angle=vulkan"])).toEqual([]);
    expect(gpuBackendArgs("linux", false, "linux", ["--use-gl=egl"], remote)).toEqual([]);
    expect(gpuBackendArgs("linux", false, "linux", ["--ignore-gpu-blocklist"], remote)).toEqual(["--use-angle=gl"]);
    expect(gpuBackendArgs("windows", true, "win32")).toEqual([]);
    expect(gpuBackendArgs("windows", true, "darwin")).toEqual([]);
    expect(gpuBackendArgs(undefined, false, "linux", [], remote)).toEqual([]);
    expect(gpuBackendArgs("android", false, "linux", [], remote)).toEqual([]);
  });

  it("xDisplayAvailable: empty and malformed displays are unreachable, a host:N display is trusted", () => {
    expect(xDisplayAvailable({ DISPLAY: "" } as NodeJS.ProcessEnv)).toBe(false);
    expect(xDisplayAvailable({ DISPLAY: ":abc" } as NodeJS.ProcessEnv)).toBe(false);
    expect(xDisplayAvailable(remote)).toBe(true);
  });

  it("never duplicates a caller-supplied flag", () => {
    expect(gpuBlocklistArgs(true, "linux", ["--ignore-gpu-blocklist"])).toEqual([]);
  });
});

describe("gateEngineSwitches", () => {
  const all = Object.keys(GATED_ENGINE_SWITCHES);

  it("keeps every new switch on an engine that implements them", () => {
    const exe = fakeEngine(all.map((s) => s.slice(2)));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const r = gateEngineSwitches(exe, ["--foo", ...all], false);
    expect(r.args).toEqual(["--foo", ...all]);
    expect(r.warnings).toEqual([]);
    expect(warn).not.toHaveBeenCalled();
  });

  it("drops each unsupported switch with a warning on an older engine, keeping everything else", () => {
    const exe = fakeEngine(["proxy-auth"]);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const r = gateEngineSwitches(exe, ["--foo=1", "--allow-third-party-cookies", "--transparent-proxy", "--disable-fingerprint-voices", "--fingerprint-passthrough"], false);
    expect(r.args).toEqual(["--foo=1"]);
    expect(r.warnings).toHaveLength(4);
    expect(warn).toHaveBeenCalledTimes(4);
    expect(r.warnings.join("\n")).toMatch(/allowThirdPartyCookies: true needs engine 152 r22/);
  });

  it("is silent under quiet but still reports the warnings", () => {
    const exe = fakeEngine([]);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const r = gateEngineSwitches(exe, ["--transparent-proxy"], true);
    expect(r.args).toEqual([]);
    expect(r.warnings).toHaveLength(1);
    expect(warn).not.toHaveBeenCalled();
  });

  it("does not mistake a longer literal for the switch", () => {
    const exe = fakeEngine(["allow-third-party-cookies-extra", "xtransparent-proxy"]);
    vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(gateEngineSwitches(exe, ["--allow-third-party-cookies", "--transparent-proxy"], true).args).toEqual([]);
  });
});

describe("engineExtrasArgs", () => {
  it("allowThirdPartyCookies emits its switch", () => {
    expect(engineExtrasArgs({ allowThirdPartyCookies: true }, undefined)).toEqual(["--allow-third-party-cookies"]);
    expect(engineExtrasArgs({ allowThirdPartyCookies: false }, undefined)).toEqual([]);
  });

  it("transparentProxy needs a proxy; without one it is dropped with a note", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(engineExtrasArgs({ transparentProxy: true }, { server: "http://p:8080" })).toEqual(["--transparent-proxy"]);
    expect(engineExtrasArgs({ transparentProxy: true }, undefined)).toEqual([]);
    expect(warn).toHaveBeenCalledWith(expect.stringMatching(/no effect without a proxy/));
  });
});

describe("fingerprint pass-through (--fingerprint=off)", () => {
  it.each(["off", "OFF", " off "])("recognises %j", (v) => {
    expect(isFingerprintPassthrough(v)).toBe(true);
  });

  // Seeds a caller may genuinely use (a loop from 0, words) must stay seeds.
  it.each(["seed-1", "offline", "0x1", "", 1, 0, "0", "no", "false", "disable", "disabled", undefined])("does not treat %j as pass-through", (v) => {
    expect(isFingerprintPassthrough(v)).toBe(false);
  });

  it("emits NO persona switches — never --fingerprint=off, which an old engine would read as a seed", () => {
    const args = fingerprintArgs({ fingerprint: "off", platform: "windows", brand: "Edge", gpuVendor: "X", lightStealth: true });
    expect(args).toEqual(["--fingerprint-passthrough"]);
    expect(args.some((a) => a.startsWith("--fingerprint="))).toBe(false);
  });

  it("keeps only explicit locale/network values, like the engine does", () => {
    expect(fingerprintArgs({ fingerprint: "off", timezone: "Europe/Berlin", acceptLanguage: "de-DE,de;q=0.9", webrtcIp: "1.2.3.4" })).toEqual([
      "--fingerprint-passthrough",
      "--timezone=Europe/Berlin",
      "--accept-lang=de-DE,de",
      "--lang=de",
      "--webrtc-ip=1.2.3.4",
    ]);
  });

  it("does not add the usual coherence defaults (accept-lang / timezone / platform / brand)", () => {
    const args = fingerprintArgs({ fingerprint: "off" });
    expect(args.join(" ")).not.toMatch(/accept-lang|timezone|fingerprint-platform|fingerprint-brand/);
  });
});

describe("fingerprintVoices", () => {
  it("is a fingerprint option (split off Playwright options)", () => {
    expect(FINGERPRINT_KEYS).toContain("fingerprintVoices");
    expect(splitFingerprintOptions({ fingerprintVoices: false, headless: true } as never).fingerprint).toEqual({ fingerprintVoices: false });
  });

  it("false emits --disable-fingerprint-voices; unset/true emits nothing", () => {
    expect(fingerprintArgs({ fingerprint: "s", fingerprintVoices: false })).toContain("--disable-fingerprint-voices");
    expect(fingerprintArgs({ fingerprint: "s", fingerprintVoices: true })).not.toContain("--disable-fingerprint-voices");
    expect(fingerprintArgs({ fingerprint: "s" })).not.toContain("--disable-fingerprint-voices");
  });
});

describe("release channel (#5)", () => {
  it("defaults to stable, honours the option over the env, and rejects typos", () => {
    expect(resolveReleaseChannel(undefined, {})).toBe("stable");
    expect(resolveReleaseChannel(undefined, { CLEARCOTE_RELEASE_CHANNEL: "preview" })).toBe("preview");
    expect(resolveReleaseChannel("stable", { CLEARCOTE_RELEASE_CHANNEL: "preview" })).toBe("stable");
    expect(resolveReleaseChannel(" Preview ", {})).toBe("preview");
    expect(() => resolveReleaseChannel("beta", {})).toThrow(/Unknown release channel 'beta'/);
  });

  it("adds channel=preview to the PRO download URL only for preview (older servers see no change)", () => {
    expect(proDownloadUrl("https://x.test/", "linux")).toBe("https://x.test/api/v1/download/pro?platform=linux");
    expect(proDownloadUrl("https://x.test", "windows", "152", "stable")).toBe("https://x.test/api/v1/download/pro?platform=windows&version=152");
    expect(proDownloadUrl("https://x.test", "windows", "152.0.7977.82-r21", "preview")).toBe(
      "https://x.test/api/v1/download/pro?platform=windows&version=152.0.7977.82-r21&channel=preview",
    );
  });
});

describe("serve as root", () => {
  it("adds --no-sandbox only for root on Linux, and never twice", async () => {
    const { serveNeedsNoSandbox } = await import("../src/index.js");
    expect(serveNeedsNoSandbox("linux", 0, [])).toBe(true);
    expect(serveNeedsNoSandbox("linux", 1000, [])).toBe(false);
    expect(serveNeedsNoSandbox("linux", 0, ["--no-sandbox"])).toBe(false);
    expect(serveNeedsNoSandbox("win32", undefined, [])).toBe(false);
  });
});

describe("serve: the unsupported-flag infobar", () => {
  it("headless: --disable-infobars, whatever else is on the line, and never twice", async () => {
    const { serveInfobarArgs } = await import("../src/index.js");
    expect(serveInfobarArgs(true, [])).toEqual(["--disable-infobars"]);
    expect(serveInfobarArgs(true, ["--no-sandbox"])).toEqual(["--disable-infobars"]);
    expect(serveInfobarArgs(true, ["--disable-infobars"])).toEqual([]);
  });

  it("headed: --test-type only with --no-sandbox, and never twice", async () => {
    const { serveInfobarArgs } = await import("../src/index.js");
    expect(serveInfobarArgs(false, [])).toEqual([]);
    expect(serveInfobarArgs(false, ["--ignore-certificate-errors"])).toEqual([]);
    expect(serveInfobarArgs(false, ["--no-sandbox"])).toEqual(["--test-type"]);
    expect(serveInfobarArgs(false, ["--no-sandbox", "--test-type=browser"])).toEqual([]);
  });
});
