import { describe, it, expect, afterEach } from "vitest";
import * as http from "node:http";
import * as net from "node:net";
import { request as pwRequest } from "playwright-core";
import { resolveProxy } from "../src/launchopts.js";
import {
  ipv6Bytes, isLoopback, parseHostPort, requestProxyArgs, requestProxyUpstream, routeRequestApi, startRequestProxy,
  type RequestProxy, type Upstream,
} from "../src/reqproxy.js";

const cleanups: Array<() => Promise<unknown> | void> = [];
afterEach(async () => { while (cleanups.length) await cleanups.pop()!(); });

/** Non-loopback names the fake proxies route to this machine (the forwarder sends loopback direct). */
const FAKE: Record<string, string> = { "origin.test": "127.0.0.1", "10.255.255.1": "127.0.0.1" };
const resolveFake = (h: string) => FAKE[h] ?? h;

function listen(server: net.Server): Promise<number> {
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve((server.address() as net.AddressInfo).port)));
}
function closeServer(server: net.Server): Promise<void> {
  return new Promise((resolve) => { server.close(() => resolve()); (server as http.Server).closeAllConnections?.(); });
}

/** An origin that echoes the request path. */
async function origin(): Promise<number> {
  const srv = http.createServer((req, res) => { res.end(`hello ${req.url}`); });
  const port = await listen(srv);
  cleanups.push(() => closeServer(srv));
  return port;
}

/** An HTTP CONNECT proxy that demands Basic auth and logs what it saw. */
async function authHttpProxy(user: string, pass: string): Promise<{ port: number; log: string[] }> {
  const log: string[] = [];
  const want = "Basic " + Buffer.from(`${user}:${pass}`, "utf8").toString("base64");
  const srv = http.createServer((_q, r) => { r.statusCode = 405; r.end(); });
  srv.on("connect", (req, client, head) => {
    const ok = req.headers["proxy-authorization"] === want;
    log.push(`${req.url} auth=${ok ? "ok" : "bad"}`);
    if (!ok) { client.end("HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n"); return; }
    const [h, p] = String(req.url).split(":");
    const up = net.connect(Number(p), resolveFake(h), () => {
      client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length) up.write(head);
      up.pipe(client); client.pipe(up);
    });
    up.on("error", () => client.destroy());
    client.on("error", () => up.destroy());
  });
  const port = await listen(srv);
  cleanups.push(() => closeServer(srv));
  return { port, log };
}

/** A SOCKS5 proxy that only accepts username/password auth, logging the target it was asked for. */
async function authSocks5(user: string, pass: string): Promise<{ port: number; log: string[] }> {
  const log: string[] = [];
  const srv = net.createServer((c) => {
    c.on("error", () => c.destroy());
    let stage = 0;
    let buf = Buffer.alloc(0);
    const onData = (d: Buffer) => {
      buf = Buffer.concat([buf, d]);
      if (stage === 0 && buf.length >= 2 && buf.length >= 2 + buf[1]) {
        const methods = [...buf.subarray(2, 2 + buf[1])];
        buf = buf.subarray(2 + buf[1]);
        if (!methods.includes(2)) { c.end(Buffer.from([5, 0xff])); return; }
        c.write(Buffer.from([5, 2])); stage = 1;
      }
      if (stage === 1 && buf.length >= 2) {
        const ul = buf[1]; if (buf.length < 3 + ul) return;
        const pl = buf[2 + ul]; if (buf.length < 3 + ul + pl) return;
        const u = buf.subarray(2, 2 + ul).toString("utf8");
        const p = buf.subarray(3 + ul, 3 + ul + pl).toString("utf8");
        buf = buf.subarray(3 + ul + pl);
        const ok = u === user && p === pass;
        log.push(`auth=${ok ? "ok" : "bad"}`);
        if (!ok) { c.end(Buffer.from([1, 1])); return; }
        c.write(Buffer.from([1, 0])); stage = 2;
      }
      if (stage === 2 && buf.length >= 5) {
        const atyp = buf[3];
        let host: string; let off: number;
        if (atyp === 1) { if (buf.length < 10) return; host = [...buf.subarray(4, 8)].join("."); off = 8; }
        else if (atyp === 3) { const l = buf[4]; if (buf.length < 7 + l) return; host = buf.subarray(5, 5 + l).toString(); off = 5 + l; }
        else { c.end(Buffer.from([5, 8, 0, 1, 0, 0, 0, 0, 0, 0])); return; }
        const port = buf.readUInt16BE(off);
        buf = buf.subarray(off + 2);
        log.push(`connect ${atyp === 3 ? "name" : "ip"} ${host}:${port}`);
        c.off("data", onData);
        const up = net.connect(port, resolveFake(host), () => {
          c.write(Buffer.from([5, 0, 0, 1, 127, 0, 0, 1, 0, 0]));
          if (buf.length) up.write(buf);
          up.pipe(c); c.pipe(up);
        });
        up.on("error", () => c.end(Buffer.from([5, 5, 0, 1, 0, 0, 0, 0, 0, 0])));
      }
    };
    c.on("data", onData);
  });
  const port = await listen(srv);
  cleanups.push(() => closeServer(srv));
  return { port, log };
}

async function forwarder(up: Upstream, bypass?: string): Promise<RequestProxy> {
  const rp = await startRequestProxy(up, bypass);
  cleanups.push(() => rp.close());
  return rp;
}

describe("requestProxyUpstream", () => {
  it("is null unless resolveProxy moved credentials to the engine", () => {
    expect(requestProxyUpstream(undefined, [])).toBeNull();
    const plain = { server: "http://h:3128" };
    expect(requestProxyUpstream(plain, resolveProxy(plain, true).args)).toBeNull();
    // an old engine without --proxy-auth leaves http credentials with Playwright, which proxies the request API itself
    const http1 = { server: "http://h:3128", username: "u", password: "p" };
    expect(requestProxyUpstream(http1, resolveProxy(http1, false).args)).toBeNull();
  });
  it("reads http, https and socks5 upstreams with decoded URL credentials", () => {
    const h = { server: "http://us%40er:p%3Ass@h:3128" };
    expect(requestProxyUpstream(h, resolveProxy(h, true).args)).toEqual({ protocol: "http", host: "h", port: 3128, username: "us@er", password: "p:ss" });
    const s = { server: "https://proxy.example", username: "u", password: "p" };
    expect(requestProxyUpstream(s, resolveProxy(s, true).args)).toMatchObject({ protocol: "https", host: "proxy.example", port: 443 });
    const k = { server: "socks5://u:p@[::1]:1080" };
    expect(requestProxyUpstream(k, resolveProxy(k).args)).toMatchObject({ protocol: "socks5", host: "::1", port: 1080 });
  });
  it("leaves socks4 alone (no password auth to add)", () => {
    const k = { server: "socks4://u:p@h:1080" };
    expect(requestProxyUpstream(k, resolveProxy(k).args)).toBeNull();
  });
});

describe("requestProxyArgs", () => {
  it("adds an empty bypass list to override Playwright's <-loopback>, unless the caller set one", () => {
    expect(requestProxyArgs(["--proxy-server=http://h:1", "--proxy-auth=u:p"])).toEqual(["--proxy-bypass-list="]);
    expect(requestProxyArgs(["--proxy-server=http://h:1", "--proxy-bypass-list=*.internal"])).toEqual([]);
  });
});

describe("parsing helpers", () => {
  it("parses CONNECT targets", () => {
    expect(parseHostPort("example.com:443")).toEqual({ host: "example.com", port: 443 });
    expect(parseHostPort("[2001:db8::1]:8443")).toEqual({ host: "2001:db8::1", port: 8443 });
    expect(parseHostPort("example.com")).toBeNull();
    expect(parseHostPort("h:0")).toBeNull();
    expect(parseHostPort("h:70000")).toBeNull();
  });
  it("expands IPv6 literals", () => {
    expect(ipv6Bytes("::1")!.toString("hex")).toBe("00000000000000000000000000000001");
    expect(ipv6Bytes("2001:db8::8:1")!.toString("hex")).toBe("20010db8000000000000000000080001");
    expect(ipv6Bytes("::ffff:1.2.3.4")).toBeNull();
    expect(ipv6Bytes("1::2::3")).toBeNull();
  });
});

describe("forwarder", () => {
  it("tunnels Playwright's request API through an authenticated HTTP proxy", async () => {
    const o = await origin();
    const up = await authHttpProxy("u1", "p@ss:w0rd#");
    const rp = await forwarder({ protocol: "http", host: "127.0.0.1", port: up.port, username: "u1", password: "p@ss:w0rd#" });
    expect(rp.playwrightProxy.server).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
    const ctx = await pwRequest.newContext({ proxy: rp.playwrightProxy });
    cleanups.push(() => ctx.dispose());
    const res = await ctx.get(`http://origin.test:${o}/via-forwarder`);
    expect(await res.text()).toBe("hello /via-forwarder");
    expect(up.log).toEqual([`origin.test:${o} auth=ok`]);
  });

  it("tunnels through an authenticated SOCKS5 proxy, which resolves names itself", async () => {
    const o = await origin();
    const up = await authSocks5("sockuser", "s3cr3t:é");
    const rp = await forwarder({ protocol: "socks5", host: "127.0.0.1", port: up.port, username: "sockuser", password: "s3cr3t:é" });
    const ctx = await pwRequest.newContext({ proxy: rp.playwrightProxy });
    cleanups.push(() => ctx.dispose());
    expect(await (await ctx.get(`http://origin.test:${o}/socks`)).text()).toBe("hello /socks");
    expect(await (await ctx.get(`http://10.255.255.1:${o}/ip`)).text()).toBe("hello /ip");
    expect(up.log).toEqual(["auth=ok", `connect name origin.test:${o}`, "auth=ok", `connect ip 10.255.255.1:${o}`]);
  });

  it("answers 502 naming the cause when the upstream refuses the credentials", async () => {
    const o = await origin();
    const up = await authHttpProxy("u1", "right");
    const rp = await forwarder({ protocol: "http", host: "127.0.0.1", port: up.port, username: "u1", password: "wrong" });
    const ctx = await pwRequest.newContext({ proxy: rp.playwrightProxy });
    cleanups.push(() => ctx.dispose());
    const res = await ctx.get(`http://origin.test:${o}/x`);
    expect(res.status()).toBe(502);
    expect(res.headers()["x-clearcote-proxy-error"]).toMatch(/407/);
    expect(up.log).toEqual([`origin.test:${o} auth=bad`]);
  });

  it("connects loopback targets directly, never through the proxy (Chromium's implicit bypass)", async () => {
    const o = await origin();
    const up = await authHttpProxy("u1", "p");
    const rp = await forwarder({ protocol: "http", host: "127.0.0.1", port: up.port, username: "u1", password: "p" });
    const ctx = await pwRequest.newContext({ proxy: rp.playwrightProxy });
    cleanups.push(() => ctx.dispose());
    expect(await (await ctx.get(`http://localhost:${o}/lo`)).text()).toBe("hello /lo");
    expect(up.log).toEqual([]);
    expect(isLoopback("127.8.9.10")).toBe(true);
    expect(isLoopback("LOCALHOST.")).toBe(true);
    expect(isLoopback("app.localhost")).toBe(true);
    expect(isLoopback("[::1]".slice(1, -1))).toBe(true);
    expect(isLoopback("128.0.0.1")).toBe(false);
    expect(isLoopback("localhost.example.com")).toBe(false);
  });

  it("refuses anything but CONNECT", async () => {
    const rp = await forwarder({ protocol: "http", host: "127.0.0.1", port: 9, username: "u", password: "p" });
    const port = Number(new URL(rp.playwrightProxy.server).port);
    const reply = await new Promise<string>((resolve) => {
      const s = net.connect(port, "127.0.0.1", () => s.write("GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n"));
      let out = ""; s.on("data", (d) => { out += d; }); s.on("close", () => resolve(out));
    });
    expect(reply).toMatch(/^HTTP\/1\.1 405 /);
  });

  it("passes the caller's bypass list to Playwright, so bypassed hosts go direct as in the browser", async () => {
    const rp = await forwarder({ protocol: "http", host: "127.0.0.1", port: 9, username: "u", password: "p" }, " *.internal ");
    expect(rp.playwrightProxy.bypass).toBe("*.internal");
  });

  it("stops accepting connections after close()", async () => {
    const rp = await startRequestProxy({ protocol: "http", host: "127.0.0.1", port: 9, username: "u", password: "p" });
    const port = Number(new URL(rp.playwrightProxy.server).port);
    await rp.close();
    await expect(new Promise((resolve, reject) => {
      const s = net.connect(port, "127.0.0.1", () => { s.destroy(); resolve("connected"); });
      s.on("error", reject);
    })).rejects.toThrow();
  });
});

describe("routeRequestApi", () => {
  it("starts a forwarder only when credentials went to the engine", async () => {
    const plain = { server: "http://h:3128" };
    expect(await routeRequestApi(plain, resolveProxy(plain, true).args, true)).toBeNull();
    const authed = { server: "http://u:p@h:3128" };
    const route = await routeRequestApi(authed, resolveProxy(authed, true).args, true);
    expect(route).not.toBeNull();
    cleanups.push(() => route!.proxy.close());
    expect(route!.args).toEqual(["--proxy-bypass-list="]);
    expect(route!.proxy.playwrightProxy).not.toHaveProperty("username");
  });
});
