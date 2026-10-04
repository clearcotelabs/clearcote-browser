import { describe, it, expect, vi, afterEach } from "vitest";
import {
  extensionArgs,
  resolveProxy,
  engineSupportsSwitch,
  warnUnsupportedEngineOptions,
  mergeFeatureFlags,
  webBluetoothArgs,
  privacySandboxArgs,
  quicArgs,
  socks5UdpArgs,
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

  it("routes a credentialed http proxy to --proxy-server + --proxy-auth (engine answers the 407)", () => {
    const r = resolveProxy({ server: "http://old:secret@h:3128", username: "u", password: "p" }, true);
    expect(r.args).toEqual(["--proxy-server=http://h:3128", "--proxy-auth=u:p"]);
    expect(r.proxy).toBeUndefined();
  });

  it("keeps the bypass list when it owns an https proxy", () => {
    const r = resolveProxy({ server: "https://h:443", username: "u", password: "p", bypass: "*.internal" }, true);
    expect(r.args).toEqual(["--proxy-server=https://h:443", "--proxy-auth=u:p", "--proxy-bypass-list=*.internal"]);
  });

  it("leaves credentialed http proxies to Playwright on engines without --proxy-auth (r18 and earlier, free)", () => {
    // Routing them anyway would strip the credentials from Playwright and every request would 407.
    const proxy = { server: "http://h:3128", username: "u", password: "p" };
    expect(resolveProxy(proxy)).toEqual({ args: [], proxy });
    expect(resolveProxy(proxy, false)).toEqual({ args: [], proxy });
  });

  // Regression (found 2026-09-24, proven at runtime on r27): credentials written INTO the URL were
  // ignored, because only the fields were read. The proxy went to Playwright, which rebuilds the
  // server as scheme://host:port, and the browser's SOCKS5 greeting offered only "no auth".
  it("routes credentials written in a SOCKS5 URL to the engine, like the fields", () => {
    const r = resolveProxy({ server: "socks5://user:pass@h:1080" });
    expect(r.args).toEqual(["--proxy-server=socks5://h:1080", "--socks5-credentials=user:pass"]);
    expect(r.proxy).toBeUndefined();
  });

  it("percent-decodes URL credentials and splits at the last '@'", () => {
    expect(resolveProxy({ server: "socks5://us%40er:p%3Ass%2Fw@h:1080" }).args)
      .toEqual(["--proxy-server=socks5://h:1080", "--socks5-credentials=us@er:p:ss/w"]);
    // an unescaped '@' in the password (URL parsers split userinfo at the LAST '@')
    expect(resolveProxy({ server: "socks5://user:p@ss@h:1080" }).args)
      .toEqual(["--proxy-server=socks5://h:1080", "--socks5-credentials=user:p@ss"]);
    // a malformed escape is taken literally rather than throwing
    expect(resolveProxy({ server: "socks5://user:100%@h:1080" }).args)
      .toEqual(["--proxy-server=socks5://h:1080", "--socks5-credentials=user:100%"]);
  });

  it("routes credentials written in an http URL to --proxy-auth on engines that have it", () => {
    const r = resolveProxy({ server: "http://u:p@h:3128", bypass: "*.internal" }, true);
    expect(r.args).toEqual(["--proxy-server=http://h:3128", "--proxy-auth=u:p", "--proxy-bypass-list=*.internal"]);
    expect(r.proxy).toBeUndefined();
  });

  it("hands URL credentials to Playwright as fields when it keeps an http proxy", () => {
    // Playwright drops userinfo from `server`: left there, every request would 407.
    const r = resolveProxy({ server: "http://u:p%21@h:3128", bypass: "*.internal" }, false);
    expect(r.args).toEqual([]);
    expect(r.proxy).toEqual({ server: "http://h:3128", username: "u", password: "p!", bypass: "*.internal" });
  });

  it("lets the fields win over userinfo, per field", () => {
    expect(resolveProxy({ server: "socks5://urluser:urlpass@h:1080", password: "field" }).args)
      .toEqual(["--proxy-server=socks5://h:1080", "--socks5-credentials=urluser:field"]);
  });

  it("warns about an engine without SOCKS5 auth when the credentials are in the URL", async () => {
    const fs = await import("node:fs"); const os = await import("node:os"); const path = await import("node:path");
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cc-warn-"));
    try {
      const noSocksAuth = path.join(dir, "r16"); fs.writeFileSync(noSocksAuth, Buffer.from("\0proxy-server\0", "latin1"));
      const msgs = warnUnsupportedEngineOptions(noSocksAuth, {}, { server: "socks5://u:p@h:1080" }, true);
      expect(msgs.some((m) => m.includes("cannot authenticate to a SOCKS5 proxy"))).toBe(true);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("socks5 routing does not depend on the --proxy-auth capability", () => {
    const proxy = { server: "socks5://h:1080", username: "u", password: "p" };
    expect(resolveProxy(proxy).args).toEqual(resolveProxy(proxy, true).args);
    expect(resolveProxy(proxy).args.some((a) => a.startsWith("--socks5-credentials="))).toBe(true);
  });

  it("warnUnsupportedEngineOptions warns only for switches the engine lacks", async () => {
    const fs = await import("node:fs"); const os = await import("node:os"); const path = await import("node:path");
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cc-warn-"));
    const oldExe = path.join(dir, "old"); fs.writeFileSync(oldExe, Buffer.from("\0socks5-credentials\0", "latin1"));
    const newExe = path.join(dir, "new"); fs.writeFileSync(newExe, Buffer.from("\0socks5-credentials\0\0fingerprint-schema\0\0fingerprint-gpu-backend-real\0\0proxy-auth\0", "latin1"));
    const fp = { personaSchema: 2, realGpuHost: true };
    const socks = { server: "socks5://h:1080", username: "u", password: "p" };
    const oldMsgs = warnUnsupportedEngineOptions(oldExe, fp, socks, true);
    expect(oldMsgs.some((m) => m.includes("personaSchema: 2"))).toBe(true);
    expect(oldMsgs.some((m) => m.includes("realGpuHost"))).toBe(true);
    expect(oldMsgs.some((m) => m.includes("SOCKS5"))).toBe(false);
    expect(warnUnsupportedEngineOptions(newExe, fp, socks, true)).toEqual([]);
    expect(warnUnsupportedEngineOptions(oldExe, { personaSchema: 1 }, undefined, true)).toEqual([]);
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("engineSupportsSwitch probes the NUL-delimited switch literal, not header names", async () => {
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cc-switch-"));
    const a = path.join(dir, "a.bin"); fs.writeFileSync(a, Buffer.from("xx\0proxy-authenticate\0yy", "latin1"));
    const b = path.join(dir, "b.bin"); fs.writeFileSync(b, Buffer.from("xx\0proxy-auth\0yy", "latin1"));
    expect(engineSupportsSwitch(a, "proxy-auth")).toBe(false);
    expect(engineSupportsSwitch(b, "proxy-auth")).toBe(true);
    expect(engineSupportsSwitch(path.join(dir, "missing"), "proxy-auth")).toBe(false);
    expect(engineSupportsSwitch(undefined, "proxy-auth")).toBe(false);
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("leaves an http proxy without creds to Playwright", () => {
    const proxy = { server: "http://h:3128" };
    expect(resolveProxy(proxy)).toEqual({ args: [], proxy });
  });

  it("leaves a SOCKS5 proxy without creds to Playwright", () => {
    const p = { server: "socks5://h:1080" };
    expect(resolveProxy(p)).toEqual({ args: [], proxy: p });
  });

  it("no longer leaves an authed HTTP proxy to Playwright when the engine implements --proxy-auth", () => {
    const p = { server: "http://h:8080", username: "u", password: "p" };
    const r = resolveProxy(p, true);
    expect(r.proxy).toBeUndefined();
    expect(r.args).toContain("--proxy-auth=u:p");
  });
});


// --------------------------------------------------------------------------- web bluetooth
// Web Bluetooth is compiled in but runtime-disabled on Linux only, so a Linux host serving a
// Windows persona exposed navigator.usb/serial/hid but not navigator.bluetooth -- a combination
// no real Windows Chrome produces. The flag restores it; off Linux it must stay a no-op.
// It is keyed on the CLAIMED platform: under a LINUX claim genuine Chrome 154 has no
// navigator.bluetooth either (measured 2026-10-04), so adding it there is a tell the other way.
describe("webBluetoothArgs", () => {
  const realPlatform = process.platform;
  const setPlatform = (p: string) =>
    Object.defineProperty(process, "platform", { value: p, configurable: true });
  afterEach(() => setPlatform(realPlatform));

  it("emits the flag on linux for a desktop claim", () => {
    setPlatform("linux");
    for (const claimed of ["windows", "macos", "android", "chromeos"]) {
      expect(webBluetoothArgs(claimed)).toEqual(["--enable-features=WebBluetooth"]);
    }
  });

  it("withholds the flag under a linux claim", () => {
    setPlatform("linux");
    expect(webBluetoothArgs("linux")).toEqual([]);
    expect(webBluetoothArgs()).toEqual([]); // bare call is conservative: host claims linux
  });

  it("is a no-op off linux", () => {
    for (const p of ["win32", "darwin"]) {
      setPlatform(p);
      for (const claimed of [undefined, "windows", "linux"]) {
        expect(webBluetoothArgs(claimed)).toEqual([]);
      }
    }
  });

  it("folds into a single --enable-features", () => {
    setPlatform("linux");
    const merged = mergeFeatureFlags([
      ...webBluetoothArgs("windows"),
      "--enable-features=SomethingElse",
    ]);
    const enables = merged.filter((a) => a.startsWith("--enable-features="));
    expect(enables).toHaveLength(1);
    expect(enables[0]).toContain("WebBluetooth");
    expect(enables[0]).toContain("SomethingElse");
  });
});

describe("socks5UdpArgs", () => {
  const socks5 = { server: "socks5://gw.example.com:1080", username: "u", password: "p" };

  it("emits the switch for a socks5 proxy when opted in", () => {
    expect(socks5UdpArgs(true, socks5)).toEqual(["--socks5-udp"]);
  });

  it("is off unless explicitly opted in", () => {
    for (const v of [undefined, false]) expect(socks5UdpArgs(v, socks5)).toEqual([]);
  });

  // UDP ASSOCIATE is a SOCKS5 command. Emitting the switch for a transport that cannot carry a
  // datagram would be accepted and silently do nothing -- the failure mode this guards against.
  it("stays silent for schemes that cannot relay UDP", () => {
    for (const server of ["http://p:8080", "https://p:8443", "socks4://p:1080"]) {
      expect(socks5UdpArgs(true, { server })).toEqual([]);
    }
  });

  it("stays silent with no proxy at all", () => {
    expect(socks5UdpArgs(true, undefined)).toEqual([]);
    expect(socks5UdpArgs(true, { server: "" })).toEqual([]);
  });

  it("accepts socks5h and is case-insensitive", () => {
    expect(socks5UdpArgs(true, { server: "socks5h://p:1080" })).toEqual(["--socks5-udp"]);
    expect(socks5UdpArgs(true, { server: "SOCKS5://p:1080" })).toEqual(["--socks5-udp"]);
  });

  // Verified against the proxy's own log: the association is established with the deny policy in
  // force, so the two are additive and enabling UDP does not mean weakening the leak default.
  it("composes with the webrtc deny default rather than replacing it", () => {
    const args = [...socks5UdpArgs(true, socks5)];
    const withDefault = [...args, ...webrtcDefaultDenyArgs(args, undefined)];
    expect(withDefault).toContain("--socks5-udp");
    expect(withDefault).toContain("--webrtc-ip-handling-policy=disable_non_proxied_udp");
  });
});
