// Maps the SDK's fingerprint options to the Clearcote Chromium command-line switches.
// Switch names mirror components/ungoogled/ungoogled_switches.cc (see patches/000-fingerprint-switches.patch).

export interface FingerprintOptions {
  /**
   * Master fingerprint seed (the per-eTLD+1 farbling root). Same seed => the same stable
   * identity across launches; different seeds => unlinkable identities. Any string or number
   * is accepted (non-numeric seeds are hashed engine-side — they will not crash the renderer).
   */
  fingerprint?: string | number;
  /** Spoofed OS family for UA / UA-CH / navigator.platform. */
  platform?: "windows" | "linux" | "macos";
  /** Spoofed platform version (UA-CH high-entropy). */
  platformVersion?: string;
  /** Browser brand for UA / UA-CH. */
  brand?: "Chrome" | "Edge" | "Opera" | "Vivaldi" | (string & {});
  /** Brand version. */
  brandVersion?: string;
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
  /** Turn OFF GPU/WebGL fingerprint spoofing (advertise the real backend). */
  disableGpuFingerprint?: boolean;
}

const FINGERPRINT_KEYS: (keyof FingerprintOptions)[] = [
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

/** Build the Chromium switches for a set of fingerprint options. */
export function fingerprintArgs(o: FingerprintOptions): string[] {
  const args: string[] = [];
  const set = (flag: string, value: unknown) => {
    if (value !== undefined && value !== null && value !== "") {
      args.push(`--${flag}=${value}`);
    }
  };
  set("fingerprint", o.fingerprint);
  set("fingerprint-platform", o.platform);
  set("fingerprint-platform-version", o.platformVersion);
  set("fingerprint-brand", o.brand);
  set("fingerprint-brand-version", o.brandVersion);
  set("fingerprint-gpu-vendor", o.gpuVendor);
  set("fingerprint-gpu-renderer", o.gpuRenderer);
  set("fingerprint-hardware-concurrency", o.hardwareConcurrency);
  set("fingerprint-location", o.location);
  set("timezone", o.timezone);
  if (o.acceptLanguage) args.push(`--accept-lang=${cleanAcceptLanguage(String(o.acceptLanguage))}`);
  set("webrtc-ip", o.webrtcIp);
  if (o.disableGpuFingerprint) args.push("--disable-gpu-fingerprint");
  return args;
}
