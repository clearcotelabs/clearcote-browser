// Launch-time option helpers that are NOT fingerprint switches: unpacked-extension loading and
// proxy resolution. Pure (input -> switches / cleaned proxy) so they're unit-testable and mirror
// the Python SDK exactly.

import { hostPersonaPlatform } from "./fingerprint.js";
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

/** Playwright's own `--disable-features` list (chromiumSwitches.js `disabledFeatures`, 1.57).
 * Playwright puts it on every launch, BEFORE the caller's args, and Chromium keeps only the last
 * `--disable-features` on the line — so the SDK re-emits this list itself, merged into its own
 * single switch (see {@link playwrightFeatureOverrideArgs}), minus the entries a page can observe.
 * An entry a newer Playwright adds and this copy lacks ends up enabled, i.e. as in genuine Chrome. */
export const PLAYWRIGHT_DISABLED_FEATURES = [
  "AcceptCHFrame", "AvoidUnnecessaryBeforeUnloadCheckSync", "DestroyProfileOnBrowserClose",
  "DialMediaRouteProvider", "GlobalMediaControls", "HttpsUpgrades", "LensOverlay", "MediaRouter",
  "PaintHolding", "ThirdPartyStoragePartitioning", "Translate", "AutoDeElevate", "RenderDocument",
  "OptimizationHints",
] as const;

/** Entries of {@link PLAYWRIGHT_DISABLED_FEATURES} that change what a web page can observe, kept
 * ENABLED.
 *
 * ThirdPartyStoragePartitioning: every Chrome since 115 partitions third-party storage, whatever
 * the user's cookie settings. With Playwright's switch it is off, so a cross-site iframe either
 * reads the storage its site wrote as a top-level page, or — with third-party cookies blocked, the
 * engine default — gets a SecurityError from localStorage. Measured 2026-10-06 (r30, genuine
 * Chrome 154.0.8037.98 on the same host): genuine gives the iframe an empty partition (null, no
 * error) with third-party cookies allowed AND blocked; genuine launched with Playwright's defaults
 * reproduces both clearcote results exactly, so the switch is the whole cause. */
export const PAGE_VISIBLE_PLAYWRIGHT_FEATURES: ReadonlySet<string> = new Set(["ThirdPartyStoragePartitioning"]);

/** `--disable-features` that replaces Playwright's own, minus {@link PAGE_VISIBLE_PLAYWRIGHT_FEATURES}.
 *
 * Only for launches Playwright starts (launch / launchPersistentContext); serve() starts Chromium
 * itself and never carries Playwright's list. mergeFeatureFlags then folds this into the SDK's
 * single `--disable-features`, which Playwright places after its own.
 *
 * Returns [] when the caller already dropped Playwright's switch — `ignoreDefaultArgs: true`, or a
 * list naming a `--disable-features=` value — because re-adding the list would disable features
 * that launch otherwise has on. */
export function playwrightFeatureOverrideArgs(ignoreDefaultArgs?: string[] | boolean): string[] {
  if (ignoreDefaultArgs === true) return [];
  if (Array.isArray(ignoreDefaultArgs) && ignoreDefaultArgs.some((a) => String(a).startsWith("--disable-features="))) {
    return [];
  }
  const keep = PLAYWRIGHT_DISABLED_FEATURES.filter((f) => !PAGE_VISIBLE_PLAYWRIGHT_FEATURES.has(f));
  return [`--disable-features=${keep.join(",")}`];
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
 * Switch to make `navigator.bluetooth` match the platform the page is TOLD it is.
 *
 * Web Bluetooth is compiled into the engine but its *default* follows the build platform:
 * Chromium's runtime_enabled_features.json5 gives WebBluetooth status "stable" on Win/Mac/Android/
 * ChromeOS and lets Linux fall through to "default": "experimental", and content_features.cc
 * declares kWebBluetooth FEATURE_DISABLED_BY_DEFAULT. So the same persona is a tell in opposite
 * directions depending on which build is running, and neither is what the page should see:
 *   - a Linux build serving a WINDOWS (or macOS/Android) persona reports navigator.usb, serial and
 *     hid but NOT navigator.bluetooth, which no real Windows Chrome produces.
 *   - a Windows build serving a LINUX claim reports navigator.bluetooth, which genuine Chrome on
 *     Linux does not have. Measured 2026-10-04 against genuine Chrome 154 on Linux:
 *     `'bluetooth' in navigator` is false (and ours answered getAvailability() === false, i.e.
 *     "API present, no adapter", a state genuine Linux Chrome cannot produce).
 * Hence the switch is derived from the CLAIM alone and the host is never consulted: whichever way
 * the build's default falls, one of the two switches lands the page on the right answer, and the
 * one that agrees with the default is a no-op. Reading process.platform here was the original bug,
 * and reading it to decide whether to bother was the half-fix: skipping the disable off Linux left
 * a Windows host serving a Linux persona exposing the API (measured on the r30 Windows build).
 *
 * Verified against Chromium 150's bluetooth.idl: getDevices() is gated on WebBluetoothGetDevices
 * and requestLEScan()/onadvertisementreceived on WebBluetoothScanning, both "experimental", so
 * real stable Chrome exposes exactly {constructor, getAvailability, requestDevice} - which is what
 * the enable switch produces. getAvailability() resolves false and requestDevice() rejects
 * NotFoundError on a machine with no adapter, matching a real desktop without Bluetooth hardware.
 */
/** Platforms whose stable Chrome ships Web Bluetooth. */
export const WEB_BLUETOOTH_PLATFORMS = ["windows", "macos", "mac", "android", "chromeos"];

export function webBluetoothArgs(claimedPlatform?: string): string[] {
  const claimed = (claimedPlatform ?? hostPersonaPlatform()).trim().toLowerCase();
  if (WEB_BLUETOOTH_PLATFORMS.includes(claimed)) return ["--enable-features=WebBluetooth"];
  // a Linux claim has no navigator.bluetooth on genuine Chrome, whatever this build's default is
  return ["--disable-features=WebBluetooth"];
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
    const { server, username, password } = proxy ? proxyCredentials(proxy) : { server: "", username: "", password: "" };
    if (server && /^socks/i.test(server) && (username || password) && !engineSupportsSwitch(exe, "socks5-credentials"))
      out.push("clearcote: this engine build cannot authenticate to a SOCKS5 proxy (needs r17+); the proxy will reject the connection.");
    if (!quiet) for (const m of out) console.warn(m);
  } catch { /* never block a launch over a warning */ }
  return out;
}

function decodeUserinfo(s: string): string {
  try { return decodeURIComponent(s); } catch { return s; } // malformed %-escape: take it literally
}

/**
 * The credentials a proxy descriptor carries, and its server with any userinfo removed.
 *
 * `socks5://user:pass@host:1080` is the shape most proxy providers hand out, so credentials written
 * into the URL count exactly like the `username`/`password` fields (which win when both are given,
 * as in {@link toProxySpec}). Userinfo is percent-decoded, like a browser reads it, and split at the
 * LAST '@' of the authority, like the WHATWG URL parser, so an unescaped '@' in a password survives.
 */
export function proxyCredentials(proxy: PwProxy): { server: string; username: string; password: string } {
  const raw = (proxy.server ?? "").trim();
  let server = raw;
  let urlUser = "";
  let urlPass = "";
  const m = /^([a-zA-Z][a-zA-Z0-9+.-]*:\/\/)([^/?#]*)(.*)$/s.exec(raw);
  const at = m ? m[2].lastIndexOf("@") : -1;
  if (m && at >= 0) {
    const info = m[2].slice(0, at);
    const colon = info.indexOf(":");
    urlUser = decodeUserinfo(colon < 0 ? info : info.slice(0, colon));
    urlPass = colon < 0 ? "" : decodeUserinfo(info.slice(colon + 1));
    server = m[1] + m[2].slice(at + 1) + m[3];
  }
  return { server, username: proxy.username || urlUser, password: proxy.password || urlPass };
}

export function resolveProxy(proxy: PwProxy | undefined, engineSupportsProxyAuth = false): { args: string[]; proxy: PwProxy | undefined } {
  if (!proxy || typeof proxy !== "object") return { args: [], proxy };
  // Credentials count wherever they were written. Reading only the fields used to send a
  // socks5://user:pass@host proxy to Playwright, which rebuilds the server as scheme://host:port:
  // the browser then offered the proxy no authentication at all.
  const { server, username, password } = proxyCredentials(proxy);
  const hasCreds = !!(username || password);
  const isSocks = /^socks/i.test(server);
  // http(s) credentials go to the engine only when it implements --proxy-auth (r19+). Older
  // engines keep Playwright's handling: it works, at the cost of the interception side effects.
  // Routing blindly would strip the credentials from Playwright and hand them to a switch the
  // engine ignores: every request 407s.
  const isHttp = /^https?:\/\//i.test(server) && engineSupportsProxyAuth;
  if (server && hasCreds && (isSocks || isHttp)) {
    // `server` has no userinfo; the engine takes it via its own switch. Userinfo left in
    // --proxy-server is rejected by Chromium's proxy parser and the entry dropped: every request
    // then fails with ERR_NO_SUPPORTED_PROXIES (measured on r27).
    // An http(s) proxy WITH credentials also goes to the engine (--proxy-auth, clearcote r19+):
    // credentials given to Playwright make its driver enable Fetch interception and
    // Network.setCacheDisabled for the whole context -- a transport tell unrelated to the persona.
    const args = [`--proxy-server=${server}`, `${isSocks ? "--socks5-credentials=" : "--proxy-auth="}${username}:${password}`];
    const bypass = (proxy.bypass ?? "").trim();
    if (bypass) args.push(`--proxy-bypass-list=${bypass}`);
    return { args, proxy: undefined };
  }
  // Left to Playwright. It drops userinfo from the server, so credentials written there are handed
  // over as the fields it reads (an http proxy on an engine without --proxy-auth).
  if (server !== (proxy.server ?? "").trim()) {
    return { args: [], proxy: { ...proxy, server, ...(username ? { username } : {}), ...(password ? { password } : {}) } };
  }
  return { args: [], proxy };
}

// ── GPU launch defaults ─────────────────────────────────────────────────────────────────────────

/**
 * Playwright launch defaults the SDK removes.
 *
 * `--enable-automation` keeps the engine's AutomationControlled feature off. `--enable-unsafe-swiftshader`
 * is added by Playwright (1.49+) to every Chromium launch; it lets WebGL fall back to SwiftShader
 * software rendering, which real Chrome no longer does for WebGL. Stripping it on its own is NOT
 * safe: measured on a GPU-less Linux host, a HEADED launch then has no WebGL at all. It is only
 * removed together with {@link gpuBlocklistArgs}, which restores WebGL through the normal GPU path.
 * `--hide-scrollbars` is added by Playwright to every HEADLESS launch; with it the page measures 0 px
 * scrollbars, while genuine Chrome shows 15 px on Windows -- headed OR plain `--headless=new` (measured on
 * Chrome 154). Stripping it only restores Chrome's own default; it is a no-op headed.
 * A caller's own `ignoreDefaultArgs` always wins.
 */
export const DEFAULT_IGNORED_ARGS: readonly string[] = ["--enable-automation", "--enable-unsafe-swiftshader", "--hide-scrollbars"];

/**
 * `--ignore-gpu-blocklist` for headed launches and for every launch on Windows.
 *
 * Headed on a host without a usable GPU (a VPS under Xvfb), Chromium's blocklist disables WebGL
 * outright once the SwiftShader fallback flag is gone; this flag lets WebGL run anyway. On Windows,
 * the blocklist also refuses WebGPU on the Microsoft Basic Render Driver found on GPU-less VMs.
 * Headless Linux already renders WebGL through SwiftShader regardless (measured), so nothing is
 * added there. Not added when the caller already passes it.
 */
export function gpuBlocklistArgs(headed: boolean, platform: NodeJS.Platform = process.platform, userArgs: readonly string[] = []): string[] {
  if (!headed && platform !== "win32") return [];
  if (userArgs.includes("--ignore-gpu-blocklist")) return [];
  return ["--ignore-gpu-blocklist"];
}

// ── new engine switches (152 r22+) ──────────────────────────────────────────────────────────────

/** Switches introduced in engine 152 r22, with what the caller asked for. Gated per binary. */
export const GATED_ENGINE_SWITCHES: Readonly<Record<string, string>> = {
  "--fingerprint-passthrough": "fingerprint: \"off\" (pass-through debug mode)",
  "--disable-fingerprint-voices": "fingerprintVoices: false",
  "--allow-third-party-cookies": "allowThirdPartyCookies: true",
  "--transparent-proxy": "transparentProxy: true",
};

/**
 * Drop any 152 r22+ switch the engine that will run does not implement, with a warning.
 *
 * Chromium ignores unknown switches silently, so an older engine would launch without the feature
 * and without saying so. Detection is the same NUL-delimited literal probe as {@link engineSupportsSwitch}.
 */
export function gateEngineSwitches(exe: string | undefined, args: string[], quiet?: boolean): { args: string[]; warnings: string[] } {
  const warnings: string[] = [];
  const out = args.filter((a) => {
    const name = a.split("=")[0];
    const what = GATED_ENGINE_SWITCHES[name];
    if (!what) return true;
    if (engineSupportsSwitch(exe, name.slice(2))) return true;
    warnings.push(`clearcote: ${what} needs engine 152 r22 or newer; this engine ignores it, so it was not applied.`);
    return false;
  });
  if (!quiet) for (const w of warnings) console.warn(w);
  return { args: out, warnings };
}

/**
 * Launch switches for the non-fingerprint engine options.
 *
 * `transparentProxy` is only meaningful with a proxy: it removes what an origin or page can observe
 * about the proxy (the `Proxy-Connection` header on plain-HTTP requests, and proxy-shaped
 * DNS/connect/TLS timing). Without a proxy it is dropped with a note.
 */
export function engineExtrasArgs(
  o: { allowThirdPartyCookies?: boolean; transparentProxy?: boolean },
  proxy: PwProxy | undefined,
  quiet?: boolean,
): string[] {
  const args: string[] = [];
  if (o.allowThirdPartyCookies === true) args.push("--allow-third-party-cookies");
  if (o.transparentProxy === true) {
    if (proxy?.server) args.push("--transparent-proxy");
    else if (!quiet) console.warn("clearcote: transparentProxy has no effect without a proxy; ignored.");
  }
  return args;
}
