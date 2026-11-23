// geoip on by default (0.66.0): mode selection, fail-open default, no-proxy cache, open-build WebRTC IP.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  applyGeoipMode, dropAutoWebrtcIp, geoipMode, geoResolver, GeoipError, resetDirectGeoCache,
  type GeoFillable, type GeoResult,
} from "../src/geoip.js";
import { resetSeenWarnings } from "../src/warnings.js";

const GEO = { ip: "203.0.113.7", country: "DE", timezone: "Europe/Berlin", acceptLanguage: "de-DE", location: "52.52,13.405" };
const PROXY = { server: "http://proxy.example:8080" };

describe("geoipMode", () => {
  it("is auto when unset, strict for true, off for false", () => {
    expect(geoipMode(undefined, {})).toBe("auto");
    expect(geoipMode(true, {})).toBe("strict");
    expect(geoipMode(false, {})).toBe("off");
  });
  it("CLEARCOTE_GEOIP=0 turns the default off, but not an explicit true", () => {
    for (const v of ["0", "false", "OFF", "no"]) expect(geoipMode(undefined, { CLEARCOTE_GEOIP: v })).toBe("off");
    expect(geoipMode(undefined, { CLEARCOTE_GEOIP: "1" })).toBe("auto");
    expect(geoipMode(true, { CLEARCOTE_GEOIP: "0" })).toBe("strict");
  });
});

describe("applyGeoipMode", () => {
  const real = geoResolver.resolve;
  let answer: GeoResult;
  let asked: unknown[];
  beforeEach(() => {
    resetDirectGeoCache();
    answer = { geo: GEO, elapsedMs: 5 };
    asked = [];
    geoResolver.resolve = async (proxy) => { asked.push(proxy); return answer; };
  });
  afterEach(() => { geoResolver.resolve = real; vi.restoreAllMocks(); });

  it("auto with no proxy looks this machine's own IP up and fills every field", async () => {
    const fp: GeoFillable = {};
    const r = await applyGeoipMode(fp, undefined, "auto", true);
    expect(asked).toEqual([undefined]);
    expect(fp).toEqual({ timezone: "Europe/Berlin", acceptLanguage: "de-DE", location: "52.52,13.405", webrtcIp: "203.0.113.7" });
    expect(r.autoWebrtcIp).toBe(true);
  });

  it("auto reuses a no-proxy lookup, but looks a proxy up every launch", async () => {
    await applyGeoipMode({}, undefined, "auto", true);
    const fp: GeoFillable = {};
    await applyGeoipMode(fp, undefined, "auto", true);
    expect(asked).toHaveLength(1);
    expect(fp.timezone).toBe("Europe/Berlin");
    await applyGeoipMode({}, PROXY, "auto", true);
    await applyGeoipMode({}, PROXY, "auto", true);
    expect(asked).toEqual([undefined, PROXY, PROXY]);
  });

  it("auto keeps what the caller set and skips the lookup when nothing is left to fill", async () => {
    const fp: GeoFillable = { timezone: "Asia/Tokyo" };
    await applyGeoipMode(fp, PROXY, "auto", true);
    expect(fp.timezone).toBe("Asia/Tokyo");
    expect(fp.acceptLanguage).toBe("de-DE");
    asked = [];
    await applyGeoipMode({ timezone: "a", acceptLanguage: "b", location: "1,2", webrtcIp: "198.51.100.1" }, PROXY, "auto", true);
    expect(asked).toEqual([]);
  });

  it("auto warns and launches when the region cannot be resolved", async () => {
    answer = { geo: null, reason: "could not determine the exit IP", elapsedMs: 5 };
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    const fp: GeoFillable = {};
    const r = await applyGeoipMode(fp, PROXY, "auto", false);
    expect(fp).toEqual({});
    expect(r.autoWebrtcIp).toBe(false);
    const out = err.mock.calls.map((c) => String(c[0])).join("");
    expect(out).toContain("geoip (on by default)");
    expect(out).toContain("geoip: false");
  });

  it("strict (geoip: true) reports a WebRTC IP it filled as looked up, and one the caller set as not", async () => {
    const fp: GeoFillable = {};
    const r = await applyGeoipMode(fp, PROXY, "strict", true);
    expect(fp.webrtcIp).toBe("203.0.113.7");
    expect(r.autoWebrtcIp).toBe(true);
    const set: GeoFillable = { webrtcIp: "198.51.100.9" };
    expect((await applyGeoipMode(set, PROXY, "strict", true)).autoWebrtcIp).toBe(false);
    expect(set.webrtcIp).toBe("198.51.100.9");
  });

  it("strict (geoip: true) still fails closed", async () => {
    answer = { geo: null, reason: "could not determine the exit IP", elapsedMs: 5 };
    await expect(applyGeoipMode({}, PROXY, "strict", true)).rejects.toBeInstanceOf(GeoipError);
  });

  it("off asks nothing", async () => {
    const fp: GeoFillable = {};
    await applyGeoipMode(fp, PROXY, "off", true);
    expect(asked).toEqual([]);
    expect(fp).toEqual({});
  });
});

describe("dropAutoWebrtcIp", () => {
  const fp = (): GeoFillable => ({ timezone: "Europe/Berlin", acceptLanguage: "de-DE", location: "52.52,13.405", webrtcIp: "203.0.113.7" });
  beforeEach(() => { resetSeenWarnings(); delete process.env.CLEARCOTE_NO_WARN; });
  afterEach(() => vi.restoreAllMocks());

  it("drops a looked-up WebRTC IP behind a proxy on the licensed and the open build, and keeps the region", () => {
    for (const licensed of [true, false]) {
      const f = fp(); dropAutoWebrtcIp(f, { autoWebrtcIp: true }, PROXY, licensed, undefined, true);
      expect(f).toEqual({ timezone: "Europe/Berlin", acceptLanguage: "de-DE", location: "52.52,13.405" });
    }
  });

  it("keeps a looked-up WebRTC IP when the SOCKS5 UDP relay carries UDP, on the licensed build only", () => {
    let f = fp(); dropAutoWebrtcIp(f, { autoWebrtcIp: true }, { server: "socks5://1.2.3.4:1080" }, true, true, true);
    expect(f.webrtcIp).toBe("203.0.113.7");
    // the open build has no relay: dropped as before
    f = fp(); dropAutoWebrtcIp(f, { autoWebrtcIp: true }, { server: "socks5://1.2.3.4:1080" }, false, true, true);
    expect(f.webrtcIp).toBeUndefined();
    // a relay the proxy refused (udpRelayOrFallback resolved it to false): dropped
    f = fp(); dropAutoWebrtcIp(f, { autoWebrtcIp: true }, { server: "socks5://1.2.3.4:1080" }, true, false, true);
    expect(f.webrtcIp).toBeUndefined();
  });

  it("keeps the WebRTC IP with no proxy (this machine's own egress)", () => {
    for (const licensed of [true, false]) {
      const f = fp(); dropAutoWebrtcIp(f, { autoWebrtcIp: true }, undefined, licensed, undefined, true);
      expect(f.webrtcIp).toBe("203.0.113.7");
    }
  });

  it("keeps a WebRTC IP the caller set behind a proxy, and warns once that it can be proven fake", () => {
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    const f = fp(); dropAutoWebrtcIp(f, { autoWebrtcIp: false }, PROXY, true);
    expect(f.webrtcIp).toBe("203.0.113.7");
    dropAutoWebrtcIp(fp(), { autoWebrtcIp: false }, PROXY, true);  // once per process
    const out = err.mock.calls.map((c) => String(c[0])).join("");
    expect(out.match(/webrtcIp 203\.0\.113\.7 is set with a proxy/g)).toHaveLength(1);
    expect(out).toContain("sites that run their own STUN servers can prove it fake");
  });

  it("does not warn with the relay, without a proxy, or when quiet / CLEARCOTE_NO_WARN", () => {
    const err = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    dropAutoWebrtcIp(fp(), { autoWebrtcIp: false }, { server: "socks5://1.2.3.4:1080" }, true, true);
    dropAutoWebrtcIp(fp(), { autoWebrtcIp: false }, undefined, true);
    dropAutoWebrtcIp(fp(), { autoWebrtcIp: false }, PROXY, true, undefined, true);
    process.env.CLEARCOTE_NO_WARN = "1";
    dropAutoWebrtcIp(fp(), { autoWebrtcIp: false }, PROXY, true);
    delete process.env.CLEARCOTE_NO_WARN;
    expect(err).not.toHaveBeenCalled();
  });
});
