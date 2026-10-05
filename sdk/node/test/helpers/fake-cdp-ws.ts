// A browser-level CDP endpoint that speaks raw RFC 6455 frames, for the tests of the SDK's own
// WebSocket client (src/cdpws.ts). No WebSocket library on either side, so the tests run the same on
// every Node the SDK supports, including those without a global WebSocket.
import { createHash } from "node:crypto";
import { createServer, type Server } from "node:http";
import type { AddressInfo, Socket } from "node:net";

export type Responder = (method: string, params: Record<string, unknown>, sessionId?: string) => unknown;

export interface FakeCdpOptions {
  /** Listen here instead of 127.0.0.1 (e.g. "::1"). */
  host?: string;
  /** Answer the upgrade with this status instead of 101 (e.g. 403 for a refused origin). */
  refuseUpgrade?: number;
  /** Answer the upgrade with a Sec-WebSocket-Accept that does not match the key. */
  wrongAccept?: boolean;
  /** Split every reply into continuation frames of this many bytes. */
  fragment?: number;
  /** Send a ping, an unrelated event and a stale reply before every reply. */
  noise?: boolean;
  /** Pad every reply with this many bytes (past 65535 forces a 64-bit length). */
  padding?: number;
  /** Deliver every reply one byte per write, so frames arrive split across reads. */
  trickle?: boolean;
  /** Send a close frame instead of answering this method. */
  closeOn?: string;
  /** Never answer this method. */
  silentOn?: string;
}

export interface FakeCdp {
  wsUrl: string;
  /** Methods received, in order. */
  methods: string[];
  /** Every client frame was masked, as RFC 6455 requires of a client. */
  allMasked: boolean;
  /** Pongs received, with their payloads. */
  pongs: string[];
  /** Close frames received from the client. */
  closes: number;
  /** Connections that have ended (the client let go). */
  ended: Promise<void>;
  close(): Promise<void>;
}

function serverFrame(opcode: number, payload: Buffer, fin = true): Buffer {
  const n = payload.length;
  const b0 = (fin ? 0x80 : 0) | opcode;
  if (n < 126) return Buffer.concat([Buffer.from([b0, n]), payload]);
  if (n < 65536) {
    const h = Buffer.alloc(4);
    h[0] = b0; h[1] = 126; h.writeUInt16BE(n, 2);
    return Buffer.concat([h, payload]);
  }
  const h = Buffer.alloc(10);
  h[0] = b0; h[1] = 127; h.writeBigUInt64BE(BigInt(n), 2);
  return Buffer.concat([h, payload]);
}

export async function fakeCdpServer(respond: Responder, opts: FakeCdpOptions = {}): Promise<FakeCdp> {
  const sockets = new Set<Socket>();
  let endedResolve!: () => void;
  const fake: FakeCdp = {
    wsUrl: "",
    methods: [],
    allMasked: true,
    pongs: [],
    closes: 0,
    ended: new Promise<void>((r) => { endedResolve = r; }),
    close: async () => {
      for (const s of sockets) s.destroy();
      await new Promise<void>((r) => server.close(() => r()));
    },
  };
  const server: Server = createServer((_req, res) => { res.statusCode = 404; res.end(); });
  server.on("upgrade", (req, socket: Socket) => {
    sockets.add(socket);
    socket.on("close", () => { sockets.delete(socket); endedResolve(); });
    socket.on("error", () => {});
    if (opts.refuseUpgrade) {
      socket.end(`HTTP/1.1 ${opts.refuseUpgrade} Forbidden\r\nContent-Length: 0\r\n\r\n`);
      return;
    }
    const key = String(req.headers["sec-websocket-key"] ?? "");
    const accept = createHash("sha1").update(key + (opts.wrongAccept ? "x" : "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")).digest("base64");
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);

    const send = (data: Buffer) => {
      if (!opts.trickle) return void socket.write(data);
      for (const byte of data) socket.write(Buffer.from([byte]));
    };
    const reply = (msg: unknown) => {
      const body = Buffer.from(JSON.stringify(msg));
      if (opts.noise) {
        send(serverFrame(0x9, Buffer.from("are you there")));
        send(serverFrame(0x1, Buffer.from(JSON.stringify({ method: "Target.targetCreated", params: {} }))));
        send(serverFrame(0x1, Buffer.from(JSON.stringify({ id: 99999, result: { stale: true } }))));
      }
      if (!opts.fragment) return send(serverFrame(0x1, body));
      for (let i = 0; i < body.length; i += opts.fragment) {
        const last = i + opts.fragment >= body.length;
        send(serverFrame(i === 0 ? 0x1 : 0x0, body.subarray(i, i + opts.fragment), last));
      }
    };

    let buf = Buffer.alloc(0);
    socket.on("data", (chunk: Buffer) => {
      buf = Buffer.concat([buf, chunk]);
      while (buf.length >= 2) {
        const opcode = buf[0] & 0x0f, masked = buf[1] & 0x80;
        let n = buf[1] & 0x7f, off = 2;
        if (n === 126) { if (buf.length < 4) return; n = buf.readUInt16BE(2); off = 4; }
        else if (n === 127) { if (buf.length < 10) return; n = Number(buf.readBigUInt64BE(2)); off = 10; }
        if (!masked) fake.allMasked = false;
        const maskAt = off;
        if (masked) off += 4;
        if (buf.length < off + n) return;
        const payload = Buffer.from(buf.subarray(off, off + n).map((b, i) => (masked ? b ^ buf[maskAt + (i & 3)] : b)));
        buf = buf.subarray(off + n);
        if (opcode === 0x8) { fake.closes++; socket.end(serverFrame(0x8, Buffer.alloc(0))); return; }
        if (opcode === 0xa) { fake.pongs.push(payload.toString()); continue; }
        if (opcode !== 0x1) continue;
        const msg = JSON.parse(payload.toString()) as { id: number; method: string; params?: Record<string, unknown>; sessionId?: string };
        fake.methods.push(msg.method);
        if (msg.method === opts.closeOn) { socket.write(serverFrame(0x8, Buffer.from([0x03, 0xe8]))); continue; }
        if (msg.method === opts.silentOn) continue;
        try {
          const result = respond(msg.method, msg.params ?? {}, msg.sessionId) ?? {};
          reply({ id: msg.id, result, ...(opts.padding ? { pad: "x".repeat(opts.padding) } : {}), ...(msg.sessionId ? { sessionId: msg.sessionId } : {}) });
        } catch (e) {
          reply({ id: msg.id, error: { code: -32601, message: (e as Error).message } });
        }
      }
    });
  });
  const host = opts.host ?? "127.0.0.1";
  await new Promise<void>((r, j) => { server.once("error", j); server.listen(0, host, () => r()); });
  const authority = host.includes(":") ? `[${host}]` : host;
  fake.wsUrl = `ws://${authority}:${(server.address() as AddressInfo).port}/devtools/browser/fake`;
  return fake;
}

/**
 * The browser side of the served-window fit: the page reads its display, and the window reports the
 * bounds it was given. Same model as the in-process fake in geometry.test.ts.
 */
export function servedBrowser(display: number[]): Responder {
  let outer = [780, 580];
  return (method, params) => {
    switch (method) {
      case "Target.getTargets": return { targetInfos: [{ targetId: "T1", type: "page" }] };
      case "Target.attachToTarget": return { sessionId: "S1" };
      case "Runtime.evaluate":
        return { result: { value: String(params.expression).includes("availWidth") ? display : outer } };
      case "Emulation.getScreenInfos": return { screenInfos: [{ id: "2300000000", isPrimary: true }] };
      case "Browser.getWindowForTarget": return { windowId: 3 };
      case "Browser.setWindowBounds": {
        const b = params.bounds as { width: number; height: number };
        outer = [b.width, b.height];
        return {};
      }
      default: return {};
    }
  };
}
