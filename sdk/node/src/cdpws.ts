// A minimal CDP client over a WebSocket, on Node's standard library only. Port of the Python SDK's
// `_cdpws.py`.
//
// serve() launches the engine itself, so there is no Playwright connection to borrow, and Node only
// has a global WebSocket from 22 on while the SDK supports 20. The headless window fit used to ride
// on that global, so on Node 20 it was silently skipped: the browser got its display but kept the
// engine's default window, about 1000px wide inside a 1920px screen. What the fit needs is small: a
// handful of request/response calls on the browser endpoint (flat sessions for the one page it
// reads), text frames only, never a subscription. That is all this implements, deliberately not a
// general client.

import { createHash, randomBytes } from "node:crypto";
import net from "node:net";
import tls from "node:tls";

/** One browser-level CDP WebSocket. `send()` resolves with its own reply; events are dropped. */
export interface CdpConnection {
  send(method: string, params?: Record<string, unknown>, sessionId?: string): Promise<Record<string, unknown>>;
  close(): void;
}

const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
const OP_CONTINUATION = 0x0, OP_TEXT = 0x1, OP_BINARY = 0x2, OP_CLOSE = 0x8, OP_PING = 0x9, OP_PONG = 0xa;

/** A client frame: FIN set, payload masked (RFC 6455 5.3 requires it of clients). */
function frame(opcode: number, payload: Buffer): Buffer {
  const n = payload.length;
  let head: Buffer;
  if (n < 126) {
    head = Buffer.from([0x80 | opcode, 0x80 | n]);
  } else if (n < 65536) {
    head = Buffer.alloc(4);
    head[0] = 0x80 | opcode;
    head[1] = 0x80 | 126;
    head.writeUInt16BE(n, 2);
  } else {
    head = Buffer.alloc(10);
    head[0] = 0x80 | opcode;
    head[1] = 0x80 | 127;
    head.writeBigUInt64BE(BigInt(n), 2);
  }
  const mask = randomBytes(4);
  const masked = Buffer.alloc(n);
  for (let i = 0; i < n; i++) masked[i] = payload[i] ^ mask[i & 3];
  return Buffer.concat([head, mask, masked]);
}

/**
 * Open a CDP WebSocket (ws:// or wss://). Rejects when the endpoint refuses the upgrade or does not
 * answer within `timeoutMs`; each `send()` gets the same budget for its reply.
 */
export function connectCdp(wsUrl: string, timeoutMs: number): Promise<CdpConnection> {
  return new Promise((resolveOpen, rejectOpen) => {
    let u: URL;
    try {
      u = new URL(wsUrl);
    } catch {
      return rejectOpen(new Error(`expected a ws:// CDP URL, got ${JSON.stringify(wsUrl)}`));
    }
    if ((u.protocol !== "ws:" && u.protocol !== "wss:") || !u.hostname) {
      return rejectOpen(new Error(`expected a ws:// CDP URL, got ${JSON.stringify(wsUrl)}`));
    }
    const secure = u.protocol === "wss:";
    const host = u.hostname.replace(/^\[(.*)\]$/, "$1");   // an IPv6 literal comes bracketed
    const port = Number(u.port) || (secure ? 443 : 80);
    const key = randomBytes(16).toString("base64");
    const accept = createHash("sha1").update(key + WS_GUID).digest("base64");

    let open = false;
    let closed = false;
    let buf: Buffer = Buffer.alloc(0);
    let parts: Buffer[] = [];
    let next = 0;
    const pending = new Map<number, { resolve(v: Record<string, unknown>): void; reject(e: Error): void }>();

    const sock: net.Socket = secure
      ? tls.connect({ host, port, servername: net.isIP(host) ? undefined : host })
      : net.connect({ host, port });
    const openTimer = setTimeout(() => fail(new Error("CDP connect timed out")), timeoutMs);

    /** End the connection once: settle everything still waiting, then let the socket go. */
    function fail(err: Error, closeFrame?: Buffer): void {
      if (closed) return;
      closed = true;
      clearTimeout(openTimer);
      if (closeFrame) {
        // Flush the close frame before letting go; an endpoint that never answers it is cut off.
        sock.end(closeFrame);
        setTimeout(() => sock.destroy(), 1000).unref();
      } else {
        sock.destroy();
      }
      if (!open) rejectOpen(err);
      for (const p of pending.values()) p.reject(err);
      pending.clear();
    }

    function onMessage(text: string): void {
      let m: { id?: number; result?: Record<string, unknown>; error?: { message?: string } };
      try { m = JSON.parse(text); } catch { return; }
      const p = m.id === undefined ? undefined : pending.get(m.id);
      if (!p) return;   // an event, or a stale reply: this client subscribes to nothing
      pending.delete(m.id!);
      if (m.error) p.reject(new Error(m.error.message ?? JSON.stringify(m.error)));
      else p.resolve(m.result ?? {});
    }

    /** Consume every complete frame in `buf` (fragments joined; pings answered). */
    function readFrames(): void {
      while (!closed && buf.length >= 2) {
        const fin = buf[0] & 0x80, opcode = buf[0] & 0x0f, masked = buf[1] & 0x80;
        let n = buf[1] & 0x7f;
        let off = 2;
        if (n === 126) {
          if (buf.length < 4) return;
          n = buf.readUInt16BE(2);
          off = 4;
        } else if (n === 127) {
          if (buf.length < 10) return;
          n = Number(buf.readBigUInt64BE(2));
          off = 10;
        }
        const mask = masked ? off : -1;
        if (masked) off += 4;
        if (buf.length < off + n) return;
        let payload = buf.subarray(off, off + n);
        if (mask >= 0) payload = Buffer.from(payload.map((b, i) => b ^ buf[mask + (i & 3)]));
        buf = buf.subarray(off + n);
        if (opcode === OP_CLOSE) return fail(new Error("CDP connection closed"));
        if (opcode === OP_PING) {
          sock.write(frame(OP_PONG, payload));
          continue;
        }
        if (opcode === OP_PONG) continue;
        if (opcode === OP_TEXT || opcode === OP_BINARY || opcode === OP_CONTINUATION) {
          parts.push(payload);
          if (fin) {
            const text = Buffer.concat(parts).toString("utf8");
            parts = [];
            onMessage(text);
          }
        }
      }
    }

    /** The upgrade response: `101` and the accept hash RFC 6455 4.1 derives from our key. */
    function readHandshake(): void {
      const end = buf.indexOf("\r\n\r\n");
      if (end < 0) return;
      const lines = buf.subarray(0, end).toString("latin1").split("\r\n");
      buf = buf.subarray(end + 4);
      if (!/^HTTP\/1\.[01] 101\b/.test(lines[0])) return fail(new Error(`CDP WebSocket upgrade refused: ${lines[0]}`));
      const line = lines.slice(1).find((l) => /^sec-websocket-accept:/i.test(l));
      const got = line?.slice(line.indexOf(":") + 1).trim();
      if (got !== accept) return fail(new Error("CDP WebSocket upgrade answered with the wrong accept key"));
      open = true;
      clearTimeout(openTimer);
      resolveOpen(connection);
      readFrames();
    }

    const connection: CdpConnection = {
      send(method, params = {}, sessionId) {
        return new Promise((resolve, reject) => {
          if (closed) return reject(new Error("CDP connection closed"));
          const id = ++next;
          const t = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timed out`)); }, timeoutMs);
          pending.set(id, {
            resolve: (v) => { clearTimeout(t); resolve(v); },
            reject: (e) => { clearTimeout(t); reject(e); },
          });
          sock.write(frame(OP_TEXT, Buffer.from(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }))));
        });
      },
      close() {
        fail(new Error("CDP connection closed"), frame(OP_CLOSE, Buffer.from([0x03, 0xe8])));   // 1000: normal closure
      },
    };

    sock.on(secure ? "secureConnect" : "connect", () => {
      const path = (u.pathname || "/") + u.search;
      sock.write(
        `GET ${path} HTTP/1.1\r\nHost: ${u.host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n` +
        `Sec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`);
    });
    sock.on("data", (chunk: Buffer) => {
      if (closed) return;
      buf = buf.length ? Buffer.concat([buf, chunk]) : chunk;
      if (open) readFrames();
      else readHandshake();
    });
    sock.on("error", (err) => fail(new Error(`CDP connection failed: ${err.message}`)));
    sock.on("close", () => fail(new Error("CDP connection closed")));
  });
}
