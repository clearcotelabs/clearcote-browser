// An in-memory stand-in for the hosted Clearcote API (PLAN §3), for the cloud client and CLI tests.
// Mirrors sdk/python/tests/_fake_cloud.py: the same endpoints, shapes and canned data, so both SDKs
// are tested against the same server behaviour.
import * as http from "node:http";
import type * as net from "node:net";

export const API_KEY = "cc_live_test_key";
export const T0 = "2026-10-02T10:00:00.000Z";
export const LIVE = "https://www.clearcotelabs.test/live/bs_run1?t=tok&handoff=1";
export const RECORDING = Buffer.from("\x00\x00\x00\x18ftypmp42fake-mp4-bytes", "latin1");

const EVENTS = [
  { seq: 1, at: T0, type: "session.started", data: { kind: "run" } },
  { seq: 2, at: "2026-10-02T10:00:01.000Z", type: "navigation", data: { url: "https://example.com/", title: "Example Domain" } },
  { seq: 3, at: "2026-10-02T10:00:02.000Z", type: "tab.closed", data: {} },
  { seq: 4, at: "2026-10-02T10:00:03.000Z", type: "session.ended", data: { reason: "run_finished" } },
];

const SESSIONS = [
  { id: "bs_a1", status: "active", worker: "w1", createdAt: "2026-10-02T09:00:00.000Z", costEur: 0.0123, note: "crawler" },
  { id: "bs_b2", status: "ended", worker: "w1", createdAt: "2026-10-01T09:00:00.000Z", costEur: 0.5, note: null },
];

const RESULT = {
  status: "done", detail: null, url: "https://example.com/pricing", title: "Pricing",
  output: { plan: "Starter", price: "9.99" }, outputError: null, markdown: "# Pricing",
  steps: [{ step: 1, kind: "click", action: "Open pricing", text: null, probability: 0.85, decideMs: 361, atMs: 1200,
    url: "https://example.com/", ledTo: "https://example.com/pricing", pageChanged: true }],
  usage: { decision: { inputTokens: 1200, outputTokens: 30, requests: 2 }, text: { inputTokens: 0, outputTokens: 0, requests: 0 },
    extract: { inputTokens: 900, outputTokens: 40, requests: 1 }, estimatedUsd: 0.0006 },
  handoffs: 1, elapsedMs: 14800, jet: { version: "0.1.0", commit: "cd7011c" },
};

export interface Logged { method: string; path: string; query: string; headers: http.IncomingHttpHeaders; body: any }
type Answer = [number, unknown, Record<string, string>?];

export class FakeCloud {
  log: Logged[] = [];
  connectUrl = "ws://127.0.0.1:9/devtools/browser/none";
  runStatuses = ["queued", "running", "waiting_for_human", "running", "succeeded"];
  flaky = 0;
  handoffPolls = 2;
  recordingState = "ready";
  recordingLocation = "/dev/blob/rec.mp4?sig=abc"; // relative, or another host entirely
  profileCookies = new Map<string, Map<string, any>>();
  webhooks: any[] = [];
  url = "";
  port = 0;
  private n = 0;
  private runGets = new Map<string, number>();
  private handoffs = new Map<string, { reason: string | null; polls: number }>();
  private server?: http.Server;
  private sockets = new Set<net.Socket>();

  requests(method?: string, path?: string): Logged[] {
    return this.log.filter((r) => (method === undefined || r.method === method) && (path === undefined || r.path === path));
  }

  private sessionView(sid: string, status = "active") {
    const h = this.handoffs.get(sid);
    let handoff = null;
    if (h) {
      const state = h.polls > 0 ? "waiting" : "done";
      handoff = { state, reason: h.reason, since: T0, expiresAt: "2026-10-02T10:10:00.000Z",
        doneAt: state === "waiting" ? null : "2026-10-02T10:01:00.000Z", liveUrl: state === "waiting" ? LIVE : null };
      h.polls -= 1;
    }
    return { id: sid, status, worker: "w1", proxy: "managed", note: null, profile: null, createdAt: T0, startedAt: T0,
      endedAt: status === "active" ? null : T0, endReason: status === "active" ? null : "stopped:user", stopRequested: status !== "active",
      usage: { bytesUp: 100, bytesDown: 2000, gb: 0.0000021, seconds: 12 }, costEur: 0.0012, pricing: { eurPerGb: 1, eurPerHour: 0 },
      handoff, recording: null };
  }

  private runView(rid: string, status: string) {
    const waiting = status === "waiting_for_human";
    const finished = ["succeeded", "failed", "cancelled", "expired"].includes(status);
    return {
      id: rid, status, task: "Find the price", url: "https://example.com/", hasSchema: true, createdAt: T0,
      startedAt: status === "queued" ? null : T0, endedAt: finished ? T0 : null,
      result: status === "succeeded" ? RESULT : null,
      handoff: waiting ? { state: "waiting", reason: "needs a login", since: T0, expiresAt: "2026-10-02T10:10:00.000Z", doneAt: null, liveUrl: LIVE } : null,
      session: this.sessionView(rid, finished ? "ended" : "active"),
      costEur: { browser: 0.0012, agent: 0.0007, total: 0.0019 },
    };
  }

  handle(method: string, path: string, query: URLSearchParams, headers: http.IncomingHttpHeaders, body: any): Answer {
    if (path === "/dev/blob/rec.mp4") return [200, RECORDING, { "content-type": "video/mp4" }];
    if (path === "/login-page") {
      return [200, Buffer.from("<html><title>login</title>signed in</html>"), { "content-type": "text/html", "set-cookie": "sid=s3cret; Path=/; HttpOnly" }];
    }
    if (headers.authorization !== `Bearer ${API_KEY}`) return [401, { error: "Missing or invalid API key." }];
    const parts = path.replace(/^\/+|\/+$/g, "").split("/").map(decodeURIComponent);
    if (parts[0] !== "api" || parts[1] !== "v1") return [404, { error: "Not found." }];
    const rest = parts.slice(2);
    const is = (...p: string[]) => rest.length === p.length && p.every((x, i) => x === "*" || rest[i] === x);

    if (is("browsers") && method === "POST") {
      const sid = `bs_${++this.n}`;
      return [201, { id: sid, worker: "w1", connectUrl: this.connectUrl, expiresAt: "2026-10-02T11:00:00.000Z",
        pricing: { eurPerGb: 1, eurPerHour: 0 }, limits: { maxSeconds: 3600, idleSeconds: 300 },
        engine: { version: "153.0", revision: "r29", pinned: false }, warnings: [],
        ...(body?.profile && typeof body.profile === "object" ? { profile: body.profile } : {}) }];
    }
    if (is("browsers") && method === "GET") return [200, { balanceEur: 12.5, sessions: SESSIONS }];
    if (rest[0] === "browsers" && rest[1] === "profiles") return this.profiles(method, rest.slice(2), body);
    if (rest[0] === "browsers" && rest.length >= 2) {
      const sid = rest[1];
      const tail = rest.slice(2).join("/");
      if (tail === "" && method === "GET") return [200, this.sessionView(sid)];
      if (tail === "" && method === "DELETE") return [200, this.sessionView(sid, "ended")];
      if (tail === "live") return [200, { viewUrl: `wss://w1.example/live/${sid}`, expiresAt: T0, interactive: query.get("control") === "1" }];
      if (tail === "share") {
        return [200, { url: `https://www.clearcotelabs.test/${body?.recording ? "replay" : "live"}/${sid}?t=x`, expiresAt: T0, control: !!body?.control }];
      }
      if (tail === "handoff" && method === "POST") {
        this.handoffs.set(sid, { reason: body?.reason ?? null, polls: this.handoffPolls });
        return [200, { state: "waiting", reason: body?.reason ?? null, since: T0, expiresAt: "2026-10-02T10:10:00.000Z", liveUrl: LIVE }];
      }
      if (tail === "handoff/done" && method === "POST") {
        const h = this.handoffs.get(sid);
        if (h) h.polls = 0;
        return [200, { state: "done", reason: null, since: T0, expiresAt: T0, doneAt: "2026-10-02T10:01:00.000Z", liveUrl: null }];
      }
      if (tail === "events") {
        const after = Number(query.get("after") ?? 0);
        const limit = Number(query.get("limit") ?? 2);
        const left = EVENTS.filter((e) => e.seq > after);
        const page = left.slice(0, limit);
        return [200, { events: page, next: left.length > page.length ? page[page.length - 1].seq : null }];
      }
      if (tail === "recording") {
        if (sid === "bs_unrecorded") return [404, { error: "This session was not recorded.", code: "NOT_FOUND" }];
        if (this.recordingState !== "ready") return [409, { error: "The recording is still processing.", code: "NOT_READY" }];
        return [302, Buffer.alloc(0), { location: this.recordingLocation }];
      }
    }
    if (is("runs") && method === "POST") {
      if (body && "keepAlive" in body) return [400, { error: "keepAlive does not apply to runs" }];
      return [201, { id: "bs_run1", status: "queued", worker: "w1", createdAt: T0, expiresAt: "2026-10-02T10:15:00.000Z",
        pricing: { eurPerGb: 1, eurPerHour: 0, eurPerMTok: 0.05, eurPerMTokTextIn: 0.3, eurPerMTokTextOut: 1.2 },
        limits: { maxSeconds: 900, idleSeconds: 300 }, engine: { version: "153.0", revision: "r29", pinned: false }, warnings: [] }];
    }
    if (is("runs") && method === "GET") {
      return [200, { runs: [{ id: "bs_run1", status: "succeeded", task: "Find the price", createdAt: T0, endedAt: T0, resultStatus: "done", costEur: 0.0019 }] }];
    }
    if (is("runs", "*")) {
      const rid = rest[1];
      if (method === "DELETE") return [200, this.runView(rid, "cancelled")];
      if (this.flaky > 0) {
        this.flaky -= 1;
        return [503, Buffer.from("upstream restarting"), { "content-type": "text/plain" }];
      }
      const n = this.runGets.get(rid) ?? 0;
      this.runGets.set(rid, n + 1);
      return [200, this.runView(rid, this.runStatuses[Math.min(n, this.runStatuses.length - 1)])];
    }
    if (is("webhooks") && method === "POST") {
      const hook = { id: `wh_${this.webhooks.length + 1}`, url: body.url, events: body.events ?? [], description: body.description ?? null, createdAt: T0 };
      this.webhooks.push({ ...hook, lastDelivery: null });
      return [201, { ...hook, secret: "whsec_test_secret" }];
    }
    if (is("webhooks") && method === "GET") {
      return [200, { webhooks: this.webhooks.length ? this.webhooks : [
        { id: "wh_9", url: "https://hooks.example.com/cc", events: ["run.finished"], description: null, createdAt: T0, lastDelivery: { at: T0, status: 200, ok: true } },
        { id: "wh_8", url: "https://hooks.example.com/all", events: [], description: "all", createdAt: T0, lastDelivery: null },
      ] }];
    }
    if (is("webhooks", "*") && method === "DELETE") return [200, { ok: true }];
    if (is("webhooks", "*", "test")) return [200, { ok: true, status: 200 }];
    return [404, { error: "Not found.", code: "NOT_FOUND" }];
  }

  private profiles(method: string, rest: string[], body: any): Answer {
    if (!rest.length) {
      return [200, { profiles: [{ name: "acct-1", bytes: 2048, cookies: 12, storage: 0, savedBy: null, createdAt: T0, updatedAt: T0, inUse: false }] }];
    }
    const name = rest[0];
    if (rest[1] === "cookies" && method === "PUT") {
      if (name === "busy") return [409, { error: "A live session is saving to this profile.", code: "PROFILE_IN_USE" }];
      const have = body?.mode === "replace" ? new Map<string, any>() : new Map(this.profileCookies.get(name) ?? []);
      for (const c of body?.cookies ?? []) have.set(JSON.stringify([c.name, c.domain, c.path || "/"]), c);
      this.profileCookies.set(name, have);
      const domains = [...new Set([...have.values()].map((c) => String(c.domain).replace(/^\./, "")))].sort().slice(0, 50);
      return [200, { name, cookies: have.size, imported: (body?.cookies ?? []).length, domains, bytes: 1234, updatedAt: T0 }];
    }
    if (rest.length === 1 && method === "GET") {
      const have = this.profileCookies.get(name) ?? new Map();
      return [200, { name, cookies: have.size, domains: [...new Set([...have.values()].map((c) => c.domain))].sort(), bytes: 1234, storage: 0, updatedAt: T0 }];
    }
    if (rest.length === 1 && method === "DELETE") return [200, { ok: true }];
    return [404, { error: "Not found.", code: "NOT_FOUND" }];
  }

  async start(): Promise<this> {
    this.server = http.createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => {
        const raw = Buffer.concat(chunks).toString("utf8");
        const u = new URL(req.url ?? "/", "http://x");
        let body: any = {};
        try { body = raw ? JSON.parse(raw) : {}; } catch { body = {}; }
        this.log.push({ method: req.method ?? "", path: u.pathname, query: u.search.replace(/^\?/, ""), headers: req.headers, body: raw ? body : null });
        const [status, payload, extra] = this.handle(req.method ?? "GET", u.pathname, u.searchParams, req.headers, body);
        const data = Buffer.isBuffer(payload) ? payload : Buffer.from(JSON.stringify(payload));
        res.writeHead(status, { "content-type": "application/json", ...(extra ?? {}), "content-length": String(data.length) });
        res.end(data);
      });
    });
    this.server.on("connection", (s) => { this.sockets.add(s); s.on("close", () => this.sockets.delete(s)); });
    await new Promise<void>((r) => this.server!.listen(0, "127.0.0.1", () => r()));
    this.port = (this.server.address() as net.AddressInfo).port;
    this.url = `http://127.0.0.1:${this.port}`;
    return this;
  }

  async close(): Promise<void> {
    for (const s of this.sockets) s.destroy();
    await new Promise<void>((r) => this.server?.close(() => r()) ?? r());
  }
}

export async function startFakeCloud(): Promise<FakeCloud> {
  return new FakeCloud().start();
}
