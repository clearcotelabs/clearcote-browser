// Playwright's request API (context.request, page.request) does not go through the browser. The
// driver sends it with Node's own HTTP client, through the proxy in Playwright's launch/context
// options (playwright-core: `proxy: this._context._options.proxy || this._context._browser.options.proxy`)
// and never through the browser's --proxy-server. A proxy WITH credentials goes to the engine's own
// switches (resolveProxy) and is removed from Playwright's options, so before this module those
// requests left from this machine's own IP: measured 2026-10-21 on SDK 0.55.1 + r46, an authenticated
// proxy logged the page's CONNECT and never saw context.request or page.request, both of which
// returned 200.
//
// The fix keeps the credentials away from Playwright (credentials there make its driver turn on Fetch
// interception and Network.setCacheDisabled for the whole context, the transport tell resolveProxy
// exists to avoid). Playwright gets a credential-less proxy on 127.0.0.1 instead; this forwarder adds
// the credentials and tunnels to the real proxy (HTTP/HTTPS CONNECT, or SOCKS5 with RFC 1929 auth and
// remote name resolution). The browser itself never uses it: Playwright puts its --proxy-server before
// the caller's args, ours comes later in the same argv and Chromium keeps the last value of a switch;
// the same holds for the --proxy-bypass-list it adds (`<-loopback>`), which requestProxyArgs overrides.
// Playwright's fetch always tunnels with CONNECT (HttpsProxyAgent), even for http:// URLs, so that is
// the one method served. Mirrors sdk/python/clearcote/_reqproxy.py and sdk/dotnet/src/Clearcote/RequestProxy.cs.
import * as net from "node:net";
import * as tls from "node:tls";
import { proxyCredentials, type PwProxy } from "./launchopts.js";

/** The real proxy the forwarder tunnels to. */
export interface Upstream {
  protocol: "http" | "https" | "socks5";
  host: string;
  port: number;
  username: string;
  password: string;
}

/** A running forwarder: the proxy to hand Playwright, and how to stop it. */
export interface RequestProxy {
  /** `http://127.0.0.1:<port>`, plus the caller's bypass list so bypassed hosts go direct as in the browser. */
  playwrightProxy: PwProxy;
  close(): Promise<void>;
}

const HEAD_LIMIT = 16 * 1024;
const HANDSHAKE_MS = 30_000;

/**
 * The upstream the forwarder needs, or null when nothing has to be forwarded: no proxy, a proxy with
 * no credentials, or one resolveProxy left with Playwright (it reaches context.request already).
 * `proxyArgs` is what resolveProxy returned; credentials moved to the engine exactly when it holds
 * a --proxy-server.
 */
export function requestProxyUpstream(proxy: PwProxy | undefined, proxyArgs: readonly string[]): Upstream | null {
  if (!proxy || !proxyArgs.some((a) => a.startsWith("--proxy-server="))) return null;
  const { server, username, password } = proxyCredentials(proxy);
  if (!username && !password) return null;
  let url: URL;
  try { url = new URL(server); } catch { return null; }
  const scheme = url.protocol.replace(/:$/, "").toLowerCase();
  const protocol = scheme === "http" || scheme === "https" ? scheme : scheme === "socks5" || scheme === "socks5h" ? "socks5" : null;
  if (!protocol || !url.hostname) return null;  // socks4 has no password auth: nothing a forwarder can add
  const port = url.port ? Number(url.port) : protocol === "https" ? 443 : protocol === "socks5" ? 1080 : 80;
  const host = url.hostname.replace(/^\[(.*)\]$/, "$1");
  return { protocol, host, port, username, password };
}

/**
 * Engine args that keep the browser's bypass list the caller's own: Playwright adds `<-loopback>`
 * (proxy even loopback) whenever it is given a proxy, and an empty --proxy-bypass-list is the same as
 * none to Chromium. Nothing to add when resolveProxy already wrote one.
 */
export function requestProxyArgs(proxyArgs: readonly string[]): string[] {
  return proxyArgs.some((a) => a.startsWith("--proxy-bypass-list=")) ? [] : ["--proxy-bypass-list="];
}

/** Start a forwarder on 127.0.0.1 (a free port) for `upstream`. */
export async function startRequestProxy(upstream: Upstream, bypass?: string): Promise<RequestProxy> {
  const sockets = new Set<net.Socket>();
  const server = net.createServer((client) => {
    sockets.add(client);
    client.on("close", () => sockets.delete(client));
    handleClient(client, upstream, sockets);
  });
  server.unref();  // never keeps a script alive on its own
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => { server.off("error", reject); resolve(); });
  });
  const { port } = server.address() as net.AddressInfo;
  const b = (bypass ?? "").trim();
  return {
    playwrightProxy: { server: `http://127.0.0.1:${port}`, ...(b ? { bypass: b } : {}) },
    close: () => new Promise<void>((resolve) => {
      for (const s of sockets) s.destroy();
      server.close(() => resolve());
    }),
  };
}

function handleClient(client: net.Socket, upstream: Upstream, sockets: Set<net.Socket>): void {
  client.setTimeout(HANDSHAKE_MS, () => client.destroy());
  client.on("error", () => client.destroy());
  readHead(client).then(async ({ head, rest }) => {
    const [method, target] = head.split("\r\n", 1)[0].split(" ");
    const dest = method === "CONNECT" ? parseHostPort(target) : null;
    if (!dest) {
      client.end("HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\nContent-Length: 0\r\n\r\n");
      return;
    }
    let remote: net.Socket;
    try {
      remote = isLoopback(dest.host) ? await direct(dest.host, dest.port) : await openTunnel(upstream, dest.host, dest.port);
    } catch (e) {
      const msg = String((e as Error)?.message ?? e).replace(/[\r\n]/g, " ").slice(0, 200);
      client.end(`HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nX-Clearcote-Proxy-Error: ${msg}\r\nContent-Length: 0\r\n\r\n`);
      return;
    }
    sockets.add(remote);
    remote.on("close", () => sockets.delete(remote));
    client.setTimeout(0);
    remote.setTimeout(0);
    client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    if (rest.length) remote.write(rest);
    remote.on("error", () => client.destroy());
    client.on("error", () => remote.destroy());
    client.pipe(remote);
    remote.pipe(client);
  }).catch(() => client.destroy());
}

/** Read up to the end of an HTTP head; `rest` is whatever arrived after it. */
function readHead(sock: net.Socket): Promise<{ head: string; rest: Buffer }> {
  return new Promise((resolve, reject) => {
    let buf = Buffer.alloc(0);
    const onData = (chunk: Buffer) => {
      buf = Buffer.concat([buf, chunk]);
      const end = buf.indexOf("\r\n\r\n");
      if (end >= 0) {
        sock.off("data", onData);
        sock.off("end", onEnd);
        sock.pause();
        resolve({ head: buf.subarray(0, end).toString("latin1"), rest: buf.subarray(end + 4) });
      } else if (buf.length > HEAD_LIMIT) {
        sock.off("data", onData);
        reject(new Error("proxy response head too large"));
      }
    };
    const onEnd = () => reject(new Error("connection closed before the end of the head"));
    sock.on("data", onData);
    sock.once("end", onEnd);
  });
}

/**
 * Loopback never goes through the proxy, as in Chromium (its implicit bypass rules): the request API
 * reached localhost directly before the forwarder existed, and a remote proxy cannot reach it anyway.
 */
export function isLoopback(host: string): boolean {
  const h = host.toLowerCase().replace(/\.$/, "");
  return h === "localhost" || h.endsWith(".localhost") || /^127\.\d+\.\d+\.\d+$/.test(h) || h === "::1" || h === "0:0:0:0:0:0:0:1";
}

function direct(host: string, port: number): Promise<net.Socket> {
  return new Promise((resolve, reject) => {
    const sock = net.connect({ host, port });
    sock.once("error", reject);
    sock.once("connect", () => { sock.off("error", reject); resolve(sock); });
  });
}

/** `host:port` or `[v6]:port`. */
export function parseHostPort(target: string | undefined): { host: string; port: number } | null {
  if (!target) return null;
  const m = /^\[([^\]]+)\]:(\d{1,5})$/.exec(target) ?? /^([^:\s]+):(\d{1,5})$/.exec(target);
  if (!m) return null;
  const port = Number(m[2]);
  return port > 0 && port < 65536 ? { host: m[1], port } : null;
}

function connect(upstream: Upstream): Promise<net.Socket> {
  return new Promise((resolve, reject) => {
    const sock = upstream.protocol === "https"
      ? tls.connect({ host: upstream.host, port: upstream.port, servername: net.isIP(upstream.host) ? undefined : upstream.host, ALPNProtocols: ["http/1.1"] })
      : net.connect({ host: upstream.host, port: upstream.port });
    const ready = upstream.protocol === "https" ? "secureConnect" : "connect";
    sock.setTimeout(HANDSHAKE_MS, () => sock.destroy(new Error(`timed out connecting to the proxy ${upstream.host}:${upstream.port}`)));
    sock.once("error", reject);
    sock.once(ready, () => { sock.off("error", reject); resolve(sock); });
  });
}

/** A socket already tunnelled through `upstream` to host:port. */
export async function openTunnel(upstream: Upstream, host: string, port: number): Promise<net.Socket> {
  const sock = await connect(upstream);
  try {
    if (upstream.protocol === "socks5") await socks5Connect(sock, upstream, host, port);
    else await httpConnect(sock, upstream, host, port);
    sock.setTimeout(0);
    return sock;
  } catch (e) {
    sock.destroy();
    throw e;
  }
}

async function httpConnect(sock: net.Socket, upstream: Upstream, host: string, port: number): Promise<void> {
  const authority = host.includes(":") ? `[${host}]:${port}` : `${host}:${port}`;
  const token = Buffer.from(`${upstream.username}:${upstream.password}`, "utf8").toString("base64");
  sock.write(`CONNECT ${authority} HTTP/1.1\r\nHost: ${authority}\r\nProxy-Authorization: Basic ${token}\r\n\r\n`);
  const { head, rest } = await readHead(sock);
  const status = head.split("\r\n", 1)[0];
  if (!/^HTTP\/1\.[01] 2\d\d/.test(status)) throw new Error(`the proxy refused the tunnel: ${status}`);
  if (rest.length) sock.unshift(rest);  // left paused: the pipe in handleClient starts the flow
}

/** Read exactly n bytes. */
function readN(sock: net.Socket, n: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const tryRead = () => {
      const b = sock.read(n) as Buffer | null;
      if (b) { cleanup(); resolve(b); }
    };
    const onEnd = () => { cleanup(); reject(new Error("the SOCKS5 proxy closed the connection")); };
    const onErr = (e: Error) => { cleanup(); reject(e); };
    const cleanup = () => { sock.off("readable", tryRead); sock.off("end", onEnd); sock.off("error", onErr); };
    sock.on("readable", tryRead);
    sock.once("end", onEnd);
    sock.once("error", onErr);
    tryRead();
  });
}

const SOCKS_REPLY: Record<number, string> = {
  1: "general failure", 2: "connection not allowed by ruleset", 3: "network unreachable", 4: "host unreachable",
  5: "connection refused", 6: "TTL expired", 7: "command not supported", 8: "address type not supported",
};

async function socks5Connect(sock: net.Socket, upstream: Upstream, host: string, port: number): Promise<void> {
  const user = Buffer.from(upstream.username, "utf8");
  const pass = Buffer.from(upstream.password, "utf8");
  if (user.length > 255 || pass.length > 255) throw new Error("SOCKS5 username and password are limited to 255 bytes each");
  sock.write(Buffer.from([5, 1, 2]));  // version 5, one method: username/password
  const choice = await readN(sock, 2);
  if (choice[0] !== 5 || choice[1] !== 2) throw new Error("the SOCKS5 proxy does not accept username/password authentication");
  sock.write(Buffer.concat([Buffer.from([1, user.length]), user, Buffer.from([pass.length]), pass]));
  const auth = await readN(sock, 2);
  if (auth[1] !== 0) throw new Error("the SOCKS5 proxy rejected the username or password");
  let addr: Buffer;
  const family = net.isIP(host);
  const v6 = family === 6 ? ipv6Bytes(host) : null;
  if (family === 4) addr = Buffer.from([1, ...host.split(".").map(Number)]);
  else if (v6) addr = Buffer.concat([Buffer.from([4]), v6]);
  else {
    const name = Buffer.from(host, "utf8");  // the proxy resolves the name, as the browser's SOCKS5 does
    if (name.length > 255) throw new Error("host name too long for SOCKS5");
    addr = Buffer.concat([Buffer.from([3, name.length]), name]);
  }
  sock.write(Buffer.concat([Buffer.from([5, 1, 0]), addr, Buffer.from([port >> 8, port & 0xff])]));
  const head = await readN(sock, 4);
  if (head[1] !== 0) throw new Error(`the SOCKS5 proxy could not connect: ${SOCKS_REPLY[head[1]] ?? `reply ${head[1]}`}`);
  const len = head[3] === 1 ? 4 : head[3] === 4 ? 16 : (await readN(sock, 1))[0];
  await readN(sock, len + 2);  // bound address + port, unused
}

/** The 16 bytes of an IPv6 literal; null for a form with an embedded IPv4 tail (sent as a name instead). */
export function ipv6Bytes(ip: string): Buffer | null {
  if (ip.includes(".") || ip.includes("%")) return null;
  const halves = ip.split("::");
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(":") : [];
  const right = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const fill = halves.length === 2 ? 8 - left.length - right.length : 0;
  const groups = [...left, ...Array(Math.max(fill, 0)).fill("0"), ...right];
  if (groups.length !== 8 || groups.some((g) => !/^[0-9a-fA-F]{1,4}$/.test(g))) return null;
  const out = Buffer.alloc(16);
  groups.forEach((g, i) => out.writeUInt16BE(parseInt(g, 16), i * 2));
  return out;
}

/**
 * Start the forwarder when resolveProxy moved the credentials to the engine, else null. A failure to
 * start never fails the launch: it warns that the request API will not use the proxy.
 */
export async function routeRequestApi(
  proxy: PwProxy | undefined, proxyArgs: readonly string[], quiet?: boolean,
): Promise<{ proxy: RequestProxy; args: string[] } | null> {
  const upstream = requestProxyUpstream(proxy, proxyArgs);
  if (!upstream) return null;
  try {
    return { proxy: await startRequestProxy(upstream, proxy?.bypass), args: requestProxyArgs(proxyArgs) };
  } catch (e) {
    if (!quiet) console.warn(`clearcote: could not start the local proxy for context.request (${String((e as Error)?.message ?? e)}); context.request and page.request will connect from this machine's IP, not through your proxy.`);
    return null;
  }
}

/** A launch's `.catch`: stop the forwarder when the browser never came up, then rethrow. */
export function closeOnFailure(route: { proxy: RequestProxy } | null): (e: unknown) => Promise<never> {
  return async (e) => { await route?.proxy.close().catch(() => {}); throw e; };
}
