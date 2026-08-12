import { describe, it, expect, vi, afterEach } from "vitest";
import {
  extensionArgs,
  resolveProxy,
  mergeFeatureFlags,
  webBluetoothArgs,
  privacySandboxArgs,
  quicArgs,
  webrtcDefaultDenyArgs,
} from "../src/launchopts.js";

describe("mergeFeatureFlags", () => {
  it("collapses multiple --enable/--disable-features into one each", () => {
    const out = mergeFeatureFlags([
      "--enable-features=A", "--mute-audio", "--enable-features=B,C",
      "--disable-features=D", "--disable-features=D,E",
    ]);
    expect(out.filter((a) => a.startsWith("--enable-features="))).toEqual(["--enable-features=A,B,C"]);
    expect(out.filter((a) => a.startsWith("--disable-features="))).toEqual(["--disable-features=D,E"]);
    expect(out).toContain("--mute-audio");
  });
});

describe("privacySandboxArgs", () => {
  it("disables the Privacy Sandbox + intrusive APIs", () => {
    expect(privacySandboxArgs()).toEqual([
      "--disable-features=BrowsingTopics,BrowsingTopicsDocumentAPI,Fledge,InterestGroupStorage,PrivateAggregationApi,SharedStorageAPI,FencedFrames",
    ]);
  });
});

describe("webrtcDefaultDenyArgs", () => {
  it("defaults to disable_non_proxied_udp when no webrtcIp", () => {
    expect(webrtcDefaultDenyArgs([], undefined)).toEqual(["--webrtc-ip-handling-policy=disable_non_proxied_udp"]);
  });
  // Regression: this used to return [] when a webrtcIp was set, on the theory that the engine's
  // srflx fabrication covered WebRTC. It does not. A page using iceTransportPolicy:"relay" forces
  // TURN; TURN prefers UDP; an HTTP/SOCKS proxy carries only TCP — so the UDP left on the host's
  // own path and the TURN server read the real public IP off the packet, with no candidate
  // involved for the fabrication to rewrite. geoip:true sets webrtcIp for you, so the coherent
  // configurations were the exposed ones.
  it("still denies non-proxied UDP when a webrtcIp is set", () => {
    expect(webrtcDefaultDenyArgs([], "1.2.3.4")).toEqual(["--webrtc-ip-handling-policy=disable_non_proxied_udp"]);
  });
  it("is skipped when the caller already set a policy", () => {
    expect(webrtcDefaultDenyArgs(["--webrtc-ip-handling-policy=default"], undefined)).toEqual([]);
  });
  it("is skipped when the caller set a forced policy, even with a webrtcIp", () => {
    expect(webrtcDefaultDenyArgs(["--force-webrtc-ip-handling-policy=default"], "1.2.3.4")).toEqual([]);
  });
});

describe("quicArgs", () => {
  it("disables QUIC behind any proxy (SOCKS or HTTP)", () => {
    expect(quicArgs({ server: "socks5://host:1080" })).toEqual(["--disable-quic"]);
    expect(quicArgs({ server: "http://host:8080" })).toEqual(["--disable-quic"]);
  });
  it("leaves QUIC on when no proxy is set", () => {
    expect(quicArgs(undefined)).toEqual([]);
    expect(quicArgs({})).toEqual([]);
  });
});

describe("extensionArgs", () => {
  it("returns [] for empty input", () => {
    expect(extensionArgs()).toEqual([]);
    expect(extensionArgs([])).toEqual([]);
  });

  it("emits both --load-extension and --disable-extensions-except", () => {
    expect(extensionArgs(["/a", "/b"])).toEqual([
      "--load-extension=/a,/b",
      "--disable-extensions-except=/a,/b",
    ]);
  });
});

describe("resolveProxy", () => {
  it("passes through when no proxy", () => {
    expect(resolveProxy(undefined)).toEqual({ args: [], proxy: undefined });
  });

  it("routes a credentialed SOCKS5 proxy to --proxy-server and forwards the credentials", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const r = resolveProxy({ server: "socks5://h:1080", username: "u", password: "p" });
    expect(r.args).toEqual([
      "--proxy-server=socks5://h:1080",
      "--socks5-credentials=u:p",
    ]);
    expect(r.proxy).toBeUndefined();
    // The engine implements RFC 1929 now, so there is nothing to warn about.
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it("strips userinfo already present in the SOCKS5 URL", () => {
    const r = resolveProxy({ server: "socks5://old:secret@h:1080", username: "u", password: "p" });
    expect(r.args).toEqual([
      "--proxy-server=socks5://h:1080",
      "--socks5-credentials=u:p",
    ]);
  });

  it("leaves a SOCKS5 proxy without creds to Playwright", () => {
    const p = { server: "socks5://h:1080" };
    expect(resolveProxy(p)).toEqual({ args: [], proxy: p });
  });

  it("leaves an authed HTTP proxy to Playwright", () => {
    const p = { server: "http://h:8080", username: "u", password: "p" };
    expect(resolveProxy(p)).toEqual({ args: [], proxy: p });
  });
});


// --------------------------------------------------------------------------- web bluetooth
// Web Bluetooth is compiled in but runtime-disabled on Linux only, so a Linux host serving a
// Windows persona exposed navigator.usb/serial/hid but not navigator.bluetooth -- a combination
// no real Windows Chrome produces. The flag restores it; off Linux it must stay a no-op.
describe("webBluetoothArgs", () => {
  const realPlatform = process.platform;
  const setPlatform = (p: string) =>
    Object.defineProperty(process, "platform", { value: p, configurable: true });
  afterEach(() => setPlatform(realPlatform));

  it("emits the flag on linux", () => {
    setPlatform("linux");
    expect(webBluetoothArgs()).toEqual(["--enable-features=WebBluetooth"]);
  });

  it("is a no-op off linux", () => {
    for (const p of ["win32", "darwin"]) {
      setPlatform(p);
      expect(webBluetoothArgs()).toEqual([]);
    }
  });

  it("folds into a single --enable-features", () => {
    setPlatform("linux");
    const merged = mergeFeatureFlags([
      ...webBluetoothArgs(),
      "--enable-features=SomethingElse",
    ]);
    const enables = merged.filter((a) => a.startsWith("--enable-features="));
    expect(enables).toHaveLength(1);
    expect(enables[0]).toContain("WebBluetooth");
    expect(enables[0]).toContain("SomethingElse");
  });
});
