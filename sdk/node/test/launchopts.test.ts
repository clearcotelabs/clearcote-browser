import { describe, it, expect, vi } from "vitest";
import {
  extensionArgs,
  resolveProxy,
  mergeFeatureFlags,
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
      "--disable-features=BrowsingTopics,BrowsingTopicsDocumentAPI,Fledge,InterestGroupStorage,PrivateAggregationApi,SharedStorageAPI,FencedFrames,WebUSB",
    ]);
  });
});

describe("webrtcDefaultDenyArgs", () => {
  it("defaults to disable_non_proxied_udp when no webrtcIp", () => {
    expect(webrtcDefaultDenyArgs([], undefined)).toEqual(["--webrtc-ip-handling-policy=disable_non_proxied_udp"]);
  });
  it("is skipped when a webrtcIp is set", () => {
    expect(webrtcDefaultDenyArgs([], "1.2.3.4")).toEqual([]);
  });
  it("is skipped when the caller already set a policy", () => {
    expect(webrtcDefaultDenyArgs(["--webrtc-ip-handling-policy=default"], undefined)).toEqual([]);
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

  it("routes a credentialed SOCKS5 proxy to --proxy-server and drops it from Playwright", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const r = resolveProxy({ server: "socks5://h:1080", username: "u", password: "p" });
    expect(r.args).toEqual(["--proxy-server=socks5://h:1080"]);
    expect(r.proxy).toBeUndefined();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
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
