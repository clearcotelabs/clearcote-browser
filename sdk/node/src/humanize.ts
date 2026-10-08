// Humanized input + cursor visualization.
//
// `humanize: true` installs ONE consistent human-input standard covering moving, clicking,
//   dragging, scrolling and typing — dispatched as native trusted input (isTrusted===true,
//   navigator.webdriver stays false). It works whether you drive the page directly or via
//   locators:
//     page level    — page.click / hover / dblclick / type / fill / press / selectOption,
//                      page.mouse.move / click / wheel and held-button drags (down -> move -> up),
//                      page.keyboard.type / press
//     locator level  — locator.click / type / fill / hover / dblclick / press /
//                      pressSequentially / clear / check / uncheck / tap / selectOption / dragTo
//                      (routed through the humanized page methods; main-frame locators only,
//                      native fallback)
// `showCursor: true` — injects a red cursor dot that follows the real mousemove events.
//
// Mouse moves build an eased, slightly bowed cubic-bezier path SDK-side from the tracked cursor
// position (continuous — no snap back to 0,0) using Playwright's NATIVE mouse.move. Because it is
// native input, the button state is carried: down() -> move() -> up() is a real held-button drag
// (slider captchas work) with no separate CDP channel to desync. Typing goes key-by-key through
// the native keyboard (trusted; shift handled by the engine) with human inter-key timing. No
// special binary command required.

import type { Browser, BrowserContext, Locator, Page } from "playwright-core";
import {
  makePersona, planMove, dragDwell, clickHold, keyDwell, clickPoint, planAmbient, gaussFrom, rolloverRate,
  wheelNotches, platformFromNavigator, WHEEL_NOTCH_PX, type Persona, type Step, type WheelPlatform,
} from "./motion.js";
import { COVER_CHECK_MS, ISO_IS_FOCUSED, ISO_PLATFORM, ISO_SELECT_PLAN, ISO_SELECTED_INDEX, ISO_VIEWPORT, worldFor } from "./isolated.js";

export interface HumanizeOptions {
  /** Humanize all input (move/click/drag/scroll/type) as native trusted events. */
  humanize?: boolean;
  /** Show a red cursor dot that tracks the motion (visualization). */
  showCursor?: boolean;
  /** @internal Fingerprint seed → a stable per-identity motor persona (cadence, tremor, overshoot,
   * handedness). Threaded from `fingerprint` by launch(); unset ⇒ a random persona per session. */
  seed?: string | number;
}

/** Overlay: a red dot that follows real mousemove events. Idempotent IIFE so it works both as
 * an addInitScript source string AND when passed to page.evaluate. */
export const CURSOR_OVERLAY = `(() => {
  if (window.__clearcoteCursor) return; window.__clearcoteCursor = 1;
  const make = () => {
    if (document.getElementById('__clearcote_cursor')) return;
    const d = document.createElement('div'); d.id = '__clearcote_cursor';
    d.style.cssText = 'position:fixed;left:0;top:0;width:20px;height:20px;margin:-10px 0 0 -10px;' +
      'border-radius:50%;border:2px solid #ff3b3b;background:rgba(255,59,59,.22);' +
      'box-shadow:0 0 10px rgba(255,59,59,.6);pointer-events:none;z-index:2147483647';
    (document.body || document.documentElement).appendChild(d);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', make); else make();
  document.addEventListener('mousemove', (e) => {
    const d = document.getElementById('__clearcote_cursor');
    if (d) { d.style.left = e.clientX + 'px'; d.style.top = e.clientY + 'px'; }
  }, true);
})();`;

const rand = (a: number, b: number) => a + Math.random() * (b - a);
const randInt = (a: number, b: number) => a + Math.floor(Math.random() * (b - a + 1)); // inclusive

// Keys a person rolls over (see humanTypeText): lower-case, unshifted, one physical key each.
// Shifted characters, Enter/Backspace and the typo path keep the plain one-press-at-a-time path.
const ROLL_KEYS = new Set("abcdefghijklmnopqrstuvwxyz0123456789 ,.;'/-=");

/** The persona platform when the page cannot say: the SDK's default persona follows the host. */
const hostWheelPlatform = (): WheelPlatform =>
  process.platform === "win32" ? "windows" : process.platform === "darwin" ? "macos" : "linux";

// The engine's pointer-path command. r34 renamed Browser.humanizedClick to Browser.dispatchPointerPath:
// the old name was listed in /json/protocol for anyone who asked, and "humanizedClick" is known
// vocabulary, so the method list alone said what the build was for. r33 and older only know the old
// name, so the new one goes first and the old one is the fallback for a method-not-found only.
/** @internal exported for tests */
export const POINTER_PATH = "Browser.dispatchPointerPath";
/** @internal exported for tests */
export const POINTER_PATH_LEGACY = "Browser.humanizedClick";

// Which name each browser answered to, so only its first call can pay for a miss.
const engineMethodNames = new WeakMap<object, Map<string, string>>();

// A CDP method-not-found: the engine predates the name. A raw CDP client sees code -32601;
// Playwright's CDPSession keeps only the message ("Protocol error (X): 'X' wasn't found").
const isMethodNotFound = (e: any): boolean =>
  e?.code === -32601 || /-32601|wasn't found|method not found/i.test(String(e?.message ?? e));

/** @internal exported for tests. Send `method`, or `legacy` when the engine predates the rename, and
 * remember per `cacheKey` (a Browser, or the context when there is none) which one answered. Any
 * error other than method-not-found propagates as-is: retrying a real failure under the old name
 * would only replay it. */
export async function sendEngineCommand(
  session: { send(method: string, params?: any): Promise<any> }, cacheKey: object,
  method: string, legacy: string, params: Record<string, unknown>
): Promise<any> {
  let names = engineMethodNames.get(cacheKey);
  if (!names) { names = new Map(); engineMethodNames.set(cacheKey, names); }
  const known = names.get(method);
  if (known) return session.send(known, params);
  try {
    const r = await session.send(method, params);
    names.set(method, method);
    return r;
  } catch (e) {
    if (!isMethodNotFound(e)) throw e;
  }
  const r = await session.send(legacy, params);
  names.set(method, legacy);
  return r;
}

// The characters a US-layout keyboard (Playwright's, and the engine's keyboard.getLayoutMap())
// types with Shift held: capitals and the shifted symbols.
const SHIFTED_SYMBOLS = new Set('~!@#$%^&*()_+{}|:"<>?');
/** @internal exported for tests */
export const needsShift = (ch: string): boolean => (ch >= "A" && ch <= "Z" && ch.length === 1) || SHIFTED_SYMBOLS.has(ch);
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
// Same unseeded live source as rand() — the seeded persona rng stays inside motion.ts, this is only
// for placing a point in a plausible spread instead of on an exact landmark.
const gauss = () => gaussFrom(Math.random);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Compact keyboard-adjacency map for realistic fat-finger typos.
const NEARBY: Record<string, string> = {
  a: "sqwz", b: "vghn", c: "xdfv", d: "sfecx", e: "wrsdf", f: "dgrtcv",
  g: "fhtyb", h: "gjybn", i: "ujko", j: "hkunm", k: "jloi", l: "kop",
  m: "njk", n: "bhjm", o: "iklp", p: "ol", q: "wa", r: "edft",
  s: "awedxz", t: "rfgy", u: "yhji", v: "cfgb", w: "qase", x: "zsdc",
  y: "tghu", z: "asx",
};
function nearbyKey(ch: string): string {
  const lo = ch.toLowerCase();
  const n = NEARBY[lo];
  if (!n) return ch;
  const w = n[Math.floor(Math.random() * n.length)];
  return ch === ch.toUpperCase() && ch !== ch.toLowerCase() ? w.toUpperCase() : w;
}

const SELECT_ALL = "Control+a"; // Clearcote ships a Windows binary; Control is correct.

/** Wrap a single page's input methods + (optionally) inject the cursor overlay. */
export async function attachHumanize(browser: Browser, page: Page, opts: HumanizeOptions): Promise<void> {
  // IDEMPOTENCY GUARD — do not remove.
  //
  // installHumanize wraps BOTH browser.newPage and browser.newContext. Playwright's newPage()
  // creates a context internally, so the wrapped newContext runs installHumanizeOnContext (which
  // attaches on the "page" event) and then the outer newPage wrapper attaches a second time.
  //
  // Without this guard the mouse is wrapped twice, and the second
  //     const nativeMove = mouse.move.bind(mouse)
  // captures the FIRST WRAPPER rather than the real native move — so every step of the outer
  // planned path triggers an entire inner planned path. Measured on the same ~980px move:
  //     browser.newPage()   51338 ms   2946 samples
  //     context.newPage()    2833 ms    161 samples   <- correct (planner caps at ~180)
  // i.e. an 18x blow-up on the most common entry point in the API.
  if ((page as any).__clearcoteHumanized) return;
  (page as any).__clearcoteHumanized = true;

  if (opts.showCursor) {
    const inject = () => page.evaluate(CURSOR_OVERLAY).catch(() => {});
    inject();
    page.on("load", () => { inject(); });
    page.on("framenavigated", (f) => { if (f === page.mainFrame()) inject(); });
  }
  if (!opts.humanize) return;

  // Per-identity motor persona (cadence, tremor, overshoot, handedness) — stable across the session,
  // seeded from the fingerprint so behavior is consistent within an identity and unlinkable across
  // seeds. Stored on the page so the module-level Locator.dragTo patch can reach it too.
  const persona: Persona = makePersona(opts.seed);
  (page as any)._clearcotePersona = persona;

  // Tracked cursor position so each move continues from where the last one ended.
  // `held` tracks whether a mouse button is currently down — see mouse.move below.
  // `placed` records whether the engine's cursor has actually been moved yet. A page starts with the
  // pointer at the default origin, so anything that delivers input WITHOUT a target to move to —
  // a wheel, a mouse.down that opens a drag, a bare keyboard.type — otherwise happens from a cursor
  // that has never been anywhere. The guarantees below key off this flag.
  const st = { x: rand(140, 380), y: rand(90, 240), held: false, placed: false };

  const mouse: any = page.mouse;
  const nativeMove = mouse.move.bind(mouse);
  const nativeClick = mouse.click.bind(mouse);
  const nativeWheel = mouse.wheel.bind(mouse);
  const nativeDown = mouse.down.bind(mouse);
  const nativeUp = mouse.up.bind(mouse);

  // Walk a planned trajectory (motion.ts) with native (trusted) mouse.move + off-protocol sleeps.
  // Native input carries the button state, so a button pressed via mouse.down() stays held across
  // the whole path — the same code powers a free move AND a held-button drag.
  const dispatch = async (steps: Step[]): Promise<void> => {
    for (const s of steps) {
      try { await nativeMove(s.x, s.y); } catch { break; }
      st.placed = true;
      await sleep(s.sleepMs);
    }
  };

  // Move to (x, y) as a minimum-jerk sum-of-submovements path (primary ballistic + corrective homing)
  // with colored sub-pixel noise and Fitts-scaled duration. `settle` adds a seating jiggle (drag).
  const glide = async (x: number, y: number, o: { settle?: boolean; targetW?: number } = {}): Promise<void> => {
    await dispatch(planMove(st, { x, y }, persona, { settle: o.settle, targetW: o.targetW }));
    st.x = x; st.y = y;
  };

  // Engine real-trajectory routing (PRO): send point-to-point moves/clicks through
  // Browser.dispatchPointerPath (Browser.humanizedClick before r34, see sendEngineCommand; real
  // recorded human paths for PRO, engine bezier for free, tiered by the engine license gate).
  // Held-button DRAGS stay native; falls back to the SDK bezier if the method is unavailable
  // (older engine / no browser session).
  const eng: { session: any; key: object | null; tid: string | null; ok: boolean } =
    { session: null, key: null, tid: null, ok: true };
  const engineGlide = async (x: number, y: number, noClick: boolean): Promise<boolean> => {
    if (!eng.ok) return false;
    try {
      if (eng.session === null) {
        const ctx: any = page.context();
        const tsession = await ctx.newCDPSession(page);
        const info: any = await tsession.send("Target.getTargetInfo");
        eng.tid = info.targetInfo.targetId;
        const br: any = ctx.browser ? ctx.browser() : null;
        eng.key = br ?? ctx; // the command name is a property of the engine: learn it once per browser
        eng.session = br ? await br.newBrowserCDPSession() : tsession;
      }
      const dx = x - st.x, dy = y - st.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const dur = Math.min(1.10, Math.max(0.28, 0.30 + dist / 1700)) * (0.85 + 0.30 * Math.random());
      await sendEngineCommand(eng.session, eng.key!, POINTER_PATH, POINTER_PATH_LEGACY,
        { targetId: eng.tid, x, y, duration: dur, noClick });
      // The engine walks the trajectory in the BROWSER process and answers this command before it
      // has finished. Without waiting, a following mouse.down() fires at the cursor's STALE
      // position — measured: a move to (110,58) then a press landed at (981,629), on <body>
      // instead of the target, so every move-then-press sequence (drag, press-and-hold,
      // drag-and-drop) acted on the wrong element.
      await sleep(dur * 1000);
      try { await nativeMove(x, y); } catch { /* keep playwright cursor pos in sync for drags */ }
      st.x = x; st.y = y; st.placed = true;
      return true;
    } catch { eng.ok = false; return false; }
  };

  // The REAL viewport, in CSS px — NOT page.viewportSize().
  //
  // installHeadedViewport (index.ts) forces `viewport: null` on every headed newPage/newContext,
  // and launchPersistentContext does the same, so that innerWidth tracks the real OS window
  // instead of Playwright's emulated 1280x720 (an emulated viewport on a headed window is itself
  // a tell). With viewport:null, viewportSize() returns null for the LIFE of the page — so the
  // `|| {1280, 800}` fallback was taken on every headed launch, and the scroll anchor's
  // "is the pointer inside the viewport" gate was evaluated against a box unrelated to the
  // window. On a maximized display a cursor legitimately at x=1400 read as out-of-bounds and got
  // re-homed on EVERY scroll — precisely the behaviour that gate exists to prevent.
  // innerWidth/innerHeight are correct in both modes; the constant is a last resort only.
  let vpCache: { width: number; height: number } | null = null;
  const viewport = async (): Promise<{ width: number; height: number }> => {
    try {
      const wh = await worldFor(page).evaluate<[number, number]>(ISO_VIEWPORT);
      if (wh && wh[0] && wh[1]) { vpCache = { width: wh[0], height: wh[1] }; return vpCache; }
    } catch { /* detached/navigating — fall through */ }
    return (page.viewportSize && page.viewportSize()) || vpCache || { width: 1280, height: 800 };
  };

  // Non-goal cursor activity (idle drift + a few excursions). It never presses a button and never
  // approaches a caller-named target, so it is also the safe way to give the cursor a position when
  // an action needs one but names no coordinates.
  const ambient = async (ms: number): Promise<void> => {
    try {
      const steps = planAmbient(st, await viewport(), persona, ms);
      await dispatch(steps);
      if (steps.length) { st.x = steps[steps.length - 1].x; st.y = steps[steps.length - 1].y; }
    } catch { /* best-effort ambient */ }
  };

  // Give the cursor a position for input that names no coordinates (a bare keyboard.type). It is
  // ambient only: it never presses, so it cannot activate anything it happens to pass over.
  //
  // NOT used before mouse.down — see that wrapper. Before any real move st.x/st.y is the RANDOM
  // SEED assigned at attach, so pressing at the end of this walk would put a TRUSTED click at an
  // arbitrary page coordinate the caller never named, which can land on a link and navigate.
  const ensurePlaced = async (): Promise<void> => {
    if (st.placed) return;
    await ambient(rand(260, 560));
    if (!st.placed) {
      try { await nativeMove(st.x, st.y); st.placed = true; } catch { /* best-effort placement */ }
    }
  };

  // A HELD BUTTON MUST STAY ON THE NATIVE PATH. The engine trajectory builds its own
  // WebMouseEvents and cannot hold a button across them, so routing a drag leg through it emits
  // moves reporting MouseEvent.buttons 0 in the middle of a press. That is both a contradiction no
  // real mouse produces (buttons is derived from physical state) and a functional break — a range
  // thumb, a map or a drop target ignores a move that says nothing is down. Measured before this
  // guard: 672 of 829 held moves reported buttons 0, and a slider dragged 10%->90% never left 0.
  // A BARE MOVE ALWAYS USES THE NATIVE PATH — never the engine trajectory. Two independent
  // reasons, both measured on the Python sync file which shares this design:
  //   * a held button cannot be carried across the engine's own WebMouseEvents, so a drag leg
  //     routed through it reports MouseEvent.buttons 0 mid-press and a range thumb ignores it
  //     (349 of 354 held moves reported buttons 0; the slider moved 0 -> 1 and stopped);
  //   * the engine walks its path ASYNCHRONOUSLY in the browser process and answers before it has
  //     finished, so it keeps moving the cursor AFTER the pre-press pin in mouse.down — a move to
  //     (56,120) then down() pressed on HTML at (856,213) instead of the INPUT, and the slider
  //     stayed at 0. Re-pinning right before the press does not help; the overrun outlives it.
  // mouse.click keeps engine routing: it performs move+click entirely engine-side, where nothing
  // races the trajectory. glide() is still fully humanized (minimum-jerk, colored noise, Fitts).
  mouse.move = async (x: number, y: number) => {
    await glide(x, y);
  };
  // down/up are wrapped to TRACK the held state mouse.move needs, and — critically — to make the
  // press position DETERMINISTIC. The engine answers its pointer path before the trajectory has
  // finished walking, and it overruns the `duration` it reports, so sleeping that duration is not
  // enough: measured on Node, a move to (110,58) was still mid-flight at (416,504) when the press
  // fired, landing on <body>. Re-issuing a native move to the tracked target immediately before
  // the press pins the cursor to where the caller asked, whatever the trajectory is doing.
  mouse.down = async (...a: any[]) => {
    // DELIBERATELY NO PLACEMENT WHEN THE POINTER HAS NEVER MOVED.
    //
    // An earlier revision called ensurePlaced() here and pressed wherever the ambient walk ended.
    // But before any real move st.x/st.y is the RANDOM SEED assigned at attach — not a coordinate
    // the caller named — so that converted a harmless no-op press at the origin into a TRUSTED
    // click at an arbitrary point on the page, which can hit a link and navigate. That is a worse
    // failure than the one it was meant to fix. A bare down() with no preceding move is a caller
    // mistake, and humanize must not invent a target to paper over it; callers who want a
    // humanized drag move first, and that move glides and sets `placed`.
    if (st.placed) {
      // Re-pin only once a real move has happened — that is what makes st.x/st.y correspond to
      // where the engine's cursor actually is. The engine answers its pointer path before its
      // trajectory has finished, so without this the press can fire at a stale position.
      try { await nativeMove(st.x, st.y); } catch { /* best effort: keep the press on target */ }
    }
    st.held = true;
    try {
      return await nativeDown(...a);
    } catch (e) {
      st.held = false; // a raising press must not strand held=true and make every later move a drag leg
      throw e;
    }
  };
  mouse.up = async (...a: any[]) => { try { return await nativeUp(...a); } finally { st.held = false; } };
  mouse.click = async (x: number, y: number) => {
    if (await engineGlide(x, y, false)) return;
    await glide(x, y);
    await sleep(rand(40, 130)); // brief dwell before pressing, like a human
    // delay = the mousedown→mouseup HOLD (human ~60–150 ms); without it Playwright releases in ~2 ms.
    try { await nativeClick(x, y, { delay: clickHold(persona) }); } catch { /* best-effort */ }
  };

  // Opt-in ambient / pre-challenge cursor activity: idle drift + a few non-goal moves so a behavioral
  // collector sees non-zero pointer entropy BEFORE the first goal action (the biggest reason a
  // challenge/slider is shown to headless automation). `await page.ambientMotion(ms)`.
  (page as any).ambientMotion = async (ms = 1200): Promise<void> => { await ambient(ms); };

  // Held-button drag leg with the seating jiggle (settle), reachable from the module-level
  // Locator.dragTo patch (which can't see this closure) via the page object.
  (page as any)._clearcoteHeldGlide = async (x: number, y: number) => { await glide(x, y, { settle: true }); };

  // OPT-IN: no ambient burst on load by default (it interleaves with caller-driven input,
  // e.g. a slider drag). Call page.ambientMotion(ms) explicitly, or set
  // page._clearcoteAutoAmbient = true to restore the per-load burst.
  (page as any)._clearcoteAutoAmbient = false;
  page.on("load", () => {
    if ((page as any)._clearcoteAutoAmbient) {
      Promise.resolve((page as any).ambientMotion(rand(450, 950))).catch(() => {});
    }
  });
  // A wheel event carries the pointer position, and until something moves the cursor that position
  // is the default origin — so a scroll right after a page load reported the mouse parked in the
  // corner, over nothing it was scrolling, while the deltas rolled down the middle of the document.
  // Move to where someone reading this page would have left the cursor first.
  //
  // Only when the current position is unusable (never placed, or outside the viewport). A hand does
  // not lift off the mouse between two scrolls of the same page, so re-homing on every wheel call
  // would replace one tell with another.
  const scrollAnchor = async (): Promise<void> => {
    try {
      const vp = await viewport();
      if (st.placed && st.x > 6 && st.y > 6 && st.x < vp.width - 6 && st.y < vp.height - 6) return;
      // Upper-middle of the reading column, gaussian-jittered: an exact viewport centre is as
      // machine-made as the origin is, so the anchor has to be a distribution, not a landmark.
      const x = clamp(vp.width * 0.48 + gauss() * vp.width * 0.10, vp.width * 0.18, vp.width * 0.82);
      const y = clamp(vp.height * 0.36 + gauss() * vp.height * 0.09, vp.height * 0.14, vp.height * 0.62);
      await glide(x, y);
      await sleep(rand(60, 180)); // eyes land before the finger rolls the wheel
    } catch { /* best-effort: scroll from wherever the cursor is */ }
  };

  // Pixels per wheel notch for the platform this page claims (WHEEL_NOTCH_PX). Read once per page,
  // lazily on the first wheel, in the isolated world like every other humanize read. A page that
  // cannot say (closed, mid-navigation, no CDP) gets the host's platform. Kept for the life of the
  // page either way: a mouse does not change its notch between two scrolls.
  let notchPx: number | null = null;
  const wheelNotchPx = async (): Promise<number> => {
    if (notchPx === null) {
      let plat: WheelPlatform | null = null;
      try { plat = platformFromNavigator(await worldFor(page).evaluate<string>(ISO_PLATFORM)); } catch { /* host below */ }
      notchPx = WHEEL_NOTCH_PX[plat ?? hostWheelPlatform()];
    }
    return notchPx;
  };

  /** Scroll in WHOLE NOTCHES, the only thing a wheel emits. Humanize scrolls whole notches: the
   * delivered total is the requested distance rounded to the nearest notch (at least one), not the
   * exact pixel count.
   *
   * A CDP wheel event always reports wheelDelta ±120 (one notch), whatever its distance. The eased
   * slices this replaced (39/28/20/10/3 px for a 100 px scroll, each at wheelDelta 120, measured on
   * r32/r33) were events no mouse produces: a real notch moves 100 px on Windows, 120 on Linux, 40 on
   * macOS. One axis per event, like a real wheel or tilt, and the notches come in finger-flick bursts. */
  mouse.wheel = async (dx: number, dy: number) => {
    await scrollAnchor();
    const notch = await wheelNotchPx();
    const plan: [number, number][] = [];
    for (let k = wheelNotches(dy, notch); k > 0; k--) plan.push([0, Math.sign(dy) * notch]);
    for (let k = wheelNotches(dx, notch); k > 0; k--) plan.push([Math.sign(dx) * notch, 0]);
    let burst = randInt(2, 5);
    for (let i = 0; i < plan.length; i++) {
      await nativeWheel(plan[i][0], plan[i][1]);
      if (i === plan.length - 1) break;
      // local sleep, NOT page.waitForTimeout: the latter is a CDP round-trip per call, so it
      // emits protocol traffic bot-detectors (e.g. reCAPTCHA) can score — a self-inflicted tell
      // inside the humanize path. setTimeout is off-protocol.
      if (--burst === 0) {
        await sleep(rand(110, 320)); // the finger resets for the next flick
        burst = randInt(2, 5);
      } else {
        await sleep(rand(18, 55));
      }
      if (Math.random() < 0.07) await sleep(rand(150, 450)); // occasional mid-scroll pause (reading)
      // A hand resting on the mouse does not hold it perfectly still through a long scroll. Without
      // this a wheel burst is the one input where the pointer coordinate never changes at all. Never
      // while a button is down, where a move is a drag leg.
      if (plan.length >= 6 && !st.held && Math.random() < 0.06) {
        try { await glide(st.x + gauss() * 3.5, st.y + gauss() * 2.5); } catch { /* drift is cosmetic */ }
      }
    }
  };

  // ----------------------------------------------------------------- keyboard
  const keyboard: any = page.keyboard;
  const nativeKbType = keyboard.type.bind(keyboard);
  const nativeKbPress = keyboard.press.bind(keyboard);
  // A page-like keyboard without down/up still gets every other humanization; it just types
  // capitals without the Shift key, as before.
  const nativeKbDown = typeof keyboard.down === "function" ? keyboard.down.bind(keyboard) : undefined;
  const nativeKbUp = typeof keyboard.up === "function" ? keyboard.up.bind(keyboard) : undefined;

  // Emit one character with a human keydown→keyup DWELL. keyboard.press's `delay` IS the hold
  // (keyboard.type's delay is only inter-key flight), so per-char press gives the missing dwell.
  // Fall back to type() for anything press can't map (some symbols / composed chars).
  const emitKey = async (ch: string): Promise<void> => {
    try { await nativeKbPress(ch, { delay: keyDwell(persona) }); }
    catch { try { await nativeKbType(ch); } catch { /* best-effort */ } }
  };

  // Type text key-by-key with human timing. Each char goes through the native keyboard (trusted),
  // so this stays isTrusted===true.
  //
  // Capitals and shifted symbols are typed the way a person types them: ShiftLeft goes down a moment
  // before the key (35-120 ms) and comes up just after it (10-70 ms), held across a run of them.
  // Playwright's press("A") alone sent the key with shiftKey=false and no Shift key at all (measured
  // on r28 with "Ab!") -- input no keyboard produces.
  //
  // Fast pairs ROLL OVER: the next key goes down before the previous one comes up. One press() per
  // character never overlaps (0 of 32 keydowns, measured on r32/r33), a keystroke-dynamics tell;
  // real typists overlap many fast pairs. The share is the persona's rolloverRate, stable per seed.
  const rollRate = rolloverRate(persona);
  const humanTypeText = async (text: string): Promise<void> => {
    const chars = Array.from(text);
    const n = chars.length;
    let shift = false;
    try {
      for (let i = 0; i < n; i++) {
        let ch = chars[i];
        const need = needsShift(ch);
        if (need && !shift && nativeKbDown && nativeKbUp) {
          await nativeKbDown("Shift");
          shift = true;
          await sleep(rand(35, 120));
        }
        // occasional fat-finger on unshifted alnum chars, then notice + correct
        let typo = false;
        if (!need && /[a-z0-9]/.test(ch) && Math.random() < 0.02) {
          typo = true;
          try {
            await emitKey(nearbyKey(ch));
            await sleep(rand(120, 300));
            await nativeKbPress("Backspace", { delay: keyDwell(persona) });
            await sleep(rand(80, 200));
          } catch { /* typo path best-effort */ }
        }
        // Rollover of ch and the next char: unshifted single-key pairs only, never a key with itself,
        // never after a typo. keyboard.down inserts the character exactly as press does, so the typed
        // value is unchanged. The overlap leaves ch down alone for at least 8 ms first.
        const nx = chars[i + 1];
        let rolled = false;
        if (nativeKbDown && nativeKbUp && !shift && i + 1 < n && !typo && ROLL_KEYS.has(ch)
            && ROLL_KEYS.has(nx) && ch !== nx && Math.random() < rollRate) {
          const d1 = keyDwell(persona), d2 = keyDwell(persona);
          if (d1 - 8 >= 12) {
            const overlap = rand(12, Math.min(55, d1 - 8));
            try {
              await nativeKbDown(ch);
              await sleep(d1 - overlap);
              await nativeKbDown(nx);
              await sleep(overlap);
              await nativeKbUp(ch);
              await sleep(Math.max(5, d2 - overlap));
              await nativeKbUp(nx);
            } catch {
              // never strand a key down for the caller's next keystroke
              try { await nativeKbUp(ch); } catch { /* best-effort */ }
              try { await nativeKbUp(nx); } catch { /* best-effort */ }
              break;
            }
            rolled = true;
            i += 1; ch = nx; // nx is typed; the cadence below runs as if after it
          }
        }
        if (!rolled) { try { await emitKey(ch); } catch { break; } }
        if (shift && (i === n - 1 || !needsShift(chars[i + 1]))) {
          await sleep(rand(10, 70));
          await nativeKbUp!("Shift");
          shift = false;
        }
        if (i < n - 1) {
          if (Math.random() < 0.06) await sleep(rand(180, 450)); // brief thinking pause
          else await sleep(rand(45, 150));
          if (/\s/.test(ch)) await sleep(rand(20, 100)); // slight extra pause at word boundaries
        }
      }
    } finally {
      // never leave Shift held for the caller's next keystroke
      if (shift) { try { await nativeKbUp!("Shift"); } catch { /* best-effort */ } }
    }
  };

  // Keystrokes from a session in which the mouse never existed. Selector-based typing glides to the
  // field first (focusClick), but page.keyboard.type/press names no target, and on a page focused by
  // autofocus or by Tab it can be the first input of any kind — a keydown stream with zero preceding
  // pointer events is one of the cheapest checks a collector can run.
  //
  // AMBIENT ONLY. Deliberately NOT a move toward the focused field: the caller has already chosen
  // where the text goes, and approaching that element would hover it (and, on a control that reacts
  // to hover, move it) for no behavioral gain. Once per page — after that the cursor has a history,
  // and warming up before every keystroke would itself be the anomaly.
  let kbWarmed = false;
  const keyboardWarmup = async (): Promise<void> => {
    if (kbWarmed || st.placed) return;
    kbWarmed = true;
    await ambient(rand(280, 620));
  };

  keyboard.type = async (text: string) => { await keyboardWarmup(); await humanTypeText(text); };

  // keyboard.press with the persona's hold, unless the caller specified their own.
  //
  // WHY THIS EXISTS. keyboard.type was humanised from the start and keyboard.press was not, which
  // left the most common single-key call in any script — Enter, Tab, Escape, arrow keys — emitting
  // a keydown and keyUp in the same instant. Measured with humanize ON, before this wrapper:
  // keyboard.type held keys 58–107ms while keyboard.press held them 1.4–3.8ms. A finger cannot
  // press and release a key in under a millisecond, and a detector reading the SHORTEST dwell in a
  // session sees the press path — so one un-humanised call undid the whole keyboard persona.
  //
  // The caller's own `delay` wins: press(key, {delay}) is the documented way to hold a key for a
  // specific time, and silently overriding it would break that. Only the DEFAULT changes.
  keyboard.press = async (key: string, options: any = {}) => {
    await keyboardWarmup();
    const opts = options && typeof options === "object" ? options : {};
    if (opts.delay === undefined) opts.delay = keyDwell(persona);
    return nativeKbPress(key, opts);
  };

  // ------------------------------------------------------- page-level targeted helpers
  // Read in an isolated world (./isolated.ts): the page never sees the lookup.
  const isFocused = async (selector: string): Promise<boolean> =>
    Boolean(await worldFor(page).evaluate<boolean>(ISO_IS_FOCUSED, selector));

  const pointFor = async (selector: string, timeout: number): Promise<{ x: number; y: number } | null> => {
    const loc = page.locator(selector).first();
    await loc.waitFor({ state: "visible", timeout });
    await loc.scrollIntoViewIfNeeded({ timeout });
    if (!(await loc.isEnabled())) return null;
    let box = await loc.boundingBox();
    if (!box) return null;
    await sleep(50); // stability: a box that's still moving = an animation in flight
    const box2 = await loc.boundingBox();
    if (!box2 || Math.abs(box2.x - box.x) > 1 || Math.abs(box2.y - box.y) > 1) return null;
    box = box2;
    return clickPoint(box, st, persona); // 2D gaussian toward center, nudged to the approach side
  };

  const focusClick = async (selector: string, timeout: number): Promise<boolean> => {
    const pt = await pointFor(selector, timeout);
    if (!pt) return false;
    await glide(pt.x, pt.y);
    await sleep(rand(40, 130));
    await nativeClick(pt.x, pt.y, { delay: clickHold(persona) });
    return true;
  };

  const nativePageType = (page as any).type ? (page as any).type.bind(page) : null;
  const nativePageFill = page.fill.bind(page);
  const nativePagePress = page.press.bind(page);
  const nativePageDblclick = page.dblclick.bind(page);
  const nativePageSelect = (page as any).selectOption ? (page as any).selectOption.bind(page) : null;

  (page as any).type = async (selector: string, text: string, options: any = {}) => {
    try {
      const timeout = options.timeout ?? 30000;
      if (!(await isFocused(selector))) {
        if (!(await focusClick(selector, timeout))) {
          return nativePageType ? nativePageType(selector, text, options) : nativePageFill(selector, text, options);
        }
        await sleep(rand(40, 120));
      }
      await humanTypeText(text);
    } catch {
      return nativePageType ? nativePageType(selector, text, options) : nativePageFill(selector, text, options);
    }
  };

  (page as any).fill = async (selector: string, value: string, options: any = {}) => {
    try {
      const timeout = options.timeout ?? 30000;
      if (value.length > 200) return nativePageFill(selector, value, options); // bulk -> atomic
      if (!(await focusClick(selector, timeout))) return nativePageFill(selector, value, options);
      await sleep(rand(40, 120));
      try {
        await nativeKbPress(SELECT_ALL);
        await sleep(rand(30, 80));
        await nativeKbPress("Backspace");
        await sleep(rand(40, 120));
      } catch { /* clear best-effort */ }
      await humanTypeText(value);
    } catch {
      return nativePageFill(selector, value, options);
    }
  };

  (page as any).dblclick = async (selector: string, options: any = {}) => {
    try {
      const timeout = options.timeout ?? 30000;
      const pt = await pointFor(selector, timeout);
      if (!pt) return nativePageDblclick(selector, options);
      await glide(pt.x, pt.y);
      await sleep(rand(40, 130));
      await nativeClick(pt.x, pt.y, { clickCount: 2, delay: rand(40, 90) });
    } catch {
      return nativePageDblclick(selector, options);
    }
  };

  (page as any).press = async (selector: string, key: string, options: any = {}) => {
    try {
      const timeout = options.timeout ?? 30000;
      if (!(await isFocused(selector))) {
        if (!(await focusClick(selector, timeout))) return nativePagePress(selector, key, options);
        await sleep(rand(40, 120));
      }
      // The persona's hold, for the same reason keyboard.press above carries one: this is the
      // path locator.press() delegates to, so without it every locator.press in a script emitted
      // a zero-length keypress while the typed text around it looked human.
      await nativeKbPress(key, { delay: keyDwell(persona) });
    } catch {
      return nativePagePress(selector, key, options);
    }
  };

  // page.selectOption was the last input method humanize did not touch, so a script that used it —
  // rather than locator.selectOption, which has been patched for a while — went straight to
  // Playwright's implementation: assign the value, dispatch input+change from page script. That is
  // measurably scored: it fails 'interaction-select-change-trust' on clearcotelabs.com/audit,
  // because no user gesture can produce a change event with isTrusted false.
  //
  // The move is a HOVER, not a click: clicking a <select> opens the native popup, and the popup
  // swallows the arrow keys the trusted path steps the selection with. A person's pointer is on the
  // dropdown they are choosing from either way.
  if (nativePageSelect) {
    (page as any).selectOption = async (selector: string, values: any, options: any = {}) => {
      const timeout = options.timeout ?? 30000;
      try {
        const pt = await pointFor(selector, timeout);
        if (pt) { await glide(pt.x, pt.y); await sleep(rand(50, 150)); }
      } catch { /* the pre-move is best-effort; a trusted selection still beats a native one */ }
      try {
        const got = await trustedSelect(page, selector, values, timeout);
        if (got) return got;
      } catch { /* fall back */ }
      return nativePageSelect(selector, values, options);
    };
  }

  const wrapTargeted = (name: "click" | "hover", noClick: boolean) => {
    const orig = (page as any)[name].bind(page);
    (page as any)[name] = async (selector: string, options: any = {}) => {
      // Actionability pre-flight before a TRUSTED humanized click: visible+enabled+stable+
      // not-covered, else fall back to the native click (which has its own waits) — only improves.
      try {
        const timeout = options.timeout ?? 30000;
        const loc = page.locator(selector).first();
        await loc.waitFor({ state: "visible", timeout });
        await loc.scrollIntoViewIfNeeded({ timeout });
        if (!(await loc.isEnabled())) return orig(selector, options);
        let box = await loc.boundingBox();
        if (!box) return orig(selector, options);
        await sleep(50);
        const box2 = await loc.boundingBox();
        if (!box2 || Math.abs(box2.x - box.x) > 1 || Math.abs(box2.y - box.y) > 1) {
          return orig(selector, options);
        }
        box = box2;
        const cp = clickPoint(box, st, persona);
        const x = cp.x, y = cp.y;
        // covered-by: don't fire a trusted click at a point some overlay owns. A TRIAL action runs
        // Playwright's own hit test at exactly this point, in its utility world, and dispatches
        // nothing; the old page.evaluate(elementFromPoint) ran in the page's world, where a hooked
        // prototype saw it. A covered point times out fast -> the native path waits for it.
        try {
          await orig(selector, { trial: true, timeout: COVER_CHECK_MS, position: { x: x - box.x, y: y - box.y } });
        } catch {
          return orig(selector, options); // covered -> let PW wait for it on top
        }
        await glide(x, y);
        if (!noClick) { await sleep(rand(40, 130)); await nativeClick(x, y, { delay: clickHold(persona) }); }
        return;
      } catch {
        return orig(selector, options);
      }
    };
  };
  wrapTargeted("click", false);
  wrapTargeted("hover", true);

  // Marker the Locator-class patch keys off: humanize is active for THIS page.
  (page as any)._clearcoteHumanized = true;
  patchLocatorClass(page.locator("html"));
}

/** Choose a <select> option with the keyboard, so the ENGINE fires the events. Returns Playwright's
 * `[value]` result once the selection is VERIFIED, or null when the keyboard route cannot be shown
 * to have worked and the caller should fall back to native.
 *
 * Playwright's selectOption assigns the value and dispatches input+change from script. Those arrive
 * with isTrusted false, which is the single most reliable dropdown tell there is — the engine cannot
 * produce an untrusted change, so a page reading the flag knows the selection was not made by a
 * person. A <select> that has focus and is CLOSED steps on ArrowUp/ArrowDown and the browser emits
 * input then change itself, trusted, exactly as it does for a mouse. The fix is not to forge better
 * events, it is to stop forging them.
 *
 * Returns null whenever that cannot be guaranteed: a multi- or disabled select, an option that
 * cannot be resolved, an ElementHandle target, or a platform where arrows open the popup instead of
 * stepping (macOS). selectedIndex is verified afterwards rather than assumed — a silently wrong
 * selection is worse than an untrusted one. */
async function trustedSelect(
  page: any, selector: string, values: any, timeout: number
): Promise<string[] | null> {
  try {
    const one = (v: any) => (Array.isArray(v) && v.length === 1 ? v[0] : v);
    const v = one(values);
    let by: string | null = null, want: any = null;
    if (v && typeof v === "object" && !(v as any).constructor?.name?.includes("ElementHandle")) {
      if ((v as any).index != null) { by = "index"; want = (v as any).index; }
      else if ((v as any).label != null) { by = "label"; want = (v as any).label; }
      else if ((v as any).value != null) { by = "value"; want = (v as any).value; }
    } else if (typeof v === "string") { by = "value"; want = v; }
    if (by === null) return null;
    // Read in an isolated world (./isolated.ts): the page never sees the option lookup.
    const plan = await worldFor(page).evaluate<{ to: number; from: number; ret: string } | null>(
      ISO_SELECT_PLAN, { sel: selector, by, want });
    if (!plan) return null;
    if (plan.to === plan.from) return [plan.ret];   // already selected; forge nothing
    await page.focus(selector, { timeout });
    await sleep(rand(60, 160));
    const step = plan.to > plan.from ? "ArrowDown" : "ArrowUp";
    for (let i = 0; i < Math.abs(plan.to - plan.from); i++) {
      // page.keyboard.press — humanized by attachHumanize, so the hold comes with it.
      await page.keyboard.press(step);
      await sleep(rand(45, 120));
    }
    const got = await worldFor(page).evaluate<number>(ISO_SELECTED_INDEX, selector);
    if (got === plan.to) return [plan.ret];
  } catch { /* fall back */ }
  return null;
}

// --------------------------------------------------------------------------- locator patch
let locatorPatched = false;

/** Patch the Locator prototype once so locator.* interactions route through the humanized page.*
 * methods. Each is a no-op unless the locator's page has humanize active AND the locator targets
 * the main frame; anything else (frame locators, exotic selectors, errors) falls through to the
 * original Playwright behaviour. Composes if another library already patched the prototype. */
function patchLocatorClass(sample: Locator): void {
  if (locatorPatched) return;
  locatorPatched = true;
  const proto: any = Object.getPrototypeOf(sample);
  if (!proto) return;

  const oFill = proto.fill, oClick = proto.click, oType = proto.type, oDbl = proto.dblclick,
    oHover = proto.hover, oPress = proto.press, oPressSeq = proto.pressSequentially,
    oClear = proto.clear, oTap = proto.tap, oCheck = proto.check, oUncheck = proto.uncheck,
    oDragTo = proto.dragTo, oSelectOption = proto.selectOption;

  const on = (self: any): boolean => {
    const pg: any = self.page();
    if (!pg || !pg._clearcoteHumanized) return false;
    try { return self._frame === pg.mainFrame(); } catch { return false; }
  };
  const sel = (self: any): string => self._selector;
  const fwd = (kw: any) => (kw && kw.timeout != null ? { timeout: kw.timeout } : {});

  proto.fill = async function (this: any, value: string, kw: any = {}) {
    if (on(this)) { try { return await this.page().fill(sel(this), value, fwd(kw)); } catch { /* fall back */ } }
    return oFill.call(this, value, kw);
  };
  proto.click = async function (this: any, kw: any = {}) {
    if (on(this)) { try { return await this.page().click(sel(this), fwd(kw)); } catch { /* fall back */ } }
    return oClick.call(this, kw);
  };
  proto.type = async function (this: any, text: string, kw: any = {}) {
    if (on(this)) { try { return await this.page().type(sel(this), text, fwd(kw)); } catch { /* fall back */ } }
    return oType.call(this, text, kw);
  };
  proto.dblclick = async function (this: any, kw: any = {}) {
    if (on(this)) { try { return await this.page().dblclick(sel(this), fwd(kw)); } catch { /* fall back */ } }
    return oDbl.call(this, kw);
  };
  proto.hover = async function (this: any, kw: any = {}) {
    if (on(this)) { try { return await this.page().hover(sel(this), fwd(kw)); } catch { /* fall back */ } }
    return oHover.call(this, kw);
  };
  proto.press = async function (this: any, key: string, kw: any = {}) {
    if (on(this)) { try { return await this.page().press(sel(this), key, fwd(kw)); } catch { /* fall back */ } }
    return oPress.call(this, key, kw);
  };
  proto.pressSequentially = async function (this: any, text: string, kw: any = {}) {
    if (on(this)) { try { return await this.page().type(sel(this), text, fwd(kw)); } catch { /* fall back */ } }
    return oPressSeq.call(this, text, kw);
  };
  proto.clear = async function (this: any, kw: any = {}) {
    if (on(this)) { try { return await this.page().fill(sel(this), "", fwd(kw)); } catch { /* fall back */ } }
    return oClear.call(this, kw);
  };
  proto.tap = async function (this: any, kw: any = {}) {
    if (on(this)) { try { return await this.page().click(sel(this), fwd(kw)); } catch { /* fall back */ } }
    return oTap.call(this, kw);
  };
  proto.check = async function (this: any, kw: any = {}) {
    if (on(this)) {
      try { if (!(await this.isChecked())) return await this.page().click(sel(this), fwd(kw)); return; } catch { /* fall back */ }
    }
    return oCheck.call(this, kw);
  };
  proto.uncheck = async function (this: any, kw: any = {}) {
    if (on(this)) {
      try { if (await this.isChecked()) return await this.page().click(sel(this), fwd(kw)); return; } catch { /* fall back */ }
    }
    return oUncheck.call(this, kw);
  };
  proto.selectOption = async function (this: any, values: any, kw: any = {}) {
    // Routed through the page method (which glides to the control first) rather than calling
    // trustedSelect straight, so a locator selection gets the same pre-move as a locator click.
    if (on(this)) { try { return await this.page().selectOption(sel(this), values, fwd(kw)); } catch { /* fall back */ } }
    return oSelectOption.call(this, values, kw);
  };

  proto.dragTo = async function (this: any, target: any, kw: any = {}) {
    if (on(this)) {
      try {
        const pg: any = this.page();
        const sb = await this.boundingBox();
        const tb = await target.boundingBox();
        if (sb && tb) {
          const sx = sb.x + sb.width / 2, sy = sb.y + sb.height / 2;
          const tx = tb.x + tb.width / 2, ty = tb.y + tb.height / 2;
          // Human endpoint dynamics (the two worst slider tells): a grab hesitation AFTER pressing and
          // a settle dwell BEFORE releasing, drawn from the page's motor persona.
          const persona: Persona | undefined = pg._clearcotePersona;
          const { grabMs, releaseMs } = persona ? dragDwell(persona) : { grabMs: rand(130, 360), releaseMs: rand(90, 230) };
          await pg.mouse.move(sx, sy); await sleep(rand(100, 200));
          await pg.mouse.down(); await sleep(grabMs);   // grab hesitation (native -> button held across glide)
          const heldGlide = pg._clearcoteHeldGlide;     // humanized held-button drag + seating jiggle (settle)
          if (heldGlide) await heldGlide(tx, ty); else await pg.mouse.move(tx, ty);
          await sleep(releaseMs);                       // pre-release settle before letting go
          await pg.mouse.up();
          return;
        }
      } catch { /* fall back */ }
    }
    return oDragTo.call(this, target, kw);
  };
}

/** Install humanize/showCursor on a browser: wraps newPage/newContext so every page is covered. */
export function installHumanize(browser: Browser, opts: HumanizeOptions): void {
  if (!opts.humanize && !opts.showCursor) return;
  const origNewPage = browser.newPage.bind(browser);
  (browser as any).newPage = async (options?: any) => {
    const page = await origNewPage(options);
    await attachHumanize(browser, page, opts);
    return page;
  };
  const origNewContext = browser.newContext.bind(browser);
  (browser as any).newContext = async (options?: any) => {
    const ctx = await origNewContext(options);
    installHumanizeOnContext(ctx, opts, browser);
    return ctx;
  };
}

/** Install humanize/showCursor on a single context (used for launchPersistentContext). */
export function installHumanizeOnContext(
  context: BrowserContext, opts: HumanizeOptions, browser?: Browser | null
): void {
  if (!opts.humanize && !opts.showCursor) return;
  const b = browser ?? context.browser();
  if (opts.showCursor) { context.addInitScript(CURSOR_OVERLAY).catch(() => {}); }
  if (b) {
    context.on("page", (p) => { attachHumanize(b, p, opts).catch(() => {}); });
    for (const p of context.pages()) attachHumanize(b, p, opts).catch(() => {});
  }
}
