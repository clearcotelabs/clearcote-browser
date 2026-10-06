// Minimal HTTP(S) client that can go through an HTTP or SOCKS5 proxy, with no dependencies.
//
// Two SDK paths need to reach the network THROUGH the caller's proxy rather than from the host:
//
//   * geoip — the exit IP must be the proxy's, or the timezone/locale describe the wrong place.
//     Before this existed, a SOCKS proxy could not be used for the lookup at all and geoip was
//     silently skipped for every SOCKS user.
//   * licenseThroughProxy — lease checkout/heartbeat/checkin normally go direct, which reveals the
//     host's real address to the licence server and makes a direct connection from a machine whose
//     browsing is otherwise proxied.
//
// Node's global fetch has no proxy support, so this opens the tunnel itself: HTTP `CONNECT` (or
// absolute-form for plain-http targets) or a SOCKS5 CONNECT with RFC 1929 credentials, then speaks
// HTTP/1.1 over the socket with `Connection: close`. Deliberately small: one request per socket,
// buffered body, which is all both callers need.

import net from "node:net";
import tls from "node:tls";

/** A proxy the request should go through. `server` is `http://host:port` or `socks5://host:port`. */
export interface ProxySpec {
  server: string;
  username?: string;
  password?: string;
}

/** A request that ran out of time, through a proxy or not: `code` "ETIMEDOUT", `name` "TimeoutError" (the
 * name fetch's own AbortSignal.timeout uses), so callers can tell a timeout from other failures. */
export function timeoutError(message: string): Error {
  return Object.assign(new Error(message), { name: "TimeoutError", code: "ETIMEDOUT" });
}

export interface ProxiedRequestInit {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
  /** Whole-request budget, connect to last byte. Default 30s. */
  timeoutMs?: number;
  /** Route through this proxy. Omitted/null = direct (global fetch). */
  proxy?: ProxySpec | null;
  /** Abort the request (direct requests only): it rejects at once with an AbortError. */
  signal?: AbortSignal;
}

/** The subset of a fetch Response both callers use. */
export interface SimpleResponse {
  status: number;
  ok: boolean;
  headers: Record<string, string>;
  text(): Promise<string>;
  json(): Promise<unknown>;
}

/** The proxy schemes {@link proxiedRequest} can tunnel through. */
export const PROXIED_REQUEST_SCHEMES: ReadonlySet<string> = new Set(["http", "https", "socks5", "socks5h"]);

/** Normalise a proxy given as a URL string (credentials inline) or a Playwright-style object. */
export function toProxySpec(
  proxy: string | { server?: string; username?: string; password?: string } | null | undefined,
): ProxySpec | null {
  if (!proxy) return null;
  const raw = typeof proxy === "string" ? proxy : proxy.server;
  if (!raw) return null;
  const withScheme = /:\/\//.test(raw) ? raw : `http://${raw}`;
  const u = new URL(withScheme);
  const username = typeof proxy === "object" && proxy.username ? proxy.username : decodeURIComponent(u.username);
  const password = typeof proxy === "object" && proxy.password ? proxy.password : decodeURIComponent(u.password);
  return {
    // URL.hostname already brackets an IPv6 literal ("[::1]").
    server: `${u.protocol}//${u.hostname}:${u.port || defaultProxyPort(u.protocol)}`,
    ...(username ? { username } : {}),
    ...(password ? { password } : {}),
  };
}

function defaultProxyPort(protocol: string): string {
  if (/^socks/i.test(protocol)) return "1080";
  if (protocol === "https:") return "443";
  return "80";
}

function readUntil(socket: net.Socket, predicate: (buf: Buffer) => number, deadline: number): Promise<{ head: Buffer; rest: Buffer }> {
  return new Promise((resolve, reject) => {
    let buf = Buffer.alloc(0);
    const timer = setTimeout(() => done(timeoutError("proxy handshake timed out")), Math.max(1, deadline - Date.now()));
    const onData = (chunk: Buffer) => {
      buf = Buffer.concat([buf, chunk]);
      const n = predicate(buf);
      if (n >= 0) done(null, n);
    };
    const onEnd = () => done(new Error("proxy closed the connection during the handshake"));
    const onError = (e: Error) => done(e);
    function done(err: Error | null, n = 0) {
      clearTimeout(timer);
      socket.off("data", onData);
      socket.off("end", onEnd);
      socket.off("error", onError);
      if (err) reject(err);
      else resolve({ head: buf.subarray(0, n), rest: buf.subarray(n) });
    }
    socket.on("data", onData);
    socket.once("end", onEnd);
    socket.once("error", onError);
  });
}

function connectTcp(host: string, port: number, deadline: number): Promise<net.Socket> {
  return new Promise((resolve, reject) => {
    const s = net.connect({ host, port });
    const timer = setTimeout(() => { s.destroy(); reject(timeoutError(`connect to ${host}:${port} timed out`)); }, Math.max(1, deadline - Date.now()));
    s.once("connect", () => { clearTimeout(timer); resolve(s); });
    s.once("error", (e) => { clearTimeout(timer); reject(e); });
  });
}

/** SOCKS5 CONNECT (RFC 1928) with optional username/password (RFC 1929). Hostname sent as-is (ATYP 3). */
async function socks5Connect(s: net.Socket, proxy: ProxySpec, host: string, port: number, deadline: number): Promise<void> {
  const creds = !!(proxy.username || proxy.password);
  s.write(Buffer.from(creds ? [5, 2, 0, 2] : [5, 1, 0]));
  const hello = await readUntil(s, (b) => (b.length >= 2 ? 2 : -1), deadline);
  if (hello.head[0] !== 5) throw new Error("SOCKS5 proxy: bad greeting reply");
  const method = hello.head[1];
  if (method === 2) {
    const u = Buffer.from(proxy.username ?? "", "utf8");
    const p = Buffer.from(proxy.password ?? "", "utf8");
    if (u.length > 255 || p.length > 255) throw new Error("SOCKS5 proxy: credentials longer than 255 bytes");
    s.write(Buffer.concat([Buffer.from([1, u.length]), u, Buffer.from([p.length]), p]));
    const auth = await readUntil(s, (b) => (b.length >= 2 ? 2 : -1), deadline);
    if (auth.head[1] !== 0) throw new Error("SOCKS5 proxy rejected the username/password");
  } else if (method !== 0) {
    throw new Error(method === 0xff ? "SOCKS5 proxy accepted no offered authentication method" : `SOCKS5 proxy chose unsupported method ${method}`);
  }
  const h = Buffer.from(host, "utf8");
  s.write(Buffer.concat([Buffer.from([5, 1, 0, 3, h.length]), h, Buffer.from([(port >> 8) & 0xff, port & 0xff])]));
  const reply = await readUntil(s, (b) => {
    if (b.length < 5) return -1;
    const atyp = b[3];
    const addrLen = atyp === 1 ? 4 : atyp === 4 ? 16 : atyp === 3 ? 1 + b[4] : -1;
    if (addrLen < 0) return 0; // unknown address type: rejected below
    const total = 4 + addrLen + 2;
    return b.length >= total ? total : -1;
  }, deadline);
  if (reply.head.length === 0) throw new Error("SOCKS5 proxy: malformed reply (unknown address type)");
  if (reply.head[1] !== 0) throw new Error(`SOCKS5 proxy refused the connection (reply code ${reply.head[1]})`);
  if (reply.rest.length) s.unshift(reply.rest);
}

function basicAuth(proxy: ProxySpec): string | null {
  if (!proxy.username && !proxy.password) return null;
  return "Basic " + Buffer.from(`${proxy.username ?? ""}:${proxy.password ?? ""}`, "utf8").toString("base64");
}

function parseResponse(raw: Buffer): SimpleResponse {
  const sep = raw.indexOf("\r\n\r\n");
  if (sep < 0) throw new Error("malformed HTTP response (no header terminator)");
  const headLines = raw.subarray(0, sep).toString("latin1").split("\r\n");
  const m = /^HTTP\/\d(?:\.\d)?\s+(\d{3})/.exec(headLines[0] ?? "");
  if (!m) throw new Error(`malformed HTTP status line: ${headLines[0]}`);
  const status = Number(m[1]);
  const headers: Record<string, string> = {};
  for (const line of headLines.slice(1)) {
    const i = line.indexOf(":");
    if (i > 0) headers[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
  }
  let body = raw.subarray(sep + 4);
  if (/chunked/i.test(headers["transfer-encoding"] ?? "")) body = dechunk(body);
  else if (headers["content-length"] !== undefined) {
    const len = /^\d+$/.test(headers["content-length"]) ? Number(headers["content-length"]) : NaN;
    if (!Number.isFinite(len)) throw new Error(`malformed Content-Length: ${headers["content-length"]}`);
    // A connection closed early must not pass as a complete (shorter) body.
    if (body.length < len) throw new Error(`response truncated (${body.length} of ${len} bytes)`);
    body = body.subarray(0, len);
  }
  const text = body.toString("utf8");
  return {
    status,
    ok: status >= 200 && status < 300,
    headers,
    text: async () => text,
    json: async () => JSON.parse(text),
  };
}

function dechunk(buf: Buffer): Buffer {
  const out: Buffer[] = [];
  let i = 0;
  for (;;) {
    const eol = buf.indexOf("\r\n", i);
    if (eol < 0) throw new Error("chunked response truncated (no terminating chunk)");
    const size = parseInt(buf.subarray(i, eol).toString("latin1").split(";")[0].trim(), 16);
    if (!Number.isFinite(size)) throw new Error("malformed chunk size in response");
    if (size === 0) return Buffer.concat(out);
    if (eol + 2 + size > buf.length) throw new Error("chunked response truncated (short chunk)");
    out.push(buf.subarray(eol + 2, eol + 2 + size));
    i = eol + 2 + size + 2;
  }
}

async function directFetch(url: string, init: ProxiedRequestInit, timeoutMs: number): Promise<SimpleResponse> {
  const res = await fetch(url, {
    method: init.method ?? "GET",
    headers: init.headers,
    body: init.body,
    redirect: "follow",
    signal: init.signal ? AbortSignal.any([AbortSignal.timeout(timeoutMs), init.signal]) : AbortSignal.timeout(timeoutMs),
  });
  const text = await res.text();
  const headers: Record<string, string> = {};
  res.headers.forEach((v, k) => { headers[k] = v; });
  return { status: res.status, ok: res.ok, headers, text: async () => text, json: async () => JSON.parse(text) };
}

/**
 * Make one HTTP(S) request, through `init.proxy` when given (HTTP CONNECT / absolute-form, or
 * SOCKS5), otherwise directly. Rejects on network/proxy failure; HTTP error statuses resolve with
 * `ok: false`, like fetch.
 */
export async function proxiedRequest(url: string, init: ProxiedRequestInit = {}): Promise<SimpleResponse> {
  const timeoutMs = init.timeoutMs ?? 30_000;
  const proxy = init.proxy ? toProxySpec(init.proxy) : null;
  if (!proxy) return directFetch(url, init, timeoutMs);

  const deadline = Date.now() + timeoutMs;
  const target = new URL(url);
  const isHttps = target.protocol === "https:";
  const host = target.hostname.replace(/^\[|\]$/g, "");
  const port = Number(target.port || (isHttps ? 443 : 80));
  const hostLiteral = host.includes(":") ? `[${host}]` : host;
  const p = new URL(proxy.server);
  const pHost = p.hostname.replace(/^\[|\]$/g, "");
  const pPort = Number(p.port);
  const scheme = p.protocol.replace(":", "").toLowerCase();

  let socket: net.Socket = await connectTcp(pHost, pPort, deadline);
  try {
    let absoluteForm = false;
    if (scheme === "socks5" || scheme === "socks5h") {
      await socks5Connect(socket, proxy, host, port, deadline);
    } else if (scheme === "http" || scheme === "https") {
      if (scheme === "https") socket = tls.connect({ socket, servername: pHost });
      if (isHttps) {
        const auth = basicAuth(proxy);
        socket.write(
          `CONNECT ${hostLiteral}:${port} HTTP/1.1\r\nHost: ${hostLiteral}:${port}\r\n` +
            (auth ? `Proxy-Authorization: ${auth}\r\n` : "") + "\r\n",
        );
        const { head, rest } = await readUntil(socket, (b) => { const i = b.indexOf("\r\n\r\n"); return i < 0 ? -1 : i + 4; }, deadline);
        const code = Number(/^HTTP\/\d(?:\.\d)?\s+(\d{3})/.exec(head.toString("latin1"))?.[1] ?? 0);
        if (code !== 200) throw new Error(code === 407 ? "HTTP proxy requires (different) credentials (407)" : `HTTP proxy refused CONNECT (${code || "bad reply"})`);
        if (rest.length) socket.unshift(rest);
      } else {
        absoluteForm = true; // plain-http target: send the request straight to the proxy
      }
    } else {
      throw new Error(`unsupported proxy scheme '${scheme}' (use http://, https:// or socks5://)`);
    }

    const stream: net.Socket = isHttps ? tls.connect({ socket, servername: host }) : socket;
    const path = target.pathname + target.search;
    const headers: Record<string, string> = {
      Host: target.port ? `${hostLiteral}:${target.port}` : hostLiteral,
      "User-Agent": "clearcote-sdk",
      Accept: "*/*",
      ...(init.headers ?? {}),
      // After the caller's headers: the response is read until the server closes, so a caller's
      // keep-alive would hang every request until the timeout.
      Connection: "close",
    };
    if (absoluteForm) {
      const auth = basicAuth(proxy);
      if (auth) headers["Proxy-Authorization"] = auth;
    }
    const body = init.body !== undefined ? Buffer.from(init.body, "utf8") : null;
    if (body) headers["Content-Length"] = String(body.length);
    const requestTarget = absoluteForm ? url : path || "/";
    let head = `${init.method ?? "GET"} ${requestTarget} HTTP/1.1\r\n`;
    for (const [k, v] of Object.entries(headers)) head += `${k}: ${v}\r\n`;
    head += "\r\n";

    const raw = await new Promise<Buffer>((resolve, reject) => {
      const chunks: Buffer[] = [];
      const timer = setTimeout(() => { stream.destroy(); reject(timeoutError(`request to ${url} timed out`)); }, Math.max(1, deadline - Date.now()));
      stream.on("data", (c: Buffer) => chunks.push(c));
      stream.once("end", () => { clearTimeout(timer); resolve(Buffer.concat(chunks)); });
      stream.once("close", () => { clearTimeout(timer); resolve(Buffer.concat(chunks)); });
      stream.once("error", (e) => { clearTimeout(timer); reject(e); });
      stream.write(head);
      if (body) stream.write(body);
    });
    return parseResponse(raw);
  } finally {
    socket.destroy();
  }
}
