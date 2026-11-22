// realComputer: show parts of THIS computer instead of the persona's generated values, the way some single-identity
// browsers do on a real desktop. Three parts, each read from the operating system (never from a browser probe: a
// headless probe sees a virtual screen):
//   screen     the primary display in CSS px (size, work area, devicePixelRatio)
//   touch      navigator.maxTouchPoints (Windows: GetSystemMetrics(SM_MAXIMUMTOUCHES); macOS: 0, as Chrome reports)
//   languages  the system display language, expanded the way Chrome writes its list (en-GB -> en-GB,en-US,en)
//
// Measured 2026-11-21/22 on fingerprint.com behind residential proxies, paired against the default on the same seed
// and exit: this computer's touch value lowered the tampering score in 25 of 27 pairs (a value this computer does not
// have did not), and the real monitor in 16 of 16. Off by default: every profile on one computer then shares these
// values, which links profiles more easily, and a server or container has no real display. For one identity per
// real desktop.
//
// Rules: a value the caller set explicitly always wins. Languages follow a proxy's exit when one is set with geoip on
// (the computer's own language is only used without a proxy, or with geoip off). In pass-through (`fingerprint:
// "off"`) the engine already shows this computer, so nothing is added. What cannot be read is left to the persona,
// and the launch says so once.
import { spawn } from "node:child_process";
import { warnOnce } from "./warnings.js";

export interface HostDisplay { width: number; height: number; availWidth: number; availHeight: number; devicePixelRatio: number }
export interface HostFacts { display: HostDisplay | null; touch: number | null; language: string | null }
export type RealComputerParts = { screen?: boolean; touch?: boolean; languages?: boolean };
/** The `realComputer` option: true = all parts, or pick parts. */
export type RealComputerOption = boolean | RealComputerParts;

export const REAL_COMPUTER_WARN = "real-computer-partial";
const TIMEOUT_MS = 8000;

// One PowerShell call: DPI-aware primary screen bounds + work area (physical px), the system scaling (AppliedDPI),
// the touch points Chrome reads (GetSystemMetrics 95) and the display language Chrome follows (UI culture).
const WIN_PS = [
  "$ErrorActionPreference='Stop';",
  "Add-Type -AssemblyName System.Windows.Forms;",
  "Add-Type -Namespace CcHost -Name U -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware(); [DllImport(\"user32.dll\")] public static extern int GetSystemMetrics(int n);';",
  "[void][CcHost.U]::SetProcessDPIAware();",
  "$s=[System.Windows.Forms.Screen]::PrimaryScreen;",
  "$d=96; try { $v=(Get-ItemProperty 'HKCU:\\Control Panel\\Desktop\\WindowMetrics' -Name AppliedDPI).AppliedDPI; if ($v) { $d=$v } } catch {};",
  "\"$($s.Bounds.Width) $($s.Bounds.Height) $($s.WorkingArea.Width) $($s.WorkingArea.Height) $d $([CcHost.U]::GetSystemMetrics(95)) $((Get-UICulture).Name)\"",
].join(" ");

const MAC_JXA =
  "ObjC.import('AppKit'); var s = $.NSScreen.mainScreen; var f = s.frame, v = s.visibleFrame; " +
  "var l = $.NSLocale.preferredLanguages.count > 0 ? ObjC.unwrap($.NSLocale.preferredLanguages.objectAtIndex(0)) : ''; " +
  "[f.size.width, f.size.height, v.size.width, v.size.height, s.backingScaleFactor, l].join(' ')";

function run(cmd: string, args: string[]): Promise<string | null> {
  return new Promise((resolve) => {
    let out = "";
    let done = false;
    const finish = (v: string | null) => { if (!done) { done = true; resolve(v); } };
    try {
      const p = spawn(cmd, args, { stdio: ["ignore", "pipe", "ignore"], windowsHide: true });
      const t = setTimeout(() => { try { p.kill(); } catch { /* gone */ } finish(null); }, TIMEOUT_MS);
      t.unref?.();
      p.stdout.on("data", (b: Buffer) => { out += b.toString("utf8"); });
      p.on("error", () => { clearTimeout(t); finish(null); });
      p.on("close", (code) => { clearTimeout(t); finish(code === 0 ? out : null); });
    } catch {
      finish(null);
    }
  });
}

const css = (physical: number, ratio: number) => Math.round(physical / ratio);
const ratio2 = (r: number) => Math.round(r * 100) / 100;
const TAG = /^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$/;

/** Plausible display: at least 640x480 CSS px, a work area inside it (at least half of it), a ratio from 1 to 4. */
export function plausibleDisplay(d: HostDisplay | null): HostDisplay | null {
  if (!d) return null;
  const ok = [d.width, d.height, d.availWidth, d.availHeight, d.devicePixelRatio].every((n) => Number.isFinite(n) && n > 0) &&
    d.width >= 640 && d.height >= 480 && d.width <= 16384 && d.height <= 16384 &&
    d.availWidth <= d.width && d.availHeight <= d.height && d.availWidth >= d.width / 2 && d.availHeight >= d.height / 2 &&
    d.devicePixelRatio >= 1 && d.devicePixelRatio <= 4;
  return ok ? d : null;
}

const touchOf = (n: number): number | null => (Number.isInteger(n) && n >= 0 && n <= 256 ? n : null);
const langOf = (s: string | undefined): string | null => {
  const t = String(s ?? "").trim().replace(/_/g, "-").split(".")[0];
  return t && TAG.test(t) && t.toLowerCase() !== "c" ? t : null;
};

/** Windows: "boundsW boundsH workW workH appliedDpi touches uiCulture" (sizes in physical px). */
export function parseWindows(out: string): HostFacts {
  const f = out.trim().split(/\s+/);
  const n = f.slice(0, 6).map(Number);
  const r = Number.isFinite(n[4]) && n[4] > 0 ? ratio2(n[4] / 96) : NaN;
  const display = n.slice(0, 4).every(Number.isFinite) && Number.isFinite(r)
    ? plausibleDisplay({ width: css(n[0], r), height: css(n[1], r), availWidth: css(n[2], r), availHeight: css(n[3], r), devicePixelRatio: r })
    : null;
  return { display, touch: f.length > 5 ? touchOf(n[5]) : null, language: langOf(f[6]) };
}

/** macOS: "frameW frameH visibleW visibleH scale language" (points = CSS px). Chrome on macOS reports 0 touch points. */
export function parseMac(out: string): HostFacts {
  const f = out.trim().split(/\s+/);
  const n = f.slice(0, 5).map(Number);
  const display = n.every(Number.isFinite)
    ? plausibleDisplay({ width: Math.round(n[0]), height: Math.round(n[1]), availWidth: Math.round(n[2]), availHeight: Math.round(n[3]), devicePixelRatio: ratio2(n[4]) })
    : null;
  return { display, touch: 0, language: langOf(f[5]) };
}

/** Linux: the primary output of `xrandr --current`, else the first connected one with a mode; work area = screen. */
export function parseXrandr(out: string): HostDisplay | null {
  const lines = out.split("\n").filter((l) => / connected /.test(l));
  const line = lines.find((l) => / connected primary /.test(l)) ?? lines.find((l) => /\d+x\d+\+\d+\+\d+/.test(l));
  const m = line?.match(/(\d+)x(\d+)\+\d+\+\d+/);
  if (!m) return null;
  const w = Number(m[1]), h = Number(m[2]);
  return plausibleDisplay({ width: w, height: h, availWidth: w, availHeight: h, devicePixelRatio: 1 });
}

let cached: Promise<HostFacts> | undefined;

/** This computer's display, touch points and display language, read once per process. Parts that cannot be read are
 *  null (no display server, a timeout, an implausible answer). Linux reports no touch value (left to the persona). */
export function readHostFacts(platform: NodeJS.Platform = process.platform, env: NodeJS.ProcessEnv = process.env): Promise<HostFacts> {
  if (cached) return cached;
  cached = (async (): Promise<HostFacts> => {
    if (platform === "win32") {
      const out = await run("powershell.exe", ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", WIN_PS]);
      return out ? parseWindows(out) : { display: null, touch: null, language: null };
    }
    if (platform === "darwin") {
      const out = await run("osascript", ["-l", "JavaScript", "-e", MAC_JXA]);
      return out ? parseMac(out) : { display: null, touch: 0, language: null };
    }
    const language = langOf((env.LANGUAGE || "").split(":")[0] || env.LC_ALL || env.LC_MESSAGES || env.LANG);
    if (!env.DISPLAY && !env.WAYLAND_DISPLAY) return { display: null, touch: null, language };
    const out = await run("xrandr", ["--current"]);
    return { display: out ? parseXrandr(out) : null, touch: null, language };
  })();
  return cached;
}

/** Tests: forget the cached facts. */
export function resetHostFacts(): void { cached = undefined; }

/** Which parts an option value turns on. */
export function realComputerParts(v: unknown): Required<RealComputerParts> {
  if (v === true) return { screen: true, touch: true, languages: true };
  if (v && typeof v === "object") {
    const p = v as RealComputerParts;
    return { screen: p.screen === true, touch: p.touch === true, languages: p.languages === true };
  }
  return { screen: false, touch: false, languages: false };
}

/**
 * Apply `realComputer` to one launch's fingerprint options. Never mutates the input; returns it unchanged when the
 * option is off or in pass-through. `proxied`: a proxy is set and geoip is on, so the exit decides the languages.
 */
export async function withRealComputer<T extends Record<string, unknown>>(
  o: T, ctx: { proxied?: boolean; quiet?: boolean; read?: () => Promise<HostFacts> } = {},
): Promise<T> {
  const parts = realComputerParts(o.realComputer);
  if (!parts.screen && !parts.touch && !parts.languages) return o;
  if (String(o.fingerprint ?? "").trim().toLowerCase() === "off") return o;
  const want = {
    screen: parts.screen && o.screenWidth == null && o.screenHeight == null,
    touch: parts.touch && o.maxTouchPoints == null,
    languages: parts.languages && (o.acceptLanguage == null || o.acceptLanguage === "") && !ctx.proxied,
  };
  if (!want.screen && !want.touch && !want.languages) return o;
  const facts = await (ctx.read ?? readHostFacts)();
  const out: Record<string, unknown> = { ...o };
  const missing: string[] = [];
  if (want.screen) {
    const d = facts.display;
    if (d) Object.assign(out, { screenWidth: d.width, screenHeight: d.height, availWidth: d.availWidth, availHeight: d.availHeight, devicePixelRatio: d.devicePixelRatio });
    else missing.push("screen");
  }
  if (want.touch) {
    if (facts.touch != null) out.maxTouchPoints = facts.touch;
    else missing.push("touch");
  }
  if (want.languages) {
    if (facts.language) out.acceptLanguage = facts.language;
    else missing.push("languages");
  }
  if (missing.length) {
    warnOnce(REAL_COMPUTER_WARN,
      `realComputer: this computer's ${missing.join(" and ")} could not be read (no display server, or it did not answer), ` +
        "so the profile keeps its generated value there.", ctx.quiet);
  }
  return out as T;
}
