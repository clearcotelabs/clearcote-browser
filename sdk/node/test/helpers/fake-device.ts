// A local stand-in for the site's device-login endpoints, implementing the contract the server side is
// built to (and every branch of it). Mirrors sdk/python/tests/_fake_device.py.
//
//   POST /api/v1/device/code   {client_name, client_version} -> 200 {device_code, user_code, ...}
//                               anything else                  -> 400 {"error": "invalid_request"}
//   POST /api/v1/device/token  {device_code}                  -> the next scripted answer:
//     "pending" -> 400 authorization_pending   "slow"    -> 400 slow_down
//     "expired" -> 400 expired_token           "denied"  -> 400 access_denied
//     "invalid" -> 400 invalid_request         "429"     -> 429 (rate limited)
//     "500"     -> 500                         "drop"    -> connection destroyed, no answer
//     "ok"      -> 200 {license_key, plan, expires_at[, account_email]}
//     "slow-ok" -> the key, but only after `slowMs` (handed out at once: a client that gave up has lost it,
//                  and later polls are answered expired_token)
//     "hook-ok" -> calls `onKeyRequest()` (e.g. Ctrl-C landing mid-request), then the key
//   A wrong device_code is answered 400 invalid_request whatever the script says.
import http from "node:http";
import type net from "node:net";

export const DEVICE_CODE = "dc_fake_device_code_0001";
export const USER_CODE = "WDJB-MJHT";
export const LICENSE_KEY = "cc_lic_devicelogin_fake_key_7Q2x9";

const ERRORS: Record<string, string> = {
  pending: "authorization_pending", slow: "slow_down", expired: "expired_token", denied: "access_denied", invalid: "invalid_request",
};

export interface FakeDeviceOptions {
  plan?: string;
  expiresAt?: string | null;
  interval?: number;
  expiresIn?: number;
  codeStatus?: number;
  retryAfter?: number;
  accountEmail?: string;
  slowMs?: number;
  onKeyRequest?: () => void;
}

export interface FakeDevice {
  url: string;
  log: Array<{ path: string; body: unknown; headers: http.IncomingHttpHeaders }>;
  tokenPolls(): Array<{ path: string; body: unknown }>;
  close(): Promise<void>;
}

export async function startFakeDevice(script: string[], o: FakeDeviceOptions = {}): Promise<FakeDevice> {
  const queue = [...script];
  let handedOut = false;
  const log: FakeDevice["log"] = [];
  let url = "";
  const send = (res: http.ServerResponse, status: number, payload: unknown, headers: Record<string, string> = {}) => {
    res.writeHead(status, { "content-type": "application/json", ...headers });
    res.end(JSON.stringify(payload));
  };
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      let body: Record<string, unknown> | null = null;
      try { body = raw ? JSON.parse(raw) : {}; } catch { body = null; }
      log.push({ path: req.url ?? "", body, headers: req.headers });
      if (req.url === "/api/v1/device/code") {
        if ((o.codeStatus ?? 200) !== 200) return send(res, o.codeStatus!, { error: "not_found" });
        if (!body || !body.client_name || !body.client_version) return send(res, 400, { error: "invalid_request" });
        return send(res, 200, {
          device_code: DEVICE_CODE, user_code: USER_CODE,
          verification_uri: `${url}/device`, verification_uri_complete: `${url}/device?code=${USER_CODE}`,
          expires_in: o.expiresIn ?? 900, interval: o.interval ?? 5,
        });
      }
      if (req.url === "/api/v1/device/token") {
        if (!body || body.device_code !== DEVICE_CODE) return send(res, 400, { error: "invalid_request" });
        if (handedOut) return send(res, 400, { error: "expired_token" }); // the key goes out once
        const step = queue.shift() ?? "pending";
        if (step === "drop") return void req.socket.destroy(); // no status line: the client sees the connection go
        const grant = () => ({
          license_key: LICENSE_KEY, plan: o.plan ?? "pro", expires_at: o.expiresAt === undefined ? "2027-01-31T00:00:00.000Z" : o.expiresAt,
          ...(o.accountEmail ? { account_email: o.accountEmail } : {}),
        });
        if (step === "slow-ok") {
          handedOut = true; // handed out now; the answer is what is slow
          setTimeout(() => { try { send(res, 200, grant()); } catch { /* the client gave up on it */ } }, o.slowMs ?? 2000);
          return;
        }
        if (step === "hook-ok") {
          o.onKeyRequest?.();
          handedOut = true;
          setTimeout(() => send(res, 200, grant()), 200);
          return;
        }
        if (step === "ok") {
          handedOut = true;
          return send(res, 200, grant());
        }
        if (step === "429") return send(res, 429, { error: "Rate limit exceeded." }, o.retryAfter ? { "retry-after": String(o.retryAfter) } : {});
        if (step === "500") return send(res, 500, { error: "internal" });
        return send(res, 400, { error: ERRORS[step] });
      }
      return send(res, 404, { error: "not found" });
    });
  });
  const sockets = new Set<net.Socket>();
  server.on("connection", (c: net.Socket) => { sockets.add(c); c.on("close", () => sockets.delete(c)); });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
  url = `http://127.0.0.1:${(server.address() as net.AddressInfo).port}`;
  return {
    url,
    log,
    tokenPolls: () => log.filter((e) => e.path === "/api/v1/device/token"),
    close: () => {
      for (const c of sockets) c.destroy();
      return new Promise((r) => server.close(() => r()));
    },
  };
}
