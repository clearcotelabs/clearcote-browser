// `clearcote login --device`: sign in from a browser instead of pasting a key (OAuth 2.0 device
// authorization grant, RFC 8628, shaped to the Clearcote site's endpoints).
//
//   POST {base}/api/v1/device/code   {"client_name", "client_version"}
//     -> {"device_code", "user_code", "verification_uri", "verification_uri_complete", "expires_in", "interval"}
//   POST {base}/api/v1/device/token  {"device_code"}
//     -> 200 {"license_key", "plan", "expires_at"}
//      | 400 {"error": "authorization_pending" | "slow_down" | "expired_token" | "access_denied" | "invalid_request"}
//      | 429 (rate limited)
//
// `base` is the licence server the rest of the SDK uses (CLEARCOTE_LICENSE_API, else
// https://www.clearcotelabs.com). The key that comes back is saved exactly where and how
// `clearcote login <key>` saves one, and is never printed. Mirrors _devicelogin.py in the Python SDK.

import { readFileSync } from "node:fs";
import { licenseApiBase, licenseUserAgent } from "./license.js";
import { proxiedRequest } from "./net.js";

export const DEVICE_CLIENT_NAME = "clearcote-node";
/** RFC 8628 section 3.5: on slow_down the client adds 5 seconds to its polling interval, for good. */
export const SLOW_DOWN_STEP = 5;
const DEFAULT_INTERVAL = 5;
const MIN_INTERVAL = 1;

const SDK_VERSION: string = (() => {
  try {
    return JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version as string;
  } catch {
    return "unknown";
  }
})();

/** The device login ended without a key. `code` is the server's error (`expired_token`,
 * `access_denied`, `invalid_request`) or ours (`unreachable`, `unsupported`, `bad_response`, `cancelled`). */
export class DeviceLoginError extends Error {
  constructor(message: string, readonly code: string) {
    super(message);
    this.name = "DeviceLoginError";
  }
}

export interface DeviceCode {
  device_code: string;
  user_code: string;
  verification_uri: string;
  verification_uri_complete: string;
  expires_in: number;
  interval: number;
}

export interface DeviceGrant {
  licenseKey: string;
  plan: string | null;
  expiresAt: string | null;
}

type Json = Record<string, unknown>;

async function post(base: string, path: string, body: Json): Promise<{ status: number; body: Json; headers: Record<string, string> }> {
  const res = await proxiedRequest(`${base}${path}`, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "content-type": "application/json", accept: "application/json", "User-Agent": licenseUserAgent() },
    timeoutMs: 15_000,
  });
  let payload: unknown = {};
  try {
    const text = await res.text();
    payload = text.trim() ? JSON.parse(text) : {};
  } catch { /* not JSON */ }
  return { status: res.status, body: payload && typeof payload === "object" && !Array.isArray(payload) ? (payload as Json) : {}, headers: res.headers };
}

function seconds(value: unknown, fallback: number): number {
  const n = Number(value);
  return value !== null && value !== undefined && value !== "" && Number.isFinite(n) && n >= 0 ? n : fallback;
}

const reasonOf = (e: unknown): string => {
  const err = e as { cause?: { code?: string; message?: string }; message?: string };
  return err?.cause?.code || err?.cause?.message || err?.message || String(e);
};

/** Start a device login (`expires_in`/`interval` as numbers, `verification_uri_complete` filled in). */
export async function requestDeviceCode(opts: { licenseApiBase?: string; clientVersion?: string } = {}): Promise<DeviceCode> {
  const base = licenseApiBase(opts.licenseApiBase);
  let r;
  try {
    r = await post(base, "/api/v1/device/code", { client_name: DEVICE_CLIENT_NAME, client_version: opts.clientVersion ?? SDK_VERSION });
  } catch (e) {
    throw new DeviceLoginError(`could not reach the licence server at ${base} (${reasonOf(e)})`, "unreachable");
  }
  if (r.status === 404 || r.status === 405) {
    throw new DeviceLoginError(
      `the licence server at ${base} does not support device login (HTTP ${r.status}). Paste a key instead: clearcote login <key>`,
      "unsupported");
  }
  if (r.status !== 200) {
    const detail = (r.body.error_description as string) || (r.body.error as string) || `HTTP ${r.status}`;
    throw new DeviceLoginError(`the licence server did not start a device login (${detail})`, "bad_response");
  }
  const missing = ["device_code", "user_code", "verification_uri"].filter((k) => !r.body[k]);
  if (missing.length) throw new DeviceLoginError(`the licence server's device login answer is missing ${missing.join(", ")}`, "bad_response");
  return {
    device_code: String(r.body.device_code),
    user_code: String(r.body.user_code),
    verification_uri: String(r.body.verification_uri),
    verification_uri_complete: String(r.body.verification_uri_complete || r.body.verification_uri),
    expires_in: seconds(r.body.expires_in, 600),
    interval: Math.max(MIN_INTERVAL, seconds(r.body.interval, DEFAULT_INTERVAL)),
  };
}

export interface PollOptions {
  licenseApiBase?: string;
  /** Waits `seconds`; rejects with an AbortError-named error when `signal` aborts. Default: a timer. */
  sleep?: (seconds: number, signal?: AbortSignal) => Promise<void>;
  /** Milliseconds, monotonic enough for a deadline. Default `performance.now()`. */
  clock?: () => number;
  /** Ctrl-C: aborting ends the wait with DeviceLoginError("cancelled"). */
  signal?: AbortSignal;
  /** Told about each transient failure (network error, 5xx) that is being retried. */
  onRetry?: (reason: string) => void;
}

function timerSleep(sec: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DeviceLoginError("cancelled", "cancelled"));
    const t = setTimeout(() => { signal?.removeEventListener("abort", onAbort); resolve(); }, sec * 1000);
    const onAbort = () => { clearTimeout(t); reject(new DeviceLoginError("cancelled", "cancelled")); };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Poll the token endpoint until the user approves (resolves with the grant), denies, or the code
 * expires (both reject with DeviceLoginError). authorization_pending keeps the interval; slow_down
 * and HTTP 429 add 5 seconds for the rest of the login (a longer Retry-After wins). A network error
 * or a 5xx is retried at the same interval until the code expires.
 */
export async function pollForKey(code: DeviceCode, opts: PollOptions = {}): Promise<DeviceGrant> {
  const base = licenseApiBase(opts.licenseApiBase);
  const sleep = opts.sleep ?? timerSleep;
  const clock = opts.clock ?? (() => performance.now());
  let interval = code.interval;
  const deadline = clock() + code.expires_in * 1000;
  for (;;) {
    if (opts.signal?.aborted) throw new DeviceLoginError("cancelled", "cancelled");
    if (clock() >= deadline) throw new DeviceLoginError("the code expired before it was approved", "expired_token");
    try {
      await sleep(interval, opts.signal);
    } catch (e) {
      if (opts.signal?.aborted) throw new DeviceLoginError("cancelled", "cancelled");
      throw e;
    }
    if (opts.signal?.aborted) throw new DeviceLoginError("cancelled", "cancelled");
    let r;
    try {
      r = await post(base, "/api/v1/device/token", { device_code: code.device_code });
    } catch (e) {
      opts.onRetry?.(`licence server unreachable (${reasonOf(e)})`);
      continue;
    }
    if (r.status === 200) {
      const key = r.body.license_key;
      if (typeof key !== "string" || !key.trim()) {
        throw new DeviceLoginError("the licence server approved the login but sent no licence key", "bad_response");
      }
      return { licenseKey: key.trim(), plan: (r.body.plan as string) ?? null, expiresAt: (r.body.expires_at as string) ?? null };
    }
    const error = r.body.error;
    if (r.status === 429 || error === "slow_down") {
      interval = Math.max(interval + SLOW_DOWN_STEP, seconds(r.headers["retry-after"], 0));
      continue;
    }
    if (error === "authorization_pending") continue;
    if (error === "expired_token") throw new DeviceLoginError("the code expired before it was approved", "expired_token");
    if (error === "access_denied") throw new DeviceLoginError("the sign-in was denied in the browser", "access_denied");
    if (error === "invalid_request") {
      throw new DeviceLoginError("the licence server rejected the device login request (invalid_request)", "invalid_request");
    }
    if (r.status >= 500) {
      opts.onRetry?.(`licence server error (HTTP ${r.status})`);
      continue;
    }
    throw new DeviceLoginError(`unexpected answer from the licence server (${(error as string) || `HTTP ${r.status}`})`, "bad_response");
  }
}
