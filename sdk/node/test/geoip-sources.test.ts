// Where geoip asks: the IP echo (CLEARCOTE_GEOIP_ECHO_URL or the public echoes) and, only when opted in,
// ip-api.com. Mirrors the Python test_geoip_* tests in test_parity_network.py.
import { describe, it, expect, afterEach, vi } from "vitest";
import { IPECHO_URLS, echoUrls, geoDb, geoUrls, ipFromEcho, ipapiEnabled, resolveGeoDetailed, type DbInfo } from "../src/geoip.js";
import { startOrigin, type Started } from "./helpers/proxies.js";

const savedEcho = [...IPECHO_URLS];
const savedApi = geoUrls.ipApi;
const savedEnv = { CLEARCOTE_GEOIP_ECHO_URL: process.env.CLEARCOTE_GEOIP_ECHO_URL, CLEARCOTE_GEOIP_IPAPI: process.env.CLEARCOTE_GEOIP_IPAPI };
const started: Started<unknown>[] = [];
afterEach(async () => {
  IPECHO_URLS.splice(0, IPECHO_URLS.length, ...savedEcho);
  geoUrls.ipApi = savedApi;
  for (const [k, v] of Object.entries(savedEnv)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  vi.restoreAllMocks();
  for (const s of started.splice(0)) await s.close();
});

/** A local origin answering as an IP echo (/echo, /myecho) and as ip-api (/ipapi). */
async function geoOrigin() {
  const origin = await startOrigin((req) => ({
    body: (req.url ?? "").includes("echo") ? "203.0.113.7"
      : JSON.stringify({ status: "success", countryCode: "JP", timezone: "Asia/Tokyo", lat: 1, lon: 2, query: "203.0.113.7" }),
  }));
  started.push(origin as Started<unknown>);
  return origin;
}

describe("geoip sources", () => {
  it("never asks ip-api.com unless CLEARCOTE_GEOIP_IPAPI opts in", async () => {
    // ip-api.com's free endpoint is for non-commercial use only (45 requests/min per IP): it is not in the
    // default chain any more. Without the database there is no answer, and the reason says how to get one.
    const origin = await geoOrigin();
    IPECHO_URLS.splice(0, IPECHO_URLS.length, `http://localhost:${origin.port}/echo`);
    geoUrls.ipApi = `http://localhost:${origin.port}/ipapi`;
    delete process.env.CLEARCOTE_GEOIP_ECHO_URL;
    delete process.env.CLEARCOTE_GEOIP_IPAPI;
    vi.spyOn(geoDb, "lookup").mockImplementation(async (_ip: string, _d: number, _q?: boolean, info: DbInfo = {}) => {
      info.db = "downloading";
      return null;
    });
    let r = await resolveGeoDetailed(undefined, { quiet: true, timeoutMs: 5000 });
    expect(r.geo).toBeNull();
    expect(r.reason).toContain("still downloading");
    expect(r.reason).toContain("CLEARCOTE_GEOIP_IPAPI=1");
    expect(origin.log.map((e) => e.url)).toEqual(["/echo"]);
    process.env.CLEARCOTE_GEOIP_IPAPI = "1";
    r = await resolveGeoDetailed(undefined, { quiet: true, timeoutMs: 5000 });
    expect(r.reason).toBeUndefined();
    expect(r.geo?.timezone).toBe("Asia/Tokyo");
    expect(origin.log.map((e) => e.url)).toEqual(["/echo", "/echo", "/ipapi"]);
  });

  it("CLEARCOTE_GEOIP_ECHO_URL replaces the public echoes", async () => {
    const origin = await geoOrigin();
    IPECHO_URLS.splice(0, IPECHO_URLS.length, "http://127.0.0.1:1/never");
    process.env.CLEARCOTE_GEOIP_ECHO_URL = ` http://localhost:${origin.port}/myecho ,`;
    const seen: string[] = [];
    vi.spyOn(geoDb, "lookup").mockImplementation(async (ip: string) => {
      seen.push(ip);
      return { ip, country: "NL", timezone: "Europe/Amsterdam", acceptLanguage: "nl-NL" };
    });
    const r = await resolveGeoDetailed(undefined, { quiet: true, timeoutMs: 5000 });
    expect(r.reason).toBeUndefined();
    expect(r.geo?.timezone).toBe("Europe/Amsterdam");
    expect(seen).toEqual(["203.0.113.7"]);
    expect(origin.log.map((e) => e.url)).toEqual(["/myecho"]);
  });

  it("settings and echo answers", () => {
    expect(echoUrls({})).toEqual(["https://api.ipify.org", "https://icanhazip.com"]);
    expect(echoUrls({}).some((u) => u.includes("ip-api"))).toBe(false);
    expect(echoUrls({ CLEARCOTE_GEOIP_ECHO_URL: "http://a/ip, http://b/ip" })).toEqual(["http://a/ip", "http://b/ip"]);
    expect(echoUrls({ CLEARCOTE_GEOIP_ECHO_URL: " , " })).toEqual(IPECHO_URLS);
    expect(ipapiEnabled({})).toBe(false);
    for (const v of ["1", "true", "YES", "on"]) expect(ipapiEnabled({ CLEARCOTE_GEOIP_IPAPI: v })).toBe(true);
    expect(ipapiEnabled({ CLEARCOTE_GEOIP_IPAPI: "0" })).toBe(false);
    expect(ipFromEcho("203.0.113.7\n")).toBe("203.0.113.7");
    expect(ipFromEcho('{"ip": "2001:db8::1"}')).toBe("2001:db8::1");
    expect(ipFromEcho('{"query": "203.0.113.8", "status": "success"}')).toBe("203.0.113.8");
    expect(ipFromEcho("{not json")).toBe("");
  });
});
