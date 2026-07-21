// Launch-time option helpers that are NOT fingerprint switches: unpacked-extension loading and
// proxy resolution. Pure (input -> switches / cleaned proxy) so they're unit-testable and mirror
// the Python SDK exactly.

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
export function webrtcDefaultDenyArgs(args: string[], _webrtcIp?: unknown): string[] {
  if (args.some((a) => a.startsWith("--webrtc-ip-handling-policy") || a.startsWith("--force-webrtc-ip-handling-policy"))) {
    return [];
  }
  return ["--webrtc-ip-handling-policy=disable_non_proxied_udp"];
}

/** Switches to load unpacked extensions. Chromium needs BOTH --load-extension=<dirs> and
 * --disable-extensions-except=<dirs> (the latter keeps the listed extensions enabled while
 * everything else stays off). `paths` is a list of unpacked-extension directories. */
export function extensionArgs(paths?: string[]): string[] {
  if (!paths || paths.length === 0) return [];
  const joined = paths.join(",");
  return [`--load-extension=${joined}`, `--disable-extensions-except=${joined}`];
}

/** Resolve a Playwright proxy descriptor. Playwright rejects credentials in its proxy descriptor
 * for SOCKS schemes, so a socks5://user:pass@host:port proxy (the most common residential-proxy
 * shape) makes launch() fail outright. Route such a proxy through the --proxy-server engine switch
 * so the launch proceeds, and drop it from the Playwright options. NOTE: Chromium has no SOCKS5
 * authentication, so the credentials can't be honored either way — warn to put the auth on a local
 * relay. Everything else (http/https, or SOCKS without credentials) is left to Playwright. */
export function resolveProxy(proxy: PwProxy | undefined): { args: string[]; proxy: PwProxy | undefined } {
  if (!proxy || typeof proxy !== "object") return { args: [], proxy };
  const server = (proxy.server ?? "").trim();
  const hasCreds = !!(proxy.username || proxy.password);
  if (server && /^socks/i.test(server) && hasCreds) {
    // eslint-disable-next-line no-console
    console.warn(
      "clearcote: routed a credentialed SOCKS5 proxy via --proxy-server so the launch can proceed, " +
        "but Chromium cannot authenticate SOCKS5 — the credentials are dropped. Put the authentication " +
        "on a local relay (a local SOCKS->authenticated-SOCKS bridge)."
    );
    return { args: [`--proxy-server=${server}`], proxy: undefined };
  }
  return { args: [], proxy };
}
