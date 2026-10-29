// pro-r54-06: real STUN without a proxy (--webrtc-direct-stun), only for this machine's own WebRTC IP, only on r54+.
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { directStunEligible, type GeoFillable } from "../src/geoip.js";
import { webrtcDirectStunArgs } from "../src/launchopts.js";

const dir = mkdtempSync(join(tmpdir(), "cc-direct-stun-"));
const r54 = join(dir, "r54.bin"); writeFileSync(r54, Buffer.from("xx\0webrtc-direct-stun\0yy", "latin1"));
const r53 = join(dir, "r53.bin"); writeFileSync(r53, Buffer.from("xx\0socks5-udp\0yy", "latin1"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));

describe("directStunEligible", () => {
  const own = "203.0.113.7";
  it("only with no proxy and the WebRTC IP the lookup found", () => {
    const fp: GeoFillable = { webrtcIp: own };
    expect(directStunEligible(fp, { autoWebrtcIp: true, ip: own }, undefined)).toBe(true);
    expect(directStunEligible(fp, { autoWebrtcIp: false, ip: own }, undefined)).toBe(true);  // geoip: true
    expect(directStunEligible(fp, { autoWebrtcIp: true, ip: own }, { server: "http://p:8080" })).toBe(false);
    expect(directStunEligible({ webrtcIp: "198.51.100.9" }, { autoWebrtcIp: false, ip: own }, undefined)).toBe(false);
    expect(directStunEligible({}, { autoWebrtcIp: false, ip: own }, undefined)).toBe(false);
    expect(directStunEligible(fp, { autoWebrtcIp: false }, undefined)).toBe(false);  // geoip off, hand-set IP
  });
});

describe("webrtcDirectStunArgs", () => {
  it("emits the switch only on an engine that has it", () => {
    expect(webrtcDirectStunArgs(r54, true, [])).toEqual(["--webrtc-direct-stun"]);
    expect(webrtcDirectStunArgs(r53, true, [])).toEqual([]);
    expect(webrtcDirectStunArgs(undefined, true, [])).toEqual([]);
    expect(webrtcDirectStunArgs(r54, false, [])).toEqual([]);
  });
  it("never alongside a proxy in the caller's own args", () => {
    expect(webrtcDirectStunArgs(r54, true, ["--proxy-server=http://p:8080"])).toEqual([]);
    expect(webrtcDirectStunArgs(r54, true, ["--proxy-pac-url=http://x/p.pac"])).toEqual([]);
  });
});
