// Launch-time option helpers that are NOT fingerprint switches: unpacked-extension loading and
// proxy resolution. Pure (input -> switches / cleaned proxy) so they're unit-testable and mirror
// the Python SDK exactly.

import { closeSync, existsSync, openSync, readSync, statSync } from "node:fs";
import { dirname, join } from "node:path";

/** A Playwright proxy descriptor. */
export interface PwProxy {
  server?: string;
  username?: string;
  password?: string;
  bypass?: string;
}

/** Privacy Sandbox + intrusive web APIs a de-Googled stealth build should not expose (a build that
 * claims de-Googled while still answering document.browsingTopics()/navigator.runAdAuction is a
 * self-contradictory, pivotable fingerprint). All are runtime base::Features, so disabling needs
 * no rebuild.
 *
 * WebUSB is deliberately NOT in this list. It is not a Privacy Sandbox feature - it is a device
 * API that ships alongside Web Serial, WebHID and Web Bluetooth under identical secure-context
 * gating. Disabling only WebUSB left navigator.usb absent while serial/hid/bluetooth stayed
 * present, a combination no real Chromium produces; measured against stock Chrome on the same
 * host, that split was the single flagged difference in the device-API family. Presence leaks
 * nothing on its own - the API is permission-gated and enumerates no device without a user
 * gesture - so exposing it costs no privacy and removes a hard coherence tell. */
export const PRIVACY_SANDBOX_FEATURES = [
  "BrowsingTopics", "BrowsingTopicsDocumentAPI", "Fledge", "InterestGroupStorage",
  "PrivateAggregationApi", "SharedStorageAPI", "FencedFrames",
] as const;

/** Chromium honors only the LAST --enable-features / --disable-features on the command line (they
 * do NOT concatenate), so multiple occurrences clobber each other. Collapse all of each into a
 * single flag (order-preserving, de-duped) so defaults from different layers + the user's own flags
 * coexist. */
export function mergeFeatureFlags(args: string[]): string[] {
  const enabled: string[] = [];
  const disabled: string[] = [];
  const rest: string[] = [];
  for (const a of args) {
    if (a.startsWith("--enable-features=")) enabled.push(...a.slice(18).split(",").filter(Boolean));
    else if (a.startsWith("--disable-features=")) disabled.push(...a.slice(19).split(",").filter(Boolean));
    else rest.push(a);
  }
  const dedupe = (xs: string[]) => [...new Set(xs)];
  if (enabled.length) rest.push(`--enable-features=${dedupe(enabled).join(",")}`);
  if (disabled.length) rest.push(`--disable-features=${dedupe(disabled).join(",")}`);
  return rest;
}

/** Disable Privacy Sandbox + intrusive APIs (runtime, no rebuild). */
export function privacySandboxArgs(): string[] {
  return [`--disable-features=${PRIVACY_SANDBOX_FEATURES.join(",")}`];
}

/** Behind a proxy, real Chrome cannot use QUIC/HTTP3 (a SOCKS5/HTTP proxy carries only TCP), so it
 * falls back to TCP. Disable QUIC when a proxy is configured so no HTTP/3 UDP is attempted —
 * coherent with proxied Chrome, and a guarantee no UDP egresses around the proxy. No proxy -> leave
 * QUIC on (real Chrome uses it). */
export function quicArgs(proxy: PwProxy | undefined): string[] {
  return proxy && proxy.server ? ["--disable-quic"] : [];
}

/** Carry WebRTC's UDP through the SOCKS5 proxy with UDP ASSOCIATE (RFC 1928 §7) instead of letting
 * it egress on the host's own path.
 *
 * This is the transport the {@link webrtcDefaultDenyArgs} note asks for. That default sets
 * `disable_non_proxied_udp`, which on stock Chromium means "no UDP at all", because stock Chromium
 * has no way to proxy a datagram — so peer connections that genuinely need UDP simply fail. With
 * this option the engine opens a UDP association through the proxy and relays every datagram over
 * it, so UDP works AND still leaves from the proxy's address. The two compose: measured against the
 * proxy's own log, the association is established with the deny policy in force, so enabling this
 * does not require weakening the policy.
 *
 * Emitted only for a `socks5://` proxy. UDP ASSOCIATE is a SOCKS5 command — SOCKS4 has no
 * equivalent, and an HTTP proxy carries only TCP — so with any other scheme the switch would be
 * accepted and silently do nothing, which is worse than not sending it.
 *
 * Needs a PRO engine 151 r17+; older binaries ignore the switch. Off by default: it is a real
 * behaviour change (UDP starts flowing where the deny policy previously stopped it), and a proxy
 * that advertises SOCKS5 but refuses ASSOCIATE — common among cheap residential pools — leaves the
 * connection no worse off but no better either. */
export function socks5UdpArgs(socks5Udp: boolean | undefined, proxy: PwProxy | undefined): string[] {
  if (socks5Udp !== true) return [];
  const server = (proxy?.server ?? "").trim();
  return /^socks5/i.test(server) ? ["--socks5-udp"] : [];
}

/** Default WebRTC to disable_non_proxied_udp, so no UDP can egress around the proxy.
 *
 * This used to be skipped whenever `webrtcIp` was set, on the theory that the engine's srflx
 * fabrication already covered WebRTC. It does not — the two defend different things:
 *
 *   - fabrication rewrites what the browser *reports*, which beats a page that reads the candidate;
 *   - this policy stops UDP *leaving the machine*, which beats a server that watches where packets
 *     arrive from.
 *
 * A page that sets `iceTransportPolicy: "relay"` forces the browser to talk to its own TURN server.
 * TURN prefers UDP and an HTTP/SOCKS proxy carries only TCP, so that UDP left on the host's own
 * path and the TURN server read the real public address straight off the packet — no candidate
 * involved, so fabricating one changed nothing. Reported by a customer whose session was flagged
 * for location spoofing with an otherwise perfectly coherent persona.
 *
 * Worse, `geoip: true` sets `webrtcIp` for you, so the more carefully a caller configured for
 * coherence the more likely they had silently lost this. Now only an explicit policy from the
 * caller suppresses it.
 *
 * Note this is a real trade-off, not a free win: denying non-proxied UDP means peer connections
 * that genuinely need UDP will not establish. Callers who need working WebRTC through a proxy want
 * a transport that actually carries UDP (SOCKS5 with UDP ASSOCIATE, or a full tunnel) and can set
 * their own policy to opt out. */
/**
 * Switches to expose `navigator.bluetooth` on Linux hosts (empty elsewhere).
 *
 * Web Bluetooth is compiled into the engine but runtime-disabled on Linux only:
 * Chromium's runtime_enabled_features.json5 gives WebBluetooth status "stable" on Win/Mac/Android/
 * ChromeOS and lets Linux fall through to "default": "experimental", and content_features.cc
 * declares kWebBluetooth FEATURE_DISABLED_BY_DEFAULT. So a Linux host serving a Windows persona
 * reports navigator.usb, navigator.serial and navigator.hid but NOT navigator.bluetooth - a
 * combination no real Windows Chrome produces, and an OS-origin tell that survives every string
 * spoof. One flag restores it on the shipped binary; no rebuild is involved.
 * 
 * Verified against Chromium 150's bluetooth.idl: getDevices() is gated on WebBluetoothGetDevices
 * and requestLEScan()/onadvertisementreceived on WebBluetoothScanning, both "experimental", so
 * real stable Chrome exposes exactly {constructor, getAvailability, requestDevice} - which is what
 * this flag produces. getAvailability() resolves false and requestDevice() rejects NotFoundError
 * on a machine with no adapter, matching a real desktop without Bluetooth hardware.
 */
export function webBluetoothArgs(): string[] {
  if (process.platform !== "linux") return []; // Win/Mac ship it stable
  return ["--enable-features=WebBluetooth"];
}

export function webrtcDefaultDenyArgs(args: string[], _webrtcIp?: unknown): string[] {
  if (args.some((a) => a.startsWith("--webrtc-ip-handling-policy") || a.startsWith("--force-webrtc-ip-handling-policy"))) {
    return [];
  }
  return ["--webrtc-ip-handling-policy=disable_non_proxied_udp"];
}

/** Switches to load unpacked extensions. Chromium needs BOTH --load-extension=<dirs> and
 * --disable-extensions-except=<dirs> (the latter keeps the listed extensions enabled while
 * everything else stays off). `paths` is a list of unpacked-extension directories. */
/** Switches that keep the cookie encryption key with the PROFILE rather than the OS keystore, so
 * the whole user data directory can be copied to another machine and still decrypt.
 *
 * `encryptionKey` derives the key from a caller-supplied secret and writes nothing to disk — prefer
 * it when the profile is synced to shared storage. `portableProfile` generates a key and stores it
 * in the profile, which is convenient but means the cookie database is effectively unencrypted at
 * rest (inherent to portability, not a flaw in it). */
export function portableArgs(portableProfile?: boolean, encryptionKey?: string): string[] {
  if (encryptionKey) return [`--profile-encryption-key=${encryptionKey}`];
  return portableProfile ? ["--portable-profile"] : [];
}

export function extensionArgs(paths?: string[]): string[] {
  if (!paths || paths.length === 0) return [];
  const joined = paths.join(",");
  return [`--load-extension=${joined}`, `--disable-extensions-except=${joined}`];
}

/** Resolve a Playwright proxy descriptor. Playwright rejects credentials in its proxy descriptor
 * for SOCKS schemes, so a socks5://user:pass@host:port proxy (the most common residential-proxy
 * shape) makes launch() fail outright. Route such a proxy through the --proxy-server engine switch
 * so the launch proceeds, and drop it from the Playwright options.
 *
 * The credentials are forwarded to the engine as --socks5-credentials: clearcote implements RFC
 * 1929 username/password authentication, which stock Chromium does not, so no local relay is
 * needed. Everything else (http/https, or SOCKS without credentials) is left to Playwright. */
const switchCache = new Map<string, boolean>();

/**
 * Whether the engine binary that will run implements the command-line switch `name`.
 *
 * Chromium switch names are NUL-terminated C-string literals in the binary, so a NUL-delimited
 * search is a capability probe that cannot collide with header names (HPACK's `proxy-authenticate`
 * is not `\0proxy-auth\0`). On Windows the switches live in chrome.dll next to the launcher;
 * elsewhere in the executable. Cached per (path, size, mtime). Any failure answers false, which
 * selects the legacy (Playwright) path — slower, never silently broken.
 */
export function engineSupportsSwitch(exe: string | undefined, name: string): boolean {
  try {
    if (!exe) return false;
    let file = exe;
    if (process.platform === "win32") {
      const dll = join(dirname(exe), "chrome.dll");
      if (existsSync(dll)) file = dll;
    }
    const st = statSync(file);
    const key = `${file}|${name}|${st.size}|${Math.floor(st.mtimeMs)}`;
    const cached = switchCache.get(key);
    if (cached !== undefined) return cached;
    const needle = Buffer.from(`\0${name}\0`, "latin1");
    const fd = openSync(file, "r");
    let found = false;
    try {
      const chunk = Buffer.alloc(8 * 1024 * 1024);
      let tail = Buffer.alloc(0);
      for (;;) {
        const n = readSync(fd, chunk, 0, chunk.length, null);
        if (n <= 0) break;
        const view = Buffer.concat([tail, chunk.subarray(0, n)]);
        if (view.indexOf(needle) !== -1) { found = true; break; }
        tail = Buffer.from(view.subarray(view.length - needle.length));
      }
    } finally {
      closeSync(fd);
    }
    switchCache.set(key, found);
    return found;
  } catch {
    return false;
  }
}

/**
 * Warn (never throw) for each option the resolved engine cannot honour: Chromium ignores an
 * unknown switch, so an older engine launches fine — but silently, which is worse than a warning.
 */
export function warnUnsupportedEngineOptions(exe: string | undefined, fingerprint: Record<string, unknown>, proxy: PwProxy | undefined, quiet?: boolean): string[] {
  const out: string[] = [];
  try {
    if (String(fingerprint.personaSchema ?? "") === "2" && !engineSupportsSwitch(exe, "fingerprint-schema"))
      out.push("clearcote: personaSchema: 2 (engine r19+) is not supported by this engine build and is ignored; upgrade the engine to use it.");
    if (fingerprint.realGpuHost && !engineSupportsSwitch(exe, "fingerprint-gpu-backend-real"))
      out.push("clearcote: realGpuHost (engine r19+) is not supported by this engine build and is ignored; upgrade the engine to use it.");
    const server = (proxy?.server ?? "").trim();
    if (server && /^socks/i.test(server) && (proxy?.username || proxy?.password) && !engineSupportsSwitch(exe, "socks5-credentials"))
      out.push("clearcote: this engine build cannot authenticate to a SOCKS5 proxy (needs r17+); the proxy will reject the connection.");
    if (!quiet) for (const m of out) console.warn(m);
  } catch { /* never block a launch over a warning */ }
  return out;
}

export function resolveProxy(proxy: PwProxy | undefined, engineSupportsProxyAuth = false): { args: string[]; proxy: PwProxy | undefined } {
  if (!proxy || typeof proxy !== "object") return { args: [], proxy };
  const server = (proxy.server ?? "").trim();
  const hasCreds = !!(proxy.username || proxy.password);
  const isSocks = /^socks/i.test(server);
  // http(s) credentials go to the engine only when it implements --proxy-auth (r19+). Older
  // engines keep Playwright's handling: it works, at the cost of the interception side effects.
  // Routing blindly would strip the credentials from Playwright and hand them to a switch the
  // engine ignores: every request 407s.
  const isHttp = /^https?:\/\//i.test(server) && engineSupportsProxyAuth;
  if (server && hasCreds && (isSocks || isHttp)) {
    // Strip any userinfo already in the URL; the engine takes it via its own switch. Userinfo left
    // in --proxy-server is rejected by Chromium's proxy parser and the entry dropped (DIRECT).
    const bare = server.replace(/^([a-zA-Z0-9+.-]+:\/\/)[^/@]*@/, "$1");
    const creds = `${proxy.username ?? ""}:${proxy.password ?? ""}`;
    // An http(s) proxy WITH credentials also goes to the engine (--proxy-auth, clearcote r19+):
    // credentials given to Playwright make its driver enable Fetch interception and
    // Network.setCacheDisabled for the whole context -- a transport tell unrelated to the persona.
    const args = [`--proxy-server=${bare}`, `${isSocks ? "--socks5-credentials=" : "--proxy-auth="}${creds}`];
    const bypass = (proxy.bypass ?? "").trim();
    if (bypass) args.push(`--proxy-bypass-list=${bypass}`);
    return { args, proxy: undefined };
  }
  return { args: [], proxy };
}
