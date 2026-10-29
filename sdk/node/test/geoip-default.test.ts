// geoip on by default (0.66.0): mode selection, fail-open default, no-proxy cache, open-build WebRTC IP.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  applyGeoipMode, dropAutoWebrtcIp, geoipMode, geoResolver, GeoipError, resetDirectGeoCache,
  type GeoFillable, type GeoResult,
} from "../src/geoip.js";

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
  it("drops a default-filled WebRTC IP only on an open-build launch through a proxy", () => {
    const fp = (): GeoFillable => ({ timezone: "Europe/Berlin", webrtcIp: "203.0.113.7" });
    let f = fp(); dropAutoWebrtcIp(f, { autoWebrtcIp: true }, PROXY, false); expect(f.webrtcIp).toBeUndefined();
    expect(f.timezone).toBe("Europe/Berlin");
    f = fp(); dropAutoWebrtcIp(f, { autoWebrtcIp: true }, PROXY, true); expect(f.webrtcIp).toBe("203.0.113.7");
    f = fp(); dropAutoWebrtcIp(f, { autoWebrtcIp: true }, undefined, false); expect(f.webrtcIp).toBe("203.0.113.7");
    f = fp(); dropAutoWebrtcIp(f, { autoWebrtcIp: false }, PROXY, false); expect(f.webrtcIp).toBe("203.0.113.7");
  });
});
