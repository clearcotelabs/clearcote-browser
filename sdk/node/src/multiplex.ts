// serveMultiplex — one CDP endpoint, many identities.
//
// `serve()` starts ONE browser with one persona. This puts an HTTP/WebSocket front on a port and
// starts a separate browser per identity on demand, chosen by the connection URL:
//
//   chromium.connectOverCDP("http://127.0.0.1:9222?fingerprint=acct-1&platform=windows")
//   chromium.connectOverCDP("http://127.0.0.1:9222?fingerprint=acct-2&proxy=socks5://u:p@host:1080&geoip=true")
//
// Playwright/Puppeteer fetch `/json/version` (keeping the query), receive a WebSocket URL routed
// through this server (`/fingerprint/<id>/devtools/browser/<uuid>`), and connect. The same seed
// reuses the same browser; asking for a running identity with DIFFERENT parameters is a 409 (close it
// first). No seed and no parameters = one shared default browser.
//
// Endpoints: GET / (status) · GET /json/version · GET /json/list · GET /json ·
// POST /fingerprint/<id>/close · WS /fingerprint/<id>/devtools/* · WS /devtools/* (default).
// WebSocket routes only reach a RUNNING identity; browsers are started by the HTTP endpoints.
//
// SAFETY, deliberately stricter than the design it mirrors (whose equivalent server had an
// unauthenticated path-traversal and a browser-origin CSRF reported against it):
//   * binds 127.0.0.1 by default; a non-loopback bind prints a warning, because anyone who can reach
//     the port can start browsers and drive them;
//   * seeds are validated against a strict pattern and profile directories are named by a HASH of
//     the identity, so no request value ever becomes part of a filesystem path;
//   * requests a web page could make are refused on every route: cross-site Sec-Fetch-Site, a browser
//     Origin that is not loopback or allowed, or a Host that is not an IP literal / localhost / allowed
//     (DNS rebinding);
//   * the number of concurrent browsers is capped (maxBrowsers, default 16).
//
// WebSockets are relayed at the socket level (the upgrade request is rewritten and the two sockets
// piped), so no WebSocket library is needed and CDP traffic is not re-framed.

import http from "node:http";
import net from "node:net";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { serve, type ServeOptions } from "./index.js";
import { toProxySpec } from "./net.js";

export interface MultiplexOptions extends Omit<ServeOptions, "port" | "host" | "userDataDir" | "allowOrigins" | "readyTimeoutMs"> {
  /** Port for the multiplexer (default 9222; 0 = ephemeral). */
  port?: number;
  /** Bind address (default 127.0.0.1). */
  host?: string;
  /**
   * Close an identity's browser this many seconds after its last WebSocket disconnects (0 = never,
   * the default). Also read from CLEARCOTE_SERVE_IDLE_TIMEOUT.
   */
  idleTimeoutSec?: number;
  /**
   * Keep each identity's profile under this directory (sub-directory named by a hash of the identity),
   * so cookies and storage survive restarts. Default: a temporary profile per browser, deleted when it
   * closes.
   */
  dataDir?: string;
  /** Maximum concurrent browsers (default 16). Further identities get HTTP 429. */
  maxBrowsers?: number;
  /** Extra browser Origins (exact, e.g. "https://tool.example") allowed to reach the endpoint. */
  allowOrigins?: string[];
  /**
   * Host names accepted in the Host header besides IP literals and "localhost" (DNS-rebinding
   * guard), e.g. "cdp.example.com" when a reverse proxy forwards that Host.
   */
  allowHosts?: string[];
  /** Per-browser startup timeout in ms (default 30000). */
  readyTimeoutMs?: number;
  /**
   * How a browser is started for an identity. Defaults to {@link serve}. Exists so the routing,
   * limits and WebSocket relay can be tested without a browser binary.
   */
  startBrowser?: (options: ServeOptions) => Promise<BrowserHandle>;
}

/** What the multiplexer needs from a started browser: its loopback CDP port, pid and a close(). */
export interface BrowserHandle {
  readonly port: number;
  readonly pid?: number;
  close(): Promise<void>;
}

export interface MultiplexServer {
  readonly host: string;
  readonly port: number;
  /** `http://host:port` — pass to connectOverCDP (optionally with `?fingerprint=...`). */
  readonly url: string;
  /** Snapshot of the running browsers. */
  status(): MultiplexStatus;
  /** Close one identity's browser. Resolves true when one was running. */
  closeIdentity(id: string): Promise<boolean>;
  /** Stop the server and every browser. */
  close(): Promise<void>;
}

export interface MultiplexStatus {
  status: "ok";
  active: number;
  maxBrowsers: number;
  idleTimeoutSec: number;
  processes: Array<{
    id: string;
    seed?: string;
    pid?: number;
    port: number;
    connections: number;
    startedAt: string;
    idleSince?: string;
    params: Record<string, string>;
  }>;
}

/** A seed usable in a URL path segment. Leading `_` is reserved for parameter-only identities. */
export const SEED_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

const NUMERIC = new Set([
  "hardwareConcurrency", "deviceMemory", "screenWidth", "screenHeight", "availWidth", "availHeight",
  "colorDepth", "devicePixelRatio", "maxTouchPoints", "storageQuota", "personaSchema",
]);
const BOOLEAN = new Set([
  "lightStealth", "realGpuHost", "disableGpuFingerprint", "fingerprintNoise", "gpuStringSpoof", "canvasNoise",
  "fingerprintVoices", "allowThirdPartyCookies", "transparentProxy", "socks5Udp",
]);
const STRING = new Set([
  "platform", "platformVersion", "brand", "brandVersion", "gpuVendor", "gpuRenderer", "location",
  "acceptLanguage", "webrtcIp", "webrtcMdns", "tlsProfile",
]);

const kebab = (k: string) => k.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
/** Query parameter (kebab-case) -> launch option name, for every parameter a connection may set. */
export const QUERY_PARAMS: Readonly<Record<string, string>> = Object.fromEntries(
  [...NUMERIC, ...BOOLEAN, ...STRING].map((k) => [kebab(k), k]),
);
const SPECIAL = new Set(["fingerprint", "timezone", "locale", "proxy", "geoip"]);

export class MultiplexRequestError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

/** Parsed per-connection identity: the id used in paths, and the launch options it adds. */
export interface ConnectionIdentity {
  /** "default", a seed, or "_p<hash>" for a parameter-only identity. */
  id: string;
  seed?: string;
  options: Record<string, unknown>;
  /** The raw accepted parameters (proxy credentials redacted), for the status page. */
  params: Record<string, string>;
}

function parseBool(name: string, v: string): boolean {
  if (/^(1|true|yes|on)$/i.test(v)) return true;
  if (/^(0|false|no|off)$/i.test(v)) return false;
  throw new MultiplexRequestError(400, `query parameter '${name}' must be true or false`);
}

/**
 * Turn a connection's query string into an identity. Unknown parameters are rejected with 400
 * rather than guessed at, so a typo never silently launches a different identity.
 */
export function parseConnectionIdentity(search: URLSearchParams): ConnectionIdentity {
  const options: Record<string, unknown> = {};
  const params: Record<string, string> = {};
  let seed: string | undefined;
  for (const [name, value] of search) {
    if (SPECIAL.has(name)) {
      if (name === "fingerprint") {
        if (!SEED_PATTERN.test(value)) {
          throw new MultiplexRequestError(400, "fingerprint must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}");
        }
        if (value === "default") {
          throw new MultiplexRequestError(400, "fingerprint 'default' is reserved for the no-parameter browser; pick another seed");
        }
        seed = value;
        options.fingerprint = value;
      } else if (name === "timezone") {
        options.timezone = value;
      } else if (name === "locale") {
        options.acceptLanguage = value;
      } else if (name === "geoip") {
        options.geoip = parseBool(name, value);
      } else if (name === "proxy") {
        let spec;
        try {
          spec = toProxySpec(value);
        } catch {
          throw new MultiplexRequestError(400, "proxy must be a URL such as socks5://user:pass@host:1080");
        }
        if (!spec) throw new MultiplexRequestError(400, "proxy is empty");
        options.proxy = { server: spec.server, username: spec.username, password: spec.password };
        params.proxy = spec.server + (spec.username ? " (with credentials)" : "");
        continue;
      }
      params[name] = value;
      continue;
    }
    const key = QUERY_PARAMS[name];
    if (!key) {
      throw new MultiplexRequestError(400, `unknown query parameter '${name}'. Supported: ${["fingerprint", "timezone", "locale", "proxy", "geoip", ...Object.keys(QUERY_PARAMS)].join(", ")}`);
    }
    if (NUMERIC.has(key)) {
      const n = value.trim() === "" ? NaN : Number(value);
      if (!Number.isFinite(n)) throw new MultiplexRequestError(400, `query parameter '${name}' must be a number`);
      options[key] = n;
    } else if (BOOLEAN.has(key)) {
      options[key] = parseBool(name, value);
    } else {
      options[key] = value;
    }
    params[name] = value;
  }
  if (seed) return { id: seed, seed, options, params };
  if (Object.keys(options).length === 0) return { id: "default", options, params };
  // Parameter-only identity: canonical form so ?a=1&b=2 and ?b=2&a=1 share one browser.
  const canonical = JSON.stringify(Object.keys(options).sort().map((k) => [k, options[k]]));
  return { id: `_p${createHash("sha256").update(canonical).digest("hex").slice(0, 16)}`, options, params };
}

/** Public ws(s):// base for URLs we hand back, honouring a reverse proxy's forwarded headers. */
export function publicWsBase(headers: http.IncomingHttpHeaders, fallbackHost: string): string {
  const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v)?.split(",")[0].trim();
  const host = first(headers["x-forwarded-host"]) || first(headers.host) || fallbackHost;
  const proto = (first(headers["x-forwarded-proto"]) || "").toLowerCase();
  return `${proto === "https" || proto === "wss" ? "wss" : "ws"}://${host}`;
}

/** Rewrite a child's ws://127.0.0.1:<port>/devtools/... URL to route through the multiplexer. */
export function rewriteWsUrl(url: string, wsBase: string, id: string): string {
  const m = /^wss?:\/\/[^/]+(\/devtools\/.*)$/.exec(url);
  if (!m) return url;
  const prefix = id === "default" ? "" : `/fingerprint/${encodeURIComponent(id)}`;
  return `${wsBase}${prefix}${m[1]}`;
}

/** Origin allowed to open a CDP WebSocket: none (non-browser client), loopback, or explicitly listed. */
export function originAllowed(origin: string | undefined, extra: readonly string[] = []): boolean {
  if (origin === undefined) return true;
  if (!origin || origin === "null") return false;
  if (extra.includes(origin)) return true;
  try {
    const h = new URL(origin).hostname.replace(/^\[|\]$/g, "");
    return h === "localhost" || h === "127.0.0.1" || h === "::1";
  } catch {
    return false;
  }
}

/** A /json, /json/version or /json/list path appended to the query (see the request handler). */
const QUERY_TAIL_PATH = /^(\?.*?)(\/json(?:\/version|\/list)?)\/?$/s;

function idleTimeoutFromEnv(): number {
  const n = Number((process.env.CLEARCOTE_SERVE_IDLE_TIMEOUT ?? "").trim());
  return Number.isFinite(n) && n > 0 ? n : 0;
}

interface Child {
  id: string;
  seed?: string;
  /** Canonical launch options: a later request for this id with different options gets 409. */
  key: string;
  params: Record<string, string>;
  srv: BrowserHandle;
  connections: number;
  startedAt: number;
  idleSince?: number;
  idleTimer?: NodeJS.Timeout;
}

/** Canonical form of an identity's launch options (proxy credentials included, never displayed). */
function optionsKey(options: Record<string, unknown>): string {
  return JSON.stringify(Object.keys(options).sort().map((k) => [k, options[k]]));
}

/**
 * Host header allowed: an IP literal or "localhost" (the rule Chrome's own DevTools server uses
 * against DNS rebinding), or a name in `extra` for a deployment behind a reverse proxy.
 */
export function hostAllowed(hostHeader: string | undefined, extra: readonly string[] = []): boolean {
  if (hostHeader === undefined) return true; // HTTP/1.0 client; nothing a browser page can omit
  const h = hostHeader.trim().toLowerCase();
  // "[v6]:port", "name:port", or a name / bare IPv6 literal ("::1": several colons, no port to strip).
  const name = h.startsWith("[")
    ? h.slice(1, h.includes("]") ? h.indexOf("]") : undefined)
    : (h.split(":").length === 2 ? h.slice(0, h.indexOf(":")) : h);
  if (!name) return false;
  if (net.isIP(name) || name === "localhost") return true;
  return extra.some((e) => e.toLowerCase() === name || e.toLowerCase() === h);
}

/**
 * Refuse requests a web page could have made: a cross-site/same-site fetch (Sec-Fetch-Site), a
 * browser Origin that is not loopback or allowed, or a Host that is not ours (DNS rebinding).
 * Playwright, Puppeteer and curl send none of these, so real clients are unaffected.
 */
export function requestRefusal(
  headers: http.IncomingHttpHeaders,
  allowOrigins: readonly string[] = [],
  allowHosts: readonly string[] = [],
): string | null {
  const origin = headers.origin as string | undefined;
  const site = String(headers["sec-fetch-site"] ?? "").toLowerCase();
  if ((site === "cross-site" || site === "same-site") && !(origin && allowOrigins.includes(origin))) {
    return "Forbidden: cross-site request";
  }
  if (!originAllowed(origin, allowOrigins)) return "Forbidden origin";
  if (!hostAllowed(headers.host, allowHosts)) return "Forbidden host";
  return null;
}

/** Start a multiplexing CDP endpoint. See the module header. */
export async function serveMultiplex(options: MultiplexOptions = {}): Promise<MultiplexServer> {
  const {
    port = 9222,
    host = "127.0.0.1",
    idleTimeoutSec = idleTimeoutFromEnv(),
    dataDir,
    maxBrowsers = 16,
    allowOrigins = [],
    allowHosts = [],
    readyTimeoutMs = 30_000,
    startBrowser = (o: ServeOptions) => serve(o) as Promise<BrowserHandle>,
    ...base
  } = options;
  if (!Number.isInteger(maxBrowsers) || maxBrowsers < 1) throw new Error("maxBrowsers must be a whole number >= 1");
  const children = new Map<string, Child>();
  const pending = new Map<string, { key: string; promise: Promise<Child> }>();
  /** Ids whose browser is shutting down; a relaunch waits so two browsers never share a profile. */
  const closingIds = new Map<string, Promise<void>>();
  /** Ids closed while still launching: the launch closes its browser as soon as it is up. */
  const cancelled = new Set<string>();
  const relaySockets = new Set<net.Socket>();
  let closing = false;

  const loopback = ["127.0.0.1", "localhost", "::1"].includes(host);
  if (!loopback && !base.quiet) {
    process.stderr.write(
      `[clearcote] WARNING: serveMultiplex is bound to ${host}. Anyone who can reach this port can ` +
        "start browsers and control them. Keep it on 127.0.0.1 or put authentication in front of it.\n",
    );
  }

  function scheduleIdle(c: Child): void {
    if (c.idleTimer) clearTimeout(c.idleTimer);
    c.idleTimer = undefined;
    if (c.connections > 0) { c.idleSince = undefined; return; }
    c.idleSince = Date.now();
    if (idleTimeoutSec > 0) {
      c.idleTimer = setTimeout(() => { void closeIdentity(c.id); }, idleTimeoutSec * 1000);
      c.idleTimer.unref?.();
    }
  }

  const conflict = (id: string) => new MultiplexRequestError(
    409,
    `identity '${id}' is already running with different parameters; close it first (POST /fingerprint/${encodeURIComponent(id)}/close)`,
  );

  async function ensureChild(ident: ConnectionIdentity): Promise<Child> {
    if (closing) throw new MultiplexRequestError(503, "shutting down");
    const key = optionsKey(ident.options);
    const existing = children.get(ident.id);
    if (existing) {
      // Silently handing back a browser started with other options (e.g. without the proxy this
      // request asks for) would send this client's traffic somewhere it did not choose.
      if (existing.key !== key) throw conflict(ident.id);
      scheduleIdle(existing); // a fresh /json/version means a client is about to connect
      return existing;
    }
    const inflight = pending.get(ident.id);
    if (inflight) {
      if (inflight.key !== key) throw conflict(ident.id);
      return inflight.promise;
    }
    const stillClosing = closingIds.get(ident.id);
    if (stillClosing) {
      await stillClosing;
      return ensureChild(ident);
    }
    if (children.size + pending.size >= maxBrowsers) {
      throw new MultiplexRequestError(429, `maxBrowsers (${maxBrowsers}) reached; close an identity first`);
    }
    cancelled.delete(ident.id);
    const promise = (async () => {
      const srv = await startBrowser({
        ...(base as ServeOptions),
        ...(ident.options as ServeOptions),
        host: "127.0.0.1",
        port: undefined,
        quiet: true,
        readyTimeoutMs,
        // Profile directory named by a hash of the identity — never by a request value.
        userDataDir: dataDir ? join(dataDir, createHash("sha256").update(`clearcote:${ident.id}`).digest("hex").slice(0, 24)) : undefined,
      });
      if (closing || cancelled.has(ident.id)) {
        cancelled.delete(ident.id);
        await srv.close().catch(() => {});
        throw new MultiplexRequestError(503, closing ? "shutting down" : `identity '${ident.id}' was closed while starting`);
      }
      const child: Child = { id: ident.id, seed: ident.seed, key, params: ident.params, srv, connections: 0, startedAt: Date.now() };
      children.set(ident.id, child);
      scheduleIdle(child);
      return child;
    })();
    pending.set(ident.id, { key, promise });
    try {
      return await promise;
    } finally {
      pending.delete(ident.id);
    }
  }

  async function closeIdentity(id: string): Promise<boolean> {
    const c = children.get(id);
    if (!c) {
      const inflight = pending.get(id);
      if (!inflight) return false;
      cancelled.add(id);
      await inflight.promise.catch(() => {});
      return true;
    }
    children.delete(id);
    if (c.idleTimer) clearTimeout(c.idleTimer);
    const done: Promise<void> = c.srv.close().catch(() => {}).finally(() => {
      if (closingIds.get(id) === done) closingIds.delete(id);
    });
    closingIds.set(id, done);
    await done;
    return true;
  }

  function status(): MultiplexStatus {
    return {
      status: "ok",
      active: children.size,
      maxBrowsers,
      idleTimeoutSec,
      processes: [...children.values()].map((c) => ({
        id: c.id,
        ...(c.seed ? { seed: c.seed } : {}),
        pid: c.srv.pid,
        port: c.srv.port,
        connections: c.connections,
        startedAt: new Date(c.startedAt).toISOString(),
        ...(c.idleSince ? { idleSince: new Date(c.idleSince).toISOString() } : {}),
        params: c.params,
      })),
    };
  }

  function sendJson(res: http.ServerResponse, code: number, body: unknown): void {
    const text = JSON.stringify(body);
    res.writeHead(code, { "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(text) });
    res.end(text);
  }

  const server = http.createServer(async (req, res) => {
    try {
      const refusal = requestRefusal(req.headers, allowOrigins, allowHosts);
      if (refusal) return sendJson(res, 403, { error: refusal });
      const u = new URL(req.url ?? "/", "http://mux.invalid");
      let path = u.pathname.replace(/\/+$/, "") || "/";
      let search = u.searchParams;
      // Playwright clients up to 1.49 build the discovery URL by appending "/json/version/" to the
      // endpoint string, so connectOverCDP("http://host:port?fingerprint=a") asks for
      // GET /?fingerprint=a/json/version/: the path lands at the end of the query. (Newer releases
      // append it to the URL's path.) A seed cannot contain '/', so nothing else matches.
      const tail = path === "/" ? QUERY_TAIL_PATH.exec(u.search) : null;
      if (tail) {
        search = new URLSearchParams(tail[1]);
        path = tail[2];
      }
      const wsBase = publicWsBase(req.headers, `${host}:${actualPort()}`);

      if (req.method === "GET" && path === "/") return sendJson(res, 200, status());

      const close = /^\/fingerprint\/([^/]+)\/close$/.exec(path);
      if (close) {
        if (req.method !== "POST") return sendJson(res, 405, { error: "use POST" });
        const id = decodeURIComponent(close[1]);
        return sendJson(res, 200, { id, terminated: await closeIdentity(id) });
      }

      if (req.method === "GET" && (path === "/json/version" || path === "/json/list" || path === "/json")) {
        const ident = parseConnectionIdentity(search);
        const child = await ensureChild(ident);
        const upstream = await fetch(`http://127.0.0.1:${child.srv.port}${path === "/json" ? "/json/list" : path}`, {
          signal: AbortSignal.timeout(10_000),
        });
        const data = (await upstream.json()) as Record<string, unknown> | Array<Record<string, unknown>>;
        const fix = (o: Record<string, unknown>) => {
          if (typeof o.webSocketDebuggerUrl === "string") o.webSocketDebuggerUrl = rewriteWsUrl(o.webSocketDebuggerUrl, wsBase, child.id);
          if (typeof o.devtoolsFrontendUrl === "string") {
            // "/devtools/inspector.html?ws=127.0.0.1:<childport>/devtools/page/<id>" -> via the multiplexer
            const hostPart = wsBase.replace(/^wss?:\/\//, "");
            const prefix = child.id === "default" ? "" : `/fingerprint/${encodeURIComponent(child.id)}`;
            // Function replacer: a forwarded host must never be read as a `$&`-style pattern.
            o.devtoolsFrontendUrl = o.devtoolsFrontendUrl.replace(/([?&]wss?=)[^/&]+(\/devtools\/)/, (_m, a: string, b: string) => `${a}${hostPart}${prefix}${b}`);
          }
          return o;
        };
        return sendJson(res, 200, Array.isArray(data) ? data.map(fix) : fix(data));
      }
      return sendJson(res, 404, { error: "not found" });
    } catch (e) {
      const code = e instanceof MultiplexRequestError ? e.status : 502;
      if (res.headersSent) { res.destroy(); return; }
      return sendJson(res, code, { error: (e as Error).message });
    }
  });

  server.on("upgrade", (req: http.IncomingMessage, socket: net.Socket, head: Buffer) => {
    // Node removes its own error handler from an upgraded socket: without this, one client reset
    // is an uncaught exception that takes down the server and every browser it holds.
    socket.on("error", () => socket.destroy());
    const reject = (code: number, msg: string) => {
      if (socket.destroyed) return;
      socket.end(`HTTP/1.1 ${code} ${msg}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n${msg}\n`);
    };
    try {
      const refusal = requestRefusal(req.headers, allowOrigins, allowHosts);
      if (refusal) return reject(403, refusal);
      if (String(req.headers.upgrade ?? "").toLowerCase() !== "websocket") return reject(400, "Only WebSocket upgrades are relayed");
      const u = new URL(req.url ?? "/", "http://mux.invalid");
      let id = "default";
      let rest = u.pathname;
      const m = /^\/fingerprint\/([^/]+)(\/devtools\/.*)$/.exec(u.pathname);
      if (m) {
        id = decodeURIComponent(m[1]);
        rest = m[2];
      } else if (!u.pathname.startsWith("/devtools/")) {
        return reject(404, "Not Found");
      }
      // The WebSocket route never starts a browser. A path carries only the id, not the proxy or
      // other options the identity was created with, so a browser started from it could send the
      // client's traffic direct; and its DevTools GUID would not match the URL anyway.
      const c = children.get(id);
      if (!c) return reject(404, `identity '${id}' is not running; connect via http://host:port/?fingerprint=... first`);
      const upstream = net.connect(c.srv.port, "127.0.0.1");
      let relaying = false;
      upstream.once("connect", () => {
        if (socket.destroyed || children.get(c.id) !== c) {
          upstream.destroy();
          if (!socket.destroyed) reject(502, "Browser closed");
          return;
        }
        relaying = true;
        let reqHead = `GET ${rest}${u.search} HTTP/1.1\r\n`;
        for (let i = 0; i < req.rawHeaders.length; i += 2) {
          const k = req.rawHeaders[i];
          const lk = k.toLowerCase();
          // The origin was checked above; the browser's own --remote-allow-origins check would
          // reject a forwarded browser Origin, and Host must name the child.
          if (lk === "host" || lk === "origin") continue;
          reqHead += `${k}: ${req.rawHeaders[i + 1]}\r\n`;
        }
        reqHead += `Host: 127.0.0.1:${c.srv.port}\r\n\r\n`;
        upstream.write(reqHead);
        if (head.length) upstream.write(head);
        c.connections++;
        scheduleIdle(c);
        let released = false;
        const release = () => {
          if (released) return;
          released = true;
          c.connections = Math.max(0, c.connections - 1);
          if (children.get(c.id) === c) scheduleIdle(c);
        };
        relaySockets.add(socket);
        relaySockets.add(upstream);
        socket.pipe(upstream).pipe(socket);
        // Tear down BOTH sides as soon as EITHER ends. http.Server sockets are half-open
        // (allowHalfOpen), so relying on "close" alone left a finished CDP session holding the
        // relay open forever: its connection never counted as gone and idle close never ran.
        const teardown = () => {
          socket.destroy();
          upstream.destroy();
          relaySockets.delete(socket);
          relaySockets.delete(upstream);
          release();
        };
        socket.once("end", teardown);
        upstream.once("end", teardown);
        socket.once("close", teardown);
        upstream.once("close", teardown);
      });
      // Once relaying, the stream belongs to the WebSocket: never write an HTTP response into it.
      upstream.once("error", () => { if (relaying) socket.destroy(); else reject(502, "Browser unreachable"); });
      socket.once("close", () => { if (!relaying) upstream.destroy(); });
    } catch (e) {
      const code = e instanceof MultiplexRequestError ? e.status : 502;
      reject(code, (e as Error).message.replace(/[\r\n]/g, " "));
    }
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => resolve());
  });
  function actualPort(): number {
    const a = server.address();
    return typeof a === "object" && a ? a.port : port;
  }
  const boundPort = actualPort();
  const urlHost = host.includes(":") ? `[${host}]` : host;

  const handle: MultiplexServer = {
    host,
    port: boundPort,
    url: `http://${urlHost}:${boundPort}`,
    status,
    closeIdentity,
    async close() {
      if (closing) return;
      closing = true;
      const stopped = new Promise<void>((resolve) => server.close(() => resolve()));
      // server.close() waits for every open connection: keep-alive HTTP clients and relayed CDP
      // sessions would hold it open indefinitely. Shutting down means ending them.
      server.closeAllConnections?.();
      for (const s of relaySockets) s.destroy();
      relaySockets.clear();
      await stopped;
      // Launches still in flight see `closing` and close their own browser; wait for them, then
      // close the running ones and any close already under way.
      await Promise.allSettled([...pending.values()].map((p) => p.promise));
      await Promise.all([...children.keys()].map((id) => closeIdentity(id)));
      await Promise.allSettled([...closingIds.values()]);
    },
  };
  process.once("exit", () => { for (const c of children.values()) void c.srv.close(); });
  if (!base.quiet) {
    process.stderr.write(
      `[clearcote] multiplexed CDP endpoint ready: ${handle.url}\n` +
        `            connectOverCDP("${handle.url}?fingerprint=<seed>")  ·  status: GET ${handle.url}/\n`,
    );
  }
  return handle;
}
