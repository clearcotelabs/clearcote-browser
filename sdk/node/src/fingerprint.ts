// Maps the SDK's fingerprint options to the Clearcote Chromium command-line switches.
// Switch names mirror components/ungoogled/ungoogled_switches.cc (see patches/000-fingerprint-switches.patch).

import { gzipSync } from "node:zlib";
import { existsSync, readFileSync } from "node:fs";

export interface FingerprintOptions {
  /**
   * Master fingerprint seed (the per-eTLD+1 farbling root). Same seed => the same stable
   * identity across launches; different seeds => unlinkable identities. Any string or number
   * is accepted (non-numeric seeds are hashed engine-side — they will not crash the renderer).
   */
  fingerprint?: string | number;
  /**
   * Spoofed OS family for UA / UA-CH / navigator.platform. `"android"` is a best-effort **mobile**
   * persona (seed-selected from a Pixel/Galaxy device pool): Android UA + `Sec-CH-UA-Mobile: ?1`,
   * touch (`maxTouchPoints`), `pointer:coarse`/`hover:none`, mobile screen/DPR, Mali/Adreno WebGL,
   * `plugins=0`, and a mobile viewport (auto-sets a phone `--window-size`). On a desktop engine the
   * GPU *render* and fine page geometry stay desktop (documented residual tells) — pair with
   * `canvasBridge` for render coherence.
   */
  platform?: "windows" | "linux" | "macos" | "android";
  /** Spoofed platform version (UA-CH high-entropy). */
  platformVersion?: string;
  /** Browser brand for UA / UA-CH. */
  brand?: "Chrome" | "Edge" | "Opera" | "Vivaldi" | (string & {});
  /** Brand version. */
  brandVersion?: string;
  /**
   * TLS network persona — keep the TLS ClientHello coherent with the persona's claimed Chrome
   * version, so the network layer follows the UA instead of always emitting the build's native TLS.
   * Only the version-variant ClientHello fields change (post-quantum key-share group, ALPS
   * codepoint); the cipher list, version bounds, and per-connection extension permutation stay
   * exactly real-Chrome. Chromium-core only (Chrome/Edge/Brave/Opera share the ClientHello; brand
   * differs in headers/UA-CH, not TLS).
   *
   * - `"match-persona"` (default) / `"auto"`: follow `brandVersion`'s major; with no `brandVersion`
   *   the persona claims the browser's native version, so this is a no-op (native TLS).
   * - `"native"` / `"off"`: leave the build's native TLS unchanged.
   * - `"chrome-<major>"` or a number (e.g. `120`): pin the TLS shape to that Chromium major.
   */
  tlsProfile?: "match-persona" | "auto" | "native" | "off" | `chrome-${number}` | (string & {}) | number;
  /** WebGL UNMASKED_VENDOR string. */
  gpuVendor?: string;
  /** WebGL UNMASKED_RENDERER string. */
  gpuRenderer?: string;
  /** navigator.hardwareConcurrency. */
  hardwareConcurrency?: number;
  /** Geolocation as "lat,lng" (only returned when the page is granted permission). */
  location?: string;
  /** IANA timezone, e.g. "America/New_York". */
  timezone?: string;
  /**
   * Accept-Language / navigator.languages, e.g. "en-US,en". Sets BOTH the server-side
   * Accept-Language header and navigator.language(s) coherently (Chromium `--accept-lang`).
   * Set this to match the timezone/proxy region — or let `geoip` fill it automatically.
   */
  acceptLanguage?: string;
  /** WebRTC egress IP to report (typically your proxy's public IP). */
  webrtcIp?: string;
  /**
   * Present the machine's **real GPU** instead of a spoofed one. WebGL `UNMASKED_VENDOR`/`RENDERER`,
   * the `getParameter` table, and the canvas/WebGL render all report the genuine host backend. This
   * is the most coherent setting against strict browser-tampering classifiers: the GPU string and
   * the actually-rendered pixels match, so there is no GPU spoof to catch (the same reason a stock
   * browser passes). Composes with `fingerprintProfile` — the profile still supplies
   * screen/fonts/audio/hardware, but the **real host GPU is kept** instead of the profile's GPU
   * (which the host cannot actually render, and would otherwise be a string-vs-render mismatch).
   * Pair with `fingerprintNoise: false` so the canvas/WebGL readback isn't perturbed either.
   *
   * Trade-off: every persona on one physical machine then shares the same canvas/WebGL identity, so
   * a tracker can link them by GPU hash. Best for single-identity / one-persona-per-host use; for
   * multi-account-on-one-machine, keep this off and instead match the profile's GPU to the host.
   */
  disableGpuFingerprint?: boolean;
  /**
   * Set `false` to turn OFF the per-eTLD+1 farbling NOISE (canvas/WebGL/audio/client-rects), so
   * those surfaces return their natural, unperturbed values. Use when a site's anti-bot ML scores
   * the noise pattern as "tampered". Identity spoofs (UA/screen/GPU/persona)
   * stay on. Default (unset/`true`) keeps the noise.
   */
  fingerprintNoise?: boolean;
  /**
   * Import a real captured fingerprint so the browser presents *that machine's* identity instead
   * of the synthetic seed-derived one. Accepts a path to a `.json` profile, a profile object, or a
   * JSON string (capture one with `tools/fingerprint-collect`). Fields present in the profile
   * override the seed-derived persona; absent fields fall back to the seed, so partial profiles
   * stay coherent.
   */
  fingerprintProfile?: string | Record<string, unknown>;
  /**
   * `navigator.storage.estimate().quota` in **megabytes**. A tiny/ephemeral quota reads as a test
   * machine or incognito; set a realistic on-disk value (e.g. `250000` for ~244 GB).
   */
  storageQuota?: number;
  /**
   * Canvas bridge — forward canvas/WebGL readbacks to a remote real-GPU host so the pixels a page
   * hashes are coherent with the GPU your persona claims. Setting `url` enables it and auto-adds
   * `--no-sandbox` (the bridge opens its socket from the renderer process).
   *
   * Latency note: a synchronous readback over the bridge is a network round-trip on the renderer
   * thread (a timing signal vs latency-aware detectors). Mitigations are built in — the engine
   * prefetches+caches so deferred/animated/repeated reads don't block — and you can tune behavior
   * here: `mode` restricts bridging to the origins where canvas coherence is actually scored, and
   * `fallback: "local"` makes a cold cache miss serve the fast local render instead of stalling.
   */
  canvasBridge?: {
    /** Bridge endpoint, "ws://host:port[/path]". Required to enable the bridge. */
    url: string;
    /** HTTP Basic credentials "user:secret"; must match the server. */
    auth?: string;
    /** Per-origin policy: "off" | "all" (default) | "allow" | "deny". */
    mode?: "off" | "all" | "allow" | "deny";
    /** eTLD+1 list bridged when mode="allow". */
    allow?: string[];
    /** eTLD+1 list NOT bridged when mode="deny". */
    deny?: string[];
    /** Cold cache-miss behavior: "block" (default) | "local" (never stall; render locally). */
    fallback?: "block" | "local";
  };
}

export const FINGERPRINT_KEYS: (keyof FingerprintOptions)[] = [
  "fingerprint",
  "platform",
  "platformVersion",
  "brand",
  "brandVersion",
  "gpuVendor",
  "gpuRenderer",
  "hardwareConcurrency",
  "location",
  "timezone",
  "acceptLanguage",
  "webrtcIp",
  "disableGpuFingerprint",
  "fingerprintNoise",
  "fingerprintProfile",
  "storageQuota",
  "canvasBridge",
  "tlsProfile",
];

/** Split an options object into its fingerprint half and the remaining (Playwright) half. */
export function splitFingerprintOptions<T extends FingerprintOptions>(
  options: T
): { fingerprint: FingerprintOptions; rest: Omit<T, keyof FingerprintOptions> } {
  const fingerprint: FingerprintOptions = {};
  const rest: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(options)) {
    if ((FINGERPRINT_KEYS as string[]).includes(k)) {
      (fingerprint as Record<string, unknown>)[k] = v;
    } else {
      rest[k] = v;
    }
  }
  return { fingerprint, rest: rest as Omit<T, keyof FingerprintOptions> };
}

/** Normalize an Accept-Language value for Chromium's `--accept-lang`: a plain comma-separated
 * tag list with NO `;q=` weights or spaces (Chromium adds the q-weights to the header itself; a
 * `;` in the switch value trips a DCHECK and crashes the renderer). */
export function cleanAcceptLanguage(v: string): string {
  return String(v)
    .split(",")
    .map((t) => t.split(";")[0].trim())
    .filter(Boolean)
    .join(",");
}

/** Encode a captured clearcote-profile for the `--fingerprint-profile` switch. `value` may be a
 * path to a `.json` file, a profile object, or a JSON string. gzip+base64 keeps the full capture
 * (~40 KB) within Chromium's command-line length limit (gzip ~6x); the engine gunzips + parses it. */
export function encodeProfile(value: string | Record<string, unknown>): string {
  let raw: Buffer;
  if (typeof value === "object") {
    raw = Buffer.from(JSON.stringify(value));
  } else if (existsSync(value)) {
    raw = readFileSync(value);
  } else {
    raw = Buffer.from(value); // assume a JSON string
  }
  return gzipSync(raw, { level: 9 }).toString("base64");
}

/** Best-effort: derive an Accept-Language from an imported profile's navigator.languages
 * (path / object / JSON string), so an imported identity keeps the donor's language order. */
export function profileAcceptLanguage(value: string | Record<string, unknown>): string | undefined {
  let obj: Record<string, unknown> | undefined;
  try {
    if (typeof value === "object") obj = value;
    else if (existsSync(value)) obj = JSON.parse(readFileSync(value, "utf8"));
    else obj = JSON.parse(value);
  } catch {
    return undefined;
  }
  const nav = obj?.navigator as Record<string, unknown> | undefined;
  const langs = nav?.languages;
  if (Array.isArray(langs) && langs.length) return langs.map(String).join(",");
  return undefined;
}

/** Build the Chromium switches for a set of fingerprint options. */
/** Parse the leading integer (the major) out of a version string like "120.0.6099.109" -> 120. */
function majorFromVersion(value?: string): number | undefined {
  if (value === undefined || value === null) return undefined;
  const head = String(value).trim().split(".")[0];
  return /^\d+$/.test(head) ? Number(head) : undefined;
}

/**
 * Resolve `tlsProfile` to a concrete `--fingerprint-tls-profile` value, or undefined (native TLS).
 * "match-persona"/"auto" (default) follows `brandVersion`; "native"/"off"/undefined -> native;
 * "chrome-<major>" or a number -> pinned. Unrecognized values -> native (never break the handshake).
 * Always resolves to `chrome-<major>` (Chromium-core; the brand lives in headers, not the ClientHello).
 */
export function resolveTlsProfile(
  value: FingerprintOptions["tlsProfile"],
  o: FingerprintOptions
): string | undefined {
  if (value === undefined || value === null || value === "" || value === "native" || value === "off") {
    return undefined;
  }
  if (value === "match-persona" || value === "auto") {
    const major = majorFromVersion(o.brandVersion);
    return major ? `chrome-${major}` : undefined;
  }
  if (typeof value === "number") return `chrome-${value}`;
  const text = String(value).trim().toLowerCase();
  if (/^chrome-\d+$/.test(text)) return text;
  if (/^\d+$/.test(text)) return `chrome-${text}`;
  return undefined;
}

// Primary Accept-Language tag -> a plausible IANA timezone, so the default persona's timezone is
// coherent with its locale instead of leaking the host's (often UTC on servers/containers). Not
// geo-truth — set geoip=true (resolve the proxy exit-IP) or an explicit timezone for accuracy.
const LOCALE_TZ: Record<string, string> = {
  "en-US": "America/New_York", "en-CA": "America/Toronto", "en-GB": "Europe/London",
  "en-AU": "Australia/Sydney", "en-NZ": "Pacific/Auckland", "en-IE": "Europe/Dublin",
  "de-DE": "Europe/Berlin", "de-AT": "Europe/Vienna", "fr-FR": "Europe/Paris",
  "es-ES": "Europe/Madrid", "es-MX": "America/Mexico_City", "it-IT": "Europe/Rome",
  "nl-NL": "Europe/Amsterdam", "pt-BR": "America/Sao_Paulo", "pt-PT": "Europe/Lisbon",
  "pl-PL": "Europe/Warsaw", "sv-SE": "Europe/Stockholm", "ja-JP": "Asia/Tokyo",
  "ko-KR": "Asia/Seoul", "zh-CN": "Asia/Shanghai", "zh-TW": "Asia/Taipei",
  "ru-RU": "Europe/Moscow", "tr-TR": "Europe/Istanbul", "ar-SA": "Asia/Riyadh",
  "hi-IN": "Asia/Kolkata", "id-ID": "Asia/Jakarta",
};

/**
 * A plausible IANA timezone for a primary Accept-Language tag (`en-US` -> `America/New_York`), so the
 * default persona's timezone is coherent with its locale rather than leaking the host's UTC. Falls
 * back by language subtag, then to America/New_York (matching the en-US Accept-Language default).
 */
export function defaultTimezone(primaryLang: string): string | undefined {
  if (!primaryLang) return undefined;
  const tag = primaryLang.trim();
  if (LOCALE_TZ[tag]) return LOCALE_TZ[tag];
  const lang = tag.split("-")[0].toLowerCase();
  for (const [key, tz] of Object.entries(LOCALE_TZ)) {
    if (key.toLowerCase().startsWith(lang + "-")) return tz;
  }
  return "America/New_York";
}

export function fingerprintArgs(o: FingerprintOptions): string[] {
  const args: string[] = [];
  const set = (flag: string, value: unknown) => {
    if (value !== undefined && value !== null && value !== "") {
      args.push(`--${flag}=${value}`);
    }
  };
  set("fingerprint", o.fingerprint);
  // Default the persona platform to the HOST OS so it's coherent with the binary the SDK ships for
  // this machine (Windows binary -> windows, Linux binary -> linux). Override via
  // platform: "windows" | "linux" | "macos".
  const hostPlatform =
    ({ win32: "windows", linux: "linux", darwin: "macos" } as Record<string, string>)[process.platform] ?? "windows";
  set("fingerprint-platform", o.platform ?? hostPlatform);
  set("fingerprint-platform-version", o.platformVersion);
  // clearcote presents as Google Chrome (its UA says "Chrome/<v>"); default the UA-CH brand to
  // "chrome" so navigator.userAgentData advertises "Google Chrome", not bare "Chromium" (a
  // UA/UA-CH mismatch some bot detectors flag). Override via brand: "Edge" etc.
  set("fingerprint-brand", o.brand ?? "chrome");
  set("fingerprint-brand-version", o.brandVersion);
  set("fingerprint-gpu-vendor", o.gpuVendor);
  set("fingerprint-gpu-renderer", o.gpuRenderer);
  set("fingerprint-hardware-concurrency", o.hardwareConcurrency);
  set("fingerprint-location", o.location);
  set("fingerprint-storage-quota", o.storageQuota);
  set("timezone", o.timezone);
  // Always send a coherent Accept-Language. Without --accept-lang Chromium falls back to the
  // build/OS locale, which can leak a language that mismatches the proxy's country/timezone
  // (e.g. en-GB on a US IP) — a geo-inconsistency tell. Prefer an explicit value, then an imported
  // profile's languages, then en-US,en (the common Chrome default; set acceptLanguage or geoip to
  // match the proxy region).
  const acceptLanguage =
    o.acceptLanguage ||
    (o.fingerprintProfile ? profileAcceptLanguage(o.fingerprintProfile) : undefined) ||
    "en-US,en";
  const cleanLang = cleanAcceptLanguage(String(acceptLanguage));
  args.push(`--accept-lang=${cleanLang}`);
  // Also pin the UI/ICU locale to the PRIMARY Accept-Language tag, so Intl.DateTimeFormat /
  // NumberFormat / Collator (main thread AND workers) resolve to the same locale as
  // navigator.language. Without --lang, Chromium falls back to the build/OS locale (e.g. en-GB on an
  // en-US persona) — a locale-incoherence tell auditors flag (navigator.language=en-US but Intl=en-GB).
  const primaryLang = cleanLang.split(",")[0];
  if (primaryLang) args.push(`--lang=${primaryLang}`);
  // Default the timezone to one coherent with the persona locale when none is set (and geoip didn't
  // resolve one), so a server/container run doesn't leak the host's UTC (a datacenter tell) while
  // navigator.language says e.g. en-US. geoip=True or an explicit timezone= override this.
  if (!o.timezone) {
    const tz = defaultTimezone(primaryLang);
    if (tz) args.push(`--timezone=${tz}`);
  }
  set("webrtc-ip", o.webrtcIp);
  if (o.disableGpuFingerprint) args.push("--disable-gpu-fingerprint");
  // fingerprintNoise=false turns OFF the per-eTLD+1 farbling noise (canvas/WebGL/audio/client-rects)
  // so those surfaces return natural values — for sites whose ML flags the noise as "tampered".
  // Identity spoofs (UA/screen/GPU/persona) stay on. Default keeps the noise.
  if (o.fingerprintNoise === false) args.push("--disable-fingerprint-noise");
  // fingerprintProfile imports a real captured fingerprint (path/object/JSON) — see
  // tools/fingerprint-collect. Its fields override the seed-derived persona; absent fields fall
  // back to the seed, so partial profiles stay coherent.
  if (o.fingerprintProfile) args.push(`--fingerprint-profile=${encodeProfile(o.fingerprintProfile)}`);
  // Canvas bridge: forward canvas/WebGL readbacks to a remote real-GPU host. Enabling it
  // (url set) requires --no-sandbox (the bridge opens its socket from the renderer process).
  if (o.canvasBridge?.url) {
    const cb = o.canvasBridge;
    args.push(`--canvas-bridge-url=${cb.url}`);
    if (cb.auth) args.push(`--canvas-bridge-auth=${cb.auth}`);
    if (cb.mode) args.push(`--canvas-bridge-mode=${cb.mode}`);
    if (cb.allow?.length) args.push(`--canvas-bridge-allow=${cb.allow.join(",")}`);
    if (cb.deny?.length) args.push(`--canvas-bridge-deny=${cb.deny.join(",")}`);
    if (cb.fallback) args.push(`--canvas-bridge-fallback=${cb.fallback}`);
    if (!args.includes("--no-sandbox")) args.push("--no-sandbox");
  }
  // tlsProfile keeps the TLS ClientHello coherent with the persona's claimed Chrome version (the
  // network layer follows the UA). Default "match-persona" follows brandVersion; "chrome-<major>"
  // pins it; "native"/off/unset leaves native TLS. Only version-variant fields change.
  const tlsSwitch = resolveTlsProfile(o.tlsProfile ?? "match-persona", o);
  if (tlsSwitch) args.push(`--fingerprint-tls-profile=${tlsSwitch}`);
  // The Android persona lays the page out as a mobile viewport, which needs a phone-sized window
  // (Chromium's ~500px minimum width floor still applies). Auto-set one; a caller-supplied
  // --window-size in `args` overrides this (Chromium takes the last --window-size).
  if (o.platform === "android") {
    args.push("--window-size=412,915");
  }
  return args;
}
