// Launch-time coherence warnings (mirror of the Python clearcote/_warnings.py).
//
// The SDK already defaults the safe things (strips --enable-automation, denies WebRTC leak,
// disables Privacy Sandbox, matches the persona to the build). What it CAN'T fix is an operator
// passing an incoherent or missing-recommended combination. coherenceWarnings() spots those at
// launch() and emitCoherenceWarnings() prints an actionable line to stderr. Never blocks the
// launch; suppressible with quiet:true or CLEARCOTE_NO_WARN=1. coherenceWarnings() is pure (no I/O).

const SOFTWARE_GPU = ["swiftshader", "llvmpipe", "microsoft basic render", "software adapter", "software"];
const seenNotes = new Set<string>(); // fire-once per process for NOTE codes

export interface CoherenceWarning {
  severity: "warn" | "note";
  code: string;
  message: string;
}

function proxyServer(proxy: unknown): string {
  if (!proxy) return "";
  if (typeof proxy === "object" && proxy && "server" in proxy) return String((proxy as { server?: unknown }).server ?? "");
  return String(proxy);
}
function isSocks(s: string): boolean {
  const l = s.toLowerCase();
  return l.startsWith("socks") || l.includes("://socks");
}
function hostFamily(host: string): string | null {
  if (host.startsWith("win")) return "windows";
  if (host === "darwin" || host.startsWith("mac")) return "macos";
  if (host.startsWith("linux")) return "linux";
  return null;
}
function gpuIncoherent(renderer: string, platform: string): string | null {
  const r = renderer.toLowerCase();
  if (platform === "macos" && (r.includes("direct3d") || r.includes("d3d"))) return "macOS uses Metal/OpenGL, never Direct3D";
  if (platform === "windows" && r.includes("metal")) return "Windows uses Direct3D/ANGLE, never Metal";
  if (platform === "linux" && (r.includes("direct3d") || r.includes("d3d") || r.includes("metal"))) return "Linux uses OpenGL/Vulkan, never Direct3D/Metal";
  return null;
}

/** Inspect resolved options for incoherent / missing-recommended combinations. Pure. */
export function coherenceWarnings(
  opts: Record<string, unknown>,
  hostPlatform?: string,
  buildMajor?: string
): CoherenceWarning[] {
  const host = hostPlatform ?? process.platform;
  const bmajor = buildMajor ?? "149";
  const out: CoherenceWarning[] = [];
  const warn = (code: string, message: string) => out.push({ severity: "warn", code, message });
  const note = (code: string, message: string) => out.push({ severity: "note", code, message });

  const server = proxyServer(opts.proxy);
  const geoip = Boolean(opts.geoip);
  const tz = opts.timezone, lang = opts.acceptLanguage;
  const platform = opts.platform as string | undefined;
  const brand = opts.brand as string | undefined, bver = opts.brandVersion as string | undefined;
  const gpuR = opts.gpuRenderer as string | undefined, gpuV = opts.gpuVendor;
  const profile = opts.fingerprintProfile;
  const dgf = opts.disableGpuFingerprint, noise = opts.fingerprintNoise;
  const headless = opts.headless;
  const bridge = opts.canvasBridge as { url?: unknown } | undefined;
  const bridgeOn = bridge && typeof bridge === "object" ? Boolean(bridge.url) : Boolean(bridge);
  const userArgs = (opts._userArgs as string[]) ?? [];

  if (server && !geoip && !tz && !lang)
    warn("proxy-no-geo",
      "proxy set without geoip and no timezone/acceptLanguage - the browser's timezone and language " +
      "will reflect THIS host, not the proxy's exit region (a geo-mismatch tell). Pass geoip:true, or " +
      "set timezone + acceptLanguage.");
  if (server && geoip && isSocks(server))
    warn("socks-geoip",
      "geoip cannot resolve a SOCKS proxy's exit IP - timezone/language will NOT auto-match. Set " +
      "timezone + acceptLanguage (+ webrtcIp) manually for SOCKS proxies.");

  const fam = hostFamily(host);
  if (platform && fam && platform !== fam && !profile)
    warn("platform-host-fonts",
      `platform='${platform}' but this host is ${fam} and no fingerprintProfile supplies that OS's ` +
      `fonts/metrics - font, canvas and font-list hashes will be host-native and won't match a real ` +
      `${platform} Chrome. Use a fingerprintProfile captured on ${platform}, or set platform='${fam}'.`);
  if (gpuR && platform) {
    const why = gpuIncoherent(gpuR, platform);
    if (why) warn("gpu-platform", `gpuRenderer is incoherent with platform='${platform}' (${why}): '${gpuR}'.`);
  }
  if (gpuR && SOFTWARE_GPU.some((s) => gpuR.toLowerCase().includes(s)))
    warn("gpu-software",
      `gpuRenderer is a SOFTWARE renderer ('${gpuR}') - a real consumer machine reports a hardware GPU. ` +
      "Pin a real GPU string, or use the canvas bridge / a real-GPU host.");
  if (brand && !["chrome", "google chrome"].includes(String(brand).toLowerCase()))
    warn("brand-mismatch",
      `brand='${brand}' is advertised in UA-CH, but the binary's TLS/JA4 and engine are Chrome ${bmajor} ` +
      "- a UA-vs-transport mismatch strict detectors cross-check. Keep brand='Chrome'.");
  if (bver && String(bver).split(".")[0] !== bmajor)
    warn("version-mismatch",
      `brandVersion major ${String(bver).split(".")[0]} differs from the build's Chrome ${bmajor} - ` +
      `JA4/UA-CH version desync. Align brandVersion to ${bmajor} (or omit it).`);

  if (dgf && noise !== false)
    warn("gpu-noise",
      "disableGpuFingerprint presents the REAL GPU, but per-eTLD farble still perturbs the canvas/WebGL " +
      "readback - noise on otherwise-real pixels is itself a tell. Pair with fingerprintNoise:false.");
  if (headless !== false && !bridgeOn && !dgf && !profile)
    note("headless-render",
      "headless with no canvasBridge/disableGpuFingerprint/fingerprintProfile - canvas and WebGL may " +
      "render on software here while the persona claims a hardware GPU (a render-vs-string mismatch on " +
      "canvas-scored sites). Use canvasBridge, disableGpuFingerprint, or a real-GPU host.");
  if (bridgeOn && !gpuR && !gpuV && !profile)
    note("bridge-no-gpu",
      "canvasBridge is set but gpuVendor/gpuRenderer aren't pinned - the WebGL renderer string may not " +
      "match the bridge node's pixels. Set them to the bridge node's GPU.");

  if (userArgs.some((a) => String(a).includes("--enable-automation") || String(a).startsWith("--remote-debugging-port")))
    warn("automation-arg",
      "your args re-introduce an automation flag (--enable-automation / --remote-debugging-port) the SDK " +
      "strips by default - a strong webdriver/CDP tell.");
  return out;
}

/** Print coherence warnings to stderr unless quiet or CLEARCOTE_NO_WARN. NOTE lines fire once/process. */
export function emitCoherenceWarnings(
  opts: Record<string, unknown>,
  quiet?: boolean,
  hostPlatform?: string,
  buildMajor?: string
): void {
  if (quiet || process.env.CLEARCOTE_NO_WARN) return;
  for (const w of coherenceWarnings(opts, hostPlatform, buildMajor)) {
    if (w.severity === "note") {
      if (seenNotes.has(w.code)) continue;
      seenNotes.add(w.code);
    }
    process.stderr.write(`clearcote: ${w.severity === "warn" ? "warning" : "note"}: ${w.message}\n`);
  }
}
