// Launch-time coherence warnings (mirror of the Python clearcote/_warnings.py).
//
// The SDK already defaults the safe things (strips --enable-automation, denies WebRTC leak,
// disables Privacy Sandbox, matches the persona to the build). What it CAN'T fix is an operator
// passing an incoherent or missing-recommended combination. coherenceWarnings() spots those at
// launch() and emitCoherenceWarnings() prints an actionable line to stderr. Never blocks the
// launch; suppressible with quiet:true or CLEARCOTE_NO_WARN=1. coherenceWarnings() is pure (no I/O).

import { isFingerprintPassthrough } from "./fingerprint.js";

const SOFTWARE_GPU = ["swiftshader", "llvmpipe", "microsoft basic render", "software adapter", "software"];
const seenNotes = new Set<string>(); // fire-once per process for NOTE codes
// WARN codes about the HOST rather than one launch's options: said once per process, like a note, so a
// program that launches in a loop is told once.
const ONCE_PER_PROCESS = new Set(["linux-persona-windows-host"]);

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
  const bmajor = buildMajor ?? "150";
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
  // No SOCKS + geoip warning: geoip resolves through socks5 (with credentials) since 0.29.0, and a
  // scheme it cannot use fails the launch with a GeoipError that names it.

  const fam = hostFamily(host);
  if (platform && fam && platform !== fam && !profile)
    warn("platform-host-fonts",
      `platform='${platform}' but this host is ${fam} and no fingerprintProfile supplies that OS's ` +
      `fonts/metrics - font, canvas and font-list hashes will be host-native and won't match a real ` +
      `${platform} Chrome. Use a fingerprintProfile captured on ${platform}, or set platform='${fam}'.`);
  // On a Windows host the GPU runs through Direct3D 11, which clamps the WebGL and WebGPU limits (vertex
  // uniform vectors, a 16384 maximum texture size, ...). A persona can lower a limit, never lift one past the
  // driver's, so a Linux claim here reports limits no real Linux machine has. Pass-through claims nothing.
  // Only a local launch gets here: a Docker launch runs on Linux in its container, a cloud one remotely.
  if (fam === "windows" && String(platform ?? "").trim().toLowerCase() === "linux" && !isFingerprintPassthrough(opts.fingerprint))
    warn("linux-persona-windows-host",
      "a Linux persona on a Windows host reports Windows GPU limits (Direct3D caps WebGL), which no real " +
      "Linux machine has. Use platform: \"windows\" on Windows, or run Linux personas on Linux or in Docker " +
      "(docker: true).");
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
  if (opts.devtools || userArgs.some((a) => String(a).startsWith("--auto-open-devtools-for-tabs")))
    warn("devtools-open",
      "DevTools is set to open (devtools:true / --auto-open-devtools-for-tabs). Pages can detect an open " +
      "DevTools (debugger and console timing probes; a docked panel also makes innerWidth/innerHeight " +
      "disagree with outerWidth/outerHeight). Leave it closed for real runs.");
  if (opts.userAgent || userArgs.some((a) => String(a).startsWith("--user-agent=")))
    warn("custom-user-agent",
      "a custom user agent (userAgent / --user-agent) replaces only the User-Agent string: " +
      "navigator.userAgentData, the Sec-CH-UA headers, navigator.platform and the rest of the persona " +
      "keep describing the persona, so a different OS or version in the string is a one-line mismatch. " +
      "Use platform, brand and brandVersion to change what the browser claims.");
  out.push(...cdpExposure(switchValue(userArgs, "--remote-debugging-address"), switchValue(userArgs, "--remote-allow-origins")));
  return out;
}

/** The value of the LAST `name=value` in `args` (Chromium keeps the last), else undefined. */
function switchValue(args: readonly unknown[], name: string): string | undefined {
  let value: string | undefined;
  for (const a of args) {
    const s = String(a);
    if (s.startsWith(`${name}=`)) value = s.slice(name.length + 1);
  }
  return value;
}

function isLoopback(host: string): boolean {
  const h = host.trim().replace(/^\[|\]$/g, "").toLowerCase();
  return h === "localhost" || h === "::1" || h.startsWith("127.");
}

/** Warnings for a DevTools endpoint reachable beyond this machine or from any web page. */
function cdpExposure(bindAddress: string | undefined, allowOrigins: string | undefined): CoherenceWarning[] {
  const out: CoherenceWarning[] = [];
  if (bindAddress !== undefined && !isLoopback(bindAddress))
    out.push({ severity: "warn", code: "cdp-public-bind", message:
      `the DevTools endpoint is bound to ${bindAddress}, not loopback: anyone who can reach that port can ` +
      "drive the browser, read its cookies and run code in its pages. Keep it on 127.0.0.1 and tunnel to it " +
      "if you need remote access." });
  if (allowOrigins !== undefined && allowOrigins.split(",").some((o) => o.trim() === "*"))
    out.push({ severity: "warn", code: "cdp-any-origin", message:
      "--remote-allow-origins=* lets any web page this browser (or any browser on this machine) opens " +
      "connect to the DevTools endpoint and take it over. List the origins you need instead." });
  return out;
}

/** The cdp-public-bind / cdp-any-origin warnings for `serve({ host, allowOrigins })`. */
export function serveExposureWarnings(host: string, allowOrigins: string): CoherenceWarning[] {
  return cdpExposure(host, allowOrigins);
}

/** Print a list of warnings to stderr unless quiet or CLEARCOTE_NO_WARN. */
export function emitWarnings(warnings: readonly CoherenceWarning[], quiet?: boolean): void {
  if (quiet || process.env.CLEARCOTE_NO_WARN) return;
  for (const w of warnings) process.stderr.write(`clearcote: ${w.severity === "warn" ? "warning" : "note"}: ${w.message}\n`);
}

/** Engine-behaviour advisories: true for every launch, so they live here rather than in
 *  coherenceWarnings() (whose contract is "a coherent default is silent"). Fire once per process. */
const ENGINE_NOTES: ReadonlyArray<readonly [string, string]> = [
  ["cdp-console-events",
    "the engine does not forward console or page-error events to automation clients: " +
    "page.on('console') and page.on('pageerror') receive nothing, by design, as part of the " +
    "protection against automation-presence probes. In-page window.onerror and " +
    "unhandledrejection handlers fire normally. To capture console output, collect it in-page " +
    "and read it back with page.evaluate(). An engine from r32 on forwards them with " +
    "stockRuntime: true (off by default: pages can observe part of what it restores)."],
];

/** Tests: forget which once-per-process lines were already said. */
export function resetSeenWarnings(): void {
  seenNotes.clear();
}

/** Print `clearcote: warning: <message>` to stderr at most once per process for `code`: for warnings about the
 *  engine or the launch mode rather than one launch's options, so a program that launches in a loop is told once.
 *  quiet and CLEARCOTE_NO_WARN silence it like every other warning, without using it up. */
export function warnOnce(code: string, message: string, quiet?: boolean): void {
  if (quiet || process.env.CLEARCOTE_NO_WARN || seenNotes.has(code)) return;
  seenNotes.add(code);
  process.stderr.write(`clearcote: warning: ${message}\n`);
}

/** Print coherence warnings to stderr unless quiet or CLEARCOTE_NO_WARN. NOTE lines (and the host warnings in
 *  ONCE_PER_PROCESS) fire once per process; the other WARN lines fire every launch. `opts._stockRuntime`: this
 *  launch's browser has --disable-runtime-suppression, so the note that it forwards no console events is left out. */
export function emitCoherenceWarnings(
  opts: Record<string, unknown>,
  quiet?: boolean,
  hostPlatform?: string,
  buildMajor?: string
): void {
  if (quiet || process.env.CLEARCOTE_NO_WARN) return;
  for (const w of coherenceWarnings(opts, hostPlatform, buildMajor)) {
    if (w.severity === "note" || ONCE_PER_PROCESS.has(w.code)) {
      if (seenNotes.has(w.code)) continue;
      seenNotes.add(w.code);
    }
    process.stderr.write(`clearcote: ${w.severity === "warn" ? "warning" : "note"}: ${w.message}\n`);
  }
  for (const [code, message] of ENGINE_NOTES) {
    if (seenNotes.has(code) || (code === "cdp-console-events" && opts._stockRuntime)) continue;
    seenNotes.add(code);
    process.stderr.write(`clearcote: note: ${message}\n`);
  }
}
