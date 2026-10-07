/**
 * Headless window geometry. Port of the Python SDK's `_geometry.py`; the profile table, the seed
 * hashing and the skip rules are identical, so a seed selects the same persona in every SDK.
 *
 * Headed launches take their geometry from the real display and the SDK keeps the page on it
 * (`viewport: null`), so screen/avail/inner/outer agree by construction. Headless has no display,
 * and what it reports depends on whether the engine's persona machinery is running. Regime 1 was
 * measured on 149.0.7827.114/linux-x64, regime 2 on win-x64 149 and 153 (below).
 *
 * REGIME 1 — a persona is active (`--fingerprint=<seed>` on the command line). The engine spoofs
 * screen AND avail from the seed, including a taskbar (seed A -> 1920x1080 / avail 1920x1040, seed B
 * -> 2560x1440 / 1400, seed C -> 1600x900 / 860), and its values BEAT a CDP screen override — so the
 * SDK must not try to set screen here, it would silently lose. Leaving Playwright's emulated viewport
 * on is just as wrong: inner 1280x720 inside an outer of 1920x1040 leaves 640px of window
 * unaccounted for by any frame a real browser has. So `viewport: null` plus one window resize into
 * the persona's own work area, which lands a maximized window:
 *
 *     screen 1920x1080   avail 1920x1040   inner 1920x952   outer 1920x1040   frame (0, 88)
 *
 * REGIME 2 — no persona (the default seedless launch, and `lightStealth`, which drops
 * `--fingerprint` deliberately). Nothing spoofs screen, so a page sees the 800x600 headless surface,
 * and Playwright's emulated viewport then synthesizes a window on top of it:
 *
 *     screen 1280x720   avail 1280x720   inner 1280x720   outer 1288x851   <- outer > screen
 *
 * A window larger than its own screen is an impossible state, readable in two property lookups, and
 * it was present on every headless shape reachable through the SDK. The fix is `--screen-info`, which
 * makes the headless DISPLAY a real-machine size (with a taskbar) before the first document exists,
 * and then the same `viewport: null` + work-area fit as regime 1. Measured on
 * 153.0.8010.36/win-x64, persistent and non-persistent contexts:
 *
 *     screen 1920x1080  avail 1920x1040  inner 1904x911  outer 1920x1040   (popups clamp inside it)
 *
 * This used to be a CDP screen override (Playwright's `screen` option) with the viewport sized as
 * screen minus a hardcoded engine frame. That had two faults: the override forces avail == screen
 * (no taskbar, a minority shape), and the frame it synthesizes is per-platform — 8x131 on linux-x64
 * but 16x134 on win-x64 (measured on both 149.0.7827.114 and 153.0.8010.36), so on Windows the window
 * landed 8x3 px past the screen edge. With a real display nothing is sized against the frame: the
 * engine draws whatever frame it draws inside a window the SDK fits to the work area. (The engine's
 * `--fingerprint-screen-*` switches are no substitute: device-width media queries keep answering
 * 800x600.)
 *
 * PARITY: the seed still selects the same screen row as the Python and .NET SDKs
 * ({@link headlessGeometry}); only how this SDK applies it changed.
 *
 * PROVENANCE of the regime-2 table: the `audit_profiles` corpus (real captures from the public
 * fingerprint audit), desktop rows whose geometry is self-consistent and which are not themselves
 * emulated-viewport captures, counted by distinct /24 so one busy machine cannot skew it. macOS rows
 * are dropped (color_depth 30, which this engine cannot spoof), non-1.0 DPR rows are dropped
 * (scaling changes what the rasterizer produces), and ultrawide 3440x1440 is capped to weight 2
 * (12 distinct /24s in the corpus — developers over-represent ultrawides).
 */
import { createHash } from "node:crypto";
import { gunzipSync } from "node:zlib";
import type { BrowserContext, Page } from "playwright-core";
import { connectCdp, type CdpConnection } from "./cdpws.js";
import { lightStealthScreen } from "./fingerprint.js";

/**
 * The linux-x64 engine's window frame around an EMULATED viewport: outer = inner + (WIDTH, HEIGHT).
 *
 * Measured with no persona on 149.0.7827.114/linux-x64 (same 8/131 on 150), constant across every
 * viewport probed. win-x64 draws 16/134 instead, which is why launch() no longer sizes anything
 * against these (see the module comment). They remain the shared cross-SDK constants behind
 * {@link headlessGeometry}'s viewport (the Python and .NET SDKs still size against them) and the
 * floor a usable imported-profile screen must clear.
 */
export const ENGINE_FRAME_WIDTH = 8;
export const ENGINE_FRAME_HEIGHT = 131;

/** `[screenWidth, screenHeight, weight, osHint]` — weight is distinct /24s in the corpus. */
export const HEADLESS_SCREEN_PROFILES: ReadonlyArray<readonly [number, number, number, string]> = [
  [1920, 1080, 24, "windows"],
  [2560, 1440, 13, "windows"],
  [1920, 1200, 6, "linux"],
  [1366, 768, 3, "windows"],
  [1600, 900, 3, "linux"],
  [3440, 1440, 2, "windows"],   // capped from 12 (see the module comment)
  [3840, 2160, 2, "windows"],
  [1680, 1050, 2, "windows"],
];

/** Window flags that mean the caller sized the window themselves. */
const CALLER_WINDOW_FLAGS = ["--window-size", "--window-position", "--start-maximized"];

export interface Size { width: number; height: number }
export interface HeadlessGeometry { screen: Size; viewport: Size }

/**
 * Whether `--fingerprint=<seed>` is on the command line, i.e. the engine spoofs screen/avail itself
 * (regime 1). `lightStealth` drops that switch on purpose, so this is false for it even though a
 * seed was passed to the SDK.
 */
export function personaActive(args?: readonly string[] | null): boolean {
  return (args ?? []).some((a) => String(a).startsWith("--fingerprint="));
}

/** True when the caller passed their own window geometry flag. */
export function callerSizedTheWindow(args?: readonly string[] | null): boolean {
  return (args ?? []).some((a) => CALLER_WINDOW_FLAGS.includes(String(a).split("=")[0]));
}

/**
 * Weighted, deterministic choice from {@link HEADLESS_SCREEN_PROFILES}. Same construction as
 * `lightStealthValues`: the full sha256 digest as a big integer, so Python, Node and .NET select the
 * identical row for a seed. An unset seed maps to a fixed key rather than randomness, so a seedless
 * launch stays reproducible.
 */
function pick(seed?: string | number | null): readonly [number, number, number, string] {
  const key = seed === undefined || seed === null || seed === "" ? "clearcote-headless-geometry" : String(seed);
  const digest = createHash("sha256").update(key, "utf8").digest("hex");
  const total = HEADLESS_SCREEN_PROFILES.reduce((n, row) => n + row[2], 0);
  let point = Number(BigInt("0x" + digest) % BigInt(total));
  for (const row of HEADLESS_SCREEN_PROFILES) {
    point -= row[2];
    if (point < 0) return row;
  }
  return HEADLESS_SCREEN_PROFILES[HEADLESS_SCREEN_PROFILES.length - 1];
}

/** Context geometry for a given screen: viewport = screen minus the engine's frame. */
function geometryFor(width: number, height: number): HeadlessGeometry {
  return {
    screen: { width, height },
    viewport: { width: width - ENGINE_FRAME_WIDTH, height: height - ENGINE_FRAME_HEIGHT },
  };
}

const PROFILE_FLAG = "--fingerprint-profile=";

/**
 * The imported profile's own screen, or null.
 *
 * Measured: `--fingerprint-profile` supplies screen/avail only when a persona (`--fingerprint=`) is
 * ALSO running. Without a seed the profile's display is inert, so the SDK's display is all the page
 * sees — and taking it from the profile keeps the imported identity instead of giving every seedless
 * profile launch the same corpus screen.
 *
 * Reads the value off the switch (gzip+base64 of the capture JSON), which is the one form every SDK
 * has in hand here whatever the caller passed. Best-effort: a profile the engine can still use must
 * never fail a launch because this could not read a screen out of it, so failures return null. A
 * screen too small to hold the engine's frame is rejected for the same reason the corpus table has no
 * tiny rows — the leftover viewport would not lay out a desktop site. (That guard is what keeps an
 * `profile="auto"` capture made on a headless host, whose screen can be the 800x600 surface, from
 * becoming the persona's display.)
 */
export function profileScreenFromArgs(args?: readonly string[] | null): [number, number] | null {
  for (const raw of args ?? []) {
    const arg = String(raw);
    if (!arg.startsWith(PROFILE_FLAG)) continue;
    try {
      const json = gunzipSync(Buffer.from(arg.slice(PROFILE_FLAG.length), "base64")).toString("utf8");
      const screen = (JSON.parse(json) || {}).screen || {};
      const width = Number(screen.width);
      const height = Number(screen.height);
      if (!Number.isFinite(width) || !Number.isFinite(height)) return null;
      if (width - ENGINE_FRAME_WIDTH < 1024 || height - ENGINE_FRAME_HEIGHT < 600) return null;
      return [width, height];
    } catch {
      return null;
    }
  }
  return null;
}

/**
 * A seed's regime-2 screen row, with the viewport the Python and .NET SDKs size against it (screen
 * minus the linux engine frame). This is the cross-SDK parity contract; this SDK's launch() takes
 * only the screen from it (see {@link applyHeadlessGeometry}).
 */
export function headlessGeometry(seed?: string | number | null): HeadlessGeometry {
  const [sw, sh] = pick(seed);
  return geometryFor(sw, sh);
}

/** The fingerprint options that decide a regime-2 display. */
export interface DisplayFingerprint {
  platform?: string;
  screenWidth?: number;
  screenHeight?: number;
  availWidth?: number;
  availHeight?: number;
}

export type AppliedGeometry =
  /** On Linux, args carries the persona's real headless display (`--screen-info`). */
  | { mode: "persona"; args: string[] }
  /** display: what `--screen-info` sets, or null when the caller passed their own display switch. */
  | { mode: "display"; display: Display | null; args: string[] };

/**
 * Default a headless launch's geometry; returns what was applied, or null.
 *
 * Both regimes set `viewport: null` in `opts` (so innerWidth tracks the real window) and leave the
 * window to be fitted to the work area on the first page (see {@link installWindowFixup}). Regime 2
 * also returns the switches that give the browser its headless display — `args`, to append to the
 * command line; the fit and skip checks keep reading the caller's own `args`, not these.
 *
 * Skipped when headed (the real window is already coherent) and when the caller expressed ANY
 * geometry intent — `viewport` (including an explicit null) or `screen`. `headless` unset means
 * headless, matching Playwright. A caller's own `--screen-info` keeps their display; a caller's own
 * window switch keeps their window (the display is still set, so that window has a real screen).
 */
export function applyHeadlessGeometry(
  opts: Record<string, unknown>,
  seed?: string | number | null,
  args?: readonly string[] | null,
  fp: DisplayFingerprint = {},
): AppliedGeometry | null {
  if (opts.headless === false) return null;
  if ("viewport" in opts || "screen" in opts) return null;
  opts.viewport = null;
  if (personaActive(args)) {
    const display = process.platform === "linux" && !callerSetTheDisplay(args)
      ? servedDisplay({ ...fp, seed, args, lightStealth: false })
      : null;
    return { mode: "persona", args: display ? [screenInfoSwitch(display)] : [] };
  }
  // Not lightStealth's own row, unlike serve(): launch() keeps the seed -> screen row the Python and
  // .NET launch() pick (headlessGeometry). An imported profile's screen still wins over it.
  const display = callerSetTheDisplay(args) ? null : servedDisplay({ ...fp, seed, args, lightStealth: false });
  return {
    mode: "display",
    display,
    args: [
      ...(display ? [screenInfoSwitch(display)] : []),
      ...(callerSizedTheWindow(args) ? [] : ["--window-position=0,0"]),
    ],
  };
}

// A plain expression, NOT "() => [...]": Playwright evaluates an arrow-function string to a
// function object rather than calling it (which silently broke this fit once).
const WORKAREA_JS = "[screen.availWidth, screen.availHeight]";
const OUTER_JS = "[outerWidth, outerHeight]";

/** A work area only a non-engaged persona would report (the headless default) is not worth fitting to. */
function plausible(area: number[] | null | undefined): boolean {
  return !!area && area.length === 2 && area[0] >= 1024 && area[1] >= 600;
}

function bounds(width: number, height: number) {
  return { left: 0, top: 0, width: Math.round(width), height: Math.round(height) };
}

/**
 * The bounds correction, given what the window reported after the first attempt.
 *
 * Requested bounds and reported outerHeight are not always the same quantity: on 149 the window
 * reported 33px less than the bounds height it was given, and on 150 a persona's window does (33px in
 * a launch, 89 in serve(); without a persona it reports the bounds exactly). Fitting bounds to the
 * work area then lands short of maximized (real maximized captures have outer == avail). Rather than
 * hardcode it, measure the shortfall and add it back — that self-tunes if the engine changes. Never
 * asks for more than the shortfall.
 *
 * `room` is the space from the window's position to the work area's far edges. On an axis where the
 * window is smaller than that, the correction never takes the bounds past the edge: only a window that
 * fills the work area needs bounds beyond it (the persona engine then clamps the window to the work
 * area), and an engine without a persona does not clamp at all.
 */
export function fitPlan(avail: number[], outer: number[], room?: readonly number[] | null): [number, number] | null {
  const dw = avail[0] - outer[0];
  const dh = avail[1] - outer[1];
  if (dw <= 0 && dh <= 0) return null;
  const capped = (want: number, i: number) => (room && avail[i] < room[i] ? Math.min(want, room[i]) : want);
  return [capped(avail[0] + Math.max(dw, 0), 0), capped(avail[1] + Math.max(dh, 0), 1)];
}

/** How long a window resize may take to reach the page before a read is taken as it stands. */
const SETTLE_MS = 1000;
const SETTLE_POLL_MS = 20;

const sameSize = (a: readonly number[], b: readonly number[]) => a[0] === b[0] && a[1] === b[1];

/**
 * Read `[outerWidth, outerHeight]` until it reflects the last `Browser.setWindowBounds`. The call
 * returns before the page has the new window size, so a read straight after it can still report the
 * previous one (measured on 150/linux-x64: for tens of milliseconds). Correcting from such a
 * read once asked for 1440+495 px on a 1920 px screen. Settled means the asked-for size, or a size
 * other than `before` that is still there one poll later; `settled` is false when neither happened
 * within `timeoutMs`.
 */
async function settledOuter(
  read: () => Promise<number[]>,
  before: readonly number[],
  want: readonly number[],
  timeoutMs = SETTLE_MS,
): Promise<{ outer: number[]; settled: boolean }> {
  const deadline = Date.now() + timeoutMs;
  let outer = await read();
  for (;;) {
    if (sameSize(outer, want)) return { outer, settled: true };
    if (Date.now() >= deadline) return { outer, settled: false };
    await new Promise((r) => setTimeout(r, SETTLE_POLL_MS));
    const next = await read();
    if (!sameSize(outer, before) && sameSize(next, outer)) return { outer, settled: true };
    outer = next;
  }
}

/**
 * Give the window `target`, then correct what the engine reports (see {@link fitPlan}), on settled
 * reads only: a correction is planned from a size the page has actually seen, and kept only when the
 * page then sees a window no larger than `target`; otherwise the bounds go back to `target`. With no
 * settled read there is nothing to correct from, and `target` lies inside the work area. Returns the
 * last outer size read.
 */
async function fitWindow(
  setBounds: (width: number, height: number) => Promise<unknown>,
  read: () => Promise<number[]>,
  target: [number, number],
  room: [number, number],
  settleMs: number,
): Promise<number[]> {
  const [w, h] = target;
  const before = await read();
  await setBounds(w, h);
  const first = await settledOuter(read, before, target, settleMs);
  const plan = first.settled ? fitPlan(target, first.outer, room) : null;
  if (!plan) return first.outer;
  await setBounds(plan[0], plan[1]);
  const fixed = await settledOuter(read, first.outer, plan, settleMs);
  // Overshooting would trade one impossible geometry for another (outer > target).
  if (fixed.settled && fixed.outer[0] <= w && fixed.outer[1] <= h) return fixed.outer;
  await setBounds(w, h);
  return (await settledOuter(read, fixed.outer, target, settleMs)).outer;
}

/**
 * Size the headless window to the display's work area — the persona's (regime 1) or the one
 * `--screen-info` set (regime 2) — so the page reports a maximized window (outer == avail) instead
 * of the headless default window sitting inside a much larger screen.
 *
 * `--start-maximized` and CDP `windowState: "maximized"` are both no-ops in headless (measured — the
 * window stays at its default size), which is why this sets explicit bounds.
 *
 * Never throws: a geometry improvement must not be able to fail a launch.
 */
export async function fitWindowToWorkArea(
  page: Page,
  args?: readonly string[] | null,
): Promise<[number, number] | null> {
  if (callerSizedTheWindow(args)) return null;
  try {
    const avail = (await page.evaluate(WORKAREA_JS)) as number[];
    if (!plausible(avail)) return null;
    const cdp = await page.context().newCDPSession(page);
    const { windowId } = (await cdp.send("Browser.getWindowForTarget")) as { windowId: number };
    const outer = await fitWindow(
      (w, h) => cdp.send("Browser.setWindowBounds", { windowId, bounds: bounds(w, h) }),
      async () => (await page.evaluate(OUTER_JS)) as number[],
      [avail[0], avail[1]], [avail[0], avail[1]], SETTLE_MS);
    return [outer[0], outer[1]];
  } catch {
    return null;   // deliberately silent: never fail a launch over geometry
  }
}

/**
 * Apply the headless window fit once, on the first page (each context is its own window). A
 * persistent context already owns a page, so act immediately; a browser-level context does not, so
 * defer to its first `newPage`. `args` are the caller's, so a window switch of theirs is respected.
 */
export async function installWindowFixup(
  context: BrowserContext,
  args?: readonly string[] | null,
): Promise<void> {
  let done = false;
  const fix = async (page: Page) => {
    if (done) return page;
    done = true;
    await fitWindowToWorkArea(page, args);
    return page;
  };
  const existing = context.pages();
  if (existing.length) {
    await fix(existing[0]);
    return;
  }
  const origNewPage = context.newPage.bind(context);
  (context as unknown as { newPage: () => Promise<Page> }).newPage = async () => fix(await origNewPage());
}

/** `inner <= outer <= avail <= screen` on both axes — the chain a real window satisfies. */
export function geometryIsCoherent(
  screen: number[], avail: number[], inner: number[], outer: number[],
): boolean {
  return inner[0] <= outer[0] && inner[1] <= outer[1]
    && outer[0] <= avail[0] && outer[1] <= avail[1]
    && avail[0] <= screen[0] && avail[1] <= screen[1];
}

// ─────────────────────────────────────────────────────────────────────────────────────────────────
// serve(): a raw CDP endpoint
//
// launch() finishes its geometry through Playwright: `viewport: null` is a context option and the
// fit runs on each context's first page. A raw endpoint has neither, and a CDP emulation override
// would not survive either, being scoped to the session that set it. What every
// target and every client of a served browser inherits is the headless DISPLAY and the real WINDOW,
// both browser-level. Untouched, measured on 153.0.8010.36/win-x64:
//
//     no persona:           screen 800x600    avail 800x600    outer 780x580   <- the headless surface
//     no persona + --window-size=1440,900:    outer 1440x900 on that 800x600 screen
//     persona:              screen 1920x1080  avail 1920x1040  outer 780x580, and --window-size is
//                           ignored: the real window is clamped to the real 800x600 display
//
// REGIME 2 (no persona): `--screen-info`, as in launch(), makes the headless display a real-machine
// size before the first document exists, so screen.*, device-width media queries, window clamping
// and popup placement all agree, with a taskbar. The one difference from launch() is which row:
// serve() takes a lightStealth seed's own row (its DPR's pair), launch() the cross-SDK one.
//
// REGIME 1 (persona): the persona picks its display inside the engine, so it is only known once a
// page can be asked. `Emulation.updateScreen` (headless-only, browser-level, outlives the session
// that sent it) then resizes the headless display to match. A fixed larger stand-in display would
// let the window grow, but lets a big popup overhang the persona's screen.
//
// BOTH: one `Browser.setWindowBounds` puts the first window on the work area, and
// `--window-position` puts later windows at the work-area origin. Deliberately no `--window-size`:
// it forces every popup to that size, ignoring the window.open() features real Chrome honours.
// Measured after the fitting connection closed, both regimes: first tab, second tab, a 500x400
// popup, a 4000x3000 popup (clamped to the work area) and a new context's window all coherent.

/** Switches that mean the caller set the headless display themselves. */
const CALLER_DISPLAY_FLAGS = ["--screen-info"];

/** Windows' taskbar at 100% scaling; the engine's persona table uses the same 40px. */
export const WINDOWS_TASKBAR_HEIGHT = 40;

/** A display in CSS px, with its work area anchored at the top-left (taskbar at the bottom). */
export interface Display { width: number; height: number; availWidth: number; availHeight: number }

export interface ServedGeometry {
  /** true: the persona owns the display, which is matched after launch (see fitServedWindow). */
  persona: boolean;
  /** Regime 2: the display serve() launches with. null under a persona. */
  display: Display | null;
  /** Switches for the command line. */
  args: string[];
}

export function callerSetTheDisplay(args?: readonly string[] | null): boolean {
  return (args ?? []).some((a) => CALLER_DISPLAY_FLAGS.includes(String(a).split("=")[0]));
}

const HOST_PLATFORM =
  ({ win32: "windows", linux: "linux", darwin: "macos" } as Record<string, string>)[process.platform] ?? "windows";

/**
 * The regime-2 headless display (serve()'s, and launch()'s without `lightStealth`). In order: an
 * explicit `screenWidth`/`screenHeight` (already spoofed into screen.* by the engine, so the display
 * must agree), an imported profile's screen, the `lightStealth` row that also supplies the seed's
 * DPR, and otherwise the cross-SDK corpus pick ({@link headlessGeometry}). A Windows persona gets a taskbar; other platforms report avail == screen, which
 * real captures do too (see the module comment).
 */
export function servedDisplay(o: {
  seed?: string | number | null;
  lightStealth?: boolean;
  platform?: string;
  screenWidth?: number;
  screenHeight?: number;
  availWidth?: number;
  availHeight?: number;
  args?: readonly string[] | null;
}): Display {
  const windows = (o.platform ?? HOST_PLATFORM) === "windows";
  const withTaskbar = (width: number, height: number): Display =>
    ({ width, height, availWidth: width, availHeight: windows ? height - WINDOWS_TASKBAR_HEIGHT : height });
  if (o.screenWidth && o.screenHeight) {
    const d = withTaskbar(o.screenWidth, o.screenHeight);
    return {
      ...d,
      availWidth: Math.min(o.availWidth || d.availWidth, d.width),
      availHeight: Math.min(o.availHeight || d.availHeight, d.height),
    };
  }
  const fromProfile = profileScreenFromArgs(o.args);
  if (fromProfile) return withTaskbar(fromProfile[0], fromProfile[1]);
  if (o.lightStealth) {
    const s = lightStealthScreen(o.seed ?? undefined);
    return windows ? s : withTaskbar(s.width, s.height);
  }
  const [w, h] = pick(o.seed);
  return withTaskbar(w, h);
}

/** `--screen-info` for a display: its size plus the work-area insets the taskbar takes. */
export function screenInfoSwitch(d: Display): string {
  const insets = ([["workAreaRight", d.width - d.availWidth], ["workAreaBottom", d.height - d.availHeight]] as const)
    .filter(([, v]) => v > 0)
    .map(([k, v]) => ` ${k}=${v}`)
    .join("");
  return `--screen-info={${d.width}x${d.height}${insets}}`;
}

/**
 * What serve() adds for a headless launch, or null to leave geometry alone: headed (a real display),
 * or the caller passed a window or display switch of their own. The SDK's own android
 * `--window-size` counts as one: a phone persona sizes itself.
 */
export function servedGeometry(
  engineArgs: readonly string[],
  fp: {
    fingerprint?: string | number | null; lightStealth?: boolean; platform?: string;
    screenWidth?: number; screenHeight?: number; availWidth?: number; availHeight?: number;
  },
  headless = true,
): ServedGeometry | null {
  if (!headless || callerSizedTheWindow(engineArgs) || callerSetTheDisplay(engineArgs)) return null;
  const origin = "--window-position=0,0";
  if (personaActive(engineArgs)) return { persona: true, display: null, args: [origin] };
  const display = servedDisplay({ ...fp, seed: fp.fingerprint, args: engineArgs });
  return { persona: false, display, args: [screenInfoSwitch(display), origin] };
}

/** The slice of a CDP connection the window fit needs (sessionId routes to a page). */
export interface CdpSend {
  send(method: string, params?: Record<string, unknown>, sessionId?: string): Promise<Record<string, unknown>>;
}

const DISPLAY_JS = "[screen.width, screen.height, screen.availLeft, screen.availTop, screen.availWidth, screen.availHeight]";

export interface ServedFit { display: Display; outer: [number, number] }

/**
 * Put the served browser's first window on its display's work area (or `windowSize`, clamped into
 * it); under a persona, first make the headless display the persona's own. Over the caller's CDP
 * connection, on the first page target. Only Target/Emulation/Browser commands plus
 * Runtime.evaluate reads (never Runtime.enable) on that page, and every change is browser-level, so
 * nothing lingers on the page when the connection closes.
 *
 * Returns null when it could not act (no page, an implausible persona display, or an engine without
 * `Emulation.updateScreen` under a persona, whose window cannot outgrow 800x600 without it). Never
 * throws: geometry must not be able to fail a launch. `settleMs` bounds the wait for each resize to
 * reach the page ({@link settledOuter}).
 */
export async function fitWindowOverCdp(
  cdp: CdpSend,
  opts: { persona: boolean; windowSize?: Size | null; settleMs?: number },
): Promise<ServedFit | null> {
  let sessionId: string | undefined;
  try {
    const { targetInfos = [] } = (await cdp.send("Target.getTargets")) as { targetInfos?: { targetId: string; type: string }[] };
    const page = targetInfos.find((t) => t.type === "page");
    if (!page) return null;
    sessionId = (await cdp.send("Target.attachToTarget", { targetId: page.targetId, flatten: true })).sessionId as string;
    const read = async (expression: string) =>
      ((await cdp.send("Runtime.evaluate", { expression, returnByValue: true }, sessionId)).result as { value: number[] }).value;
    const [sw, sh, al, at, aw, ah] = await read(DISPLAY_JS);
    if (!plausible([aw, ah]) || aw > sw || ah > sh) return null;
    if (opts.persona) {
      const { screenInfos = [] } = (await cdp.send("Emulation.getScreenInfos")) as { screenInfos?: { id: string; isPrimary?: boolean }[] };
      const primary = screenInfos.find((s) => s.isPrimary) ?? screenInfos[0];
      if (!primary) return null;
      await cdp.send("Emulation.updateScreen", {
        screenId: primary.id, left: 0, top: 0, width: sw, height: sh,
        workAreaInsets: { left: al, top: at, right: sw - al - aw, bottom: sh - at - ah },
      });
    }
    // The asked-for size, never past the work area; a window smaller than it sits 10px in, like
    // Chrome's own first placement.
    const w = Math.min(Math.round(opts.windowSize?.width ?? aw), aw);
    const h = Math.min(Math.round(opts.windowSize?.height ?? ah), ah);
    const left = al + Math.min(10, aw - w);
    const top = at + Math.min(10, ah - h);
    const { windowId } = (await cdp.send("Browser.getWindowForTarget", { targetId: page.targetId })) as { windowId: number };
    const setBounds = (bw: number, bh: number) =>
      cdp.send("Browser.setWindowBounds", { windowId, bounds: { left, top, width: Math.round(bw), height: Math.round(bh) } });
    // Same as fitWindowToWorkArea: an engine that reports less than the bounds it was given gets the
    // shortfall added back, an overshoot is reverted, and only settled reads count.
    const outer = await fitWindow(setBounds, () => read(OUTER_JS), [w, h],
      [al + aw - left, at + ah - top], opts.settleMs ?? SETTLE_MS);
    return { display: { width: sw, height: sh, availWidth: aw, availHeight: ah }, outer: [outer[0], outer[1]] };
  } catch {
    return null;
  } finally {
    if (sessionId) await cdp.send("Target.detachFromTarget", { sessionId }).catch(() => {});
  }
}

/** {@link fitWindowOverCdp} on the SDK's own short connection to a served browser. Never throws. */
export async function fitServedWindow(
  wsUrl: string | undefined,
  opts: { persona: boolean; windowSize?: Size | null; timeoutMs?: number },
): Promise<ServedFit | null> {
  if (!wsUrl) return null;
  let cdp: CdpConnection | null = null;
  try {
    // The SDK's own WebSocket client, not Node's global one: that only exists from Node 22 on.
    cdp = await connectCdp(wsUrl, opts.timeoutMs ?? 5000);
    return await fitWindowOverCdp(cdp, opts);
  } catch {
    return null;
  } finally {
    cdp?.close();
  }
}
