// Humanized input + cursor visualization.
//
// `humanize: true` installs ONE consistent human-input standard covering moving, clicking,
//   dragging, scrolling and typing — dispatched as native trusted input (isTrusted===true,
//   navigator.webdriver stays false). It works whether you drive the page directly or via
//   locators:
//     page level    — page.click / hover / dblclick / type / fill / press, page.mouse.move /
//                      click / wheel and held-button drags (down -> move -> up), page.keyboard.type
//     locator level  — locator.click / type / fill / hover / dblclick / press /
//                      pressSequentially / clear / check / uncheck / tap / dragTo (routed through
//                      the humanized page methods; main-frame locators only, native fallback)
// `showCursor: true` — injects a red cursor dot that follows the real mousemove events.
//
// Mouse moves build an eased, slightly bowed cubic-bezier path SDK-side from the tracked cursor
// position (continuous — no snap back to 0,0) using Playwright's NATIVE mouse.move. Because it is
// native input, the button state is carried: down() -> move() -> up() is a real held-button drag
// (slider captchas work) with no separate CDP channel to desync. Typing goes key-by-key through
// the native keyboard (trusted; shift handled by the engine) with human inter-key timing. No
// special binary command required.

import type { Browser, BrowserContext, Locator, Page } from "playwright-core";
import { makePersona, planMove, dragDwell, clickHold, keyDwell, clickPoint, planAmbient, type Persona, type Step } from "./motion.js";

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
  const st = { x: rand(140, 380), y: rand(90, 240) };

  const mouse: any = page.mouse;
  const nativeMove = mouse.move.bind(mouse);
  const nativeClick = mouse.click.bind(mouse);
  const nativeWheel = mouse.wheel.bind(mouse);

  // Walk a planned trajectory (motion.ts) with native (trusted) mouse.move + off-protocol sleeps.
  // Native input carries the button state, so a button pressed via mouse.down() stays held across
  // the whole path — the same code powers a free move AND a held-button drag.
  const dispatch = async (steps: Step[]): Promise<void> => {
    for (const s of steps) {
      try { await nativeMove(s.x, s.y); } catch { break; }
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
  // Browser.humanizedClick (real recorded human paths for PRO, engine bezier for free,
  // tiered by the engine license gate). Held-button DRAGS stay native; falls back to the
  // SDK bezier if the method is unavailable (older engine / no browser session).
  const eng: { session: any; tid: string | null; ok: boolean } = { session: null, tid: null, ok: true };
  const engineGlide = async (x: number, y: number, noClick: boolean): Promise<boolean> => {
    if (!eng.ok) return false;
    try {
      if (eng.session === null) {
        const ctx: any = page.context();
        const tsession = await ctx.newCDPSession(page);
        const info: any = await tsession.send("Target.getTargetInfo");
        eng.tid = info.targetInfo.targetId;
        const br: any = ctx.browser ? ctx.browser() : null;
        eng.session = br ? await br.newBrowserCDPSession() : tsession;
      }
      const dx = x - st.x, dy = y - st.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const dur = Math.min(1.10, Math.max(0.28, 0.30 + dist / 1700)) * (0.85 + 0.30 * Math.random());
      await eng.session.send("Browser.humanizedClick", { targetId: eng.tid, x, y, duration: dur, noClick });
      try { await nativeMove(x, y); } catch { /* keep playwright cursor pos in sync for drags */ }
      st.x = x; st.y = y;
      return true;
    } catch { eng.ok = false; return false; }
  };

  mouse.move = async (x: number, y: number) => { if (!(await engineGlide(x, y, true))) await glide(x, y); };
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
  (page as any).ambientMotion = async (ms = 1200): Promise<void> => {
    try {
      const vp = (page.viewportSize && page.viewportSize()) || { width: 1280, height: 800 };
      const steps = planAmbient(st, { width: vp.width, height: vp.height }, persona, ms);
      await dispatch(steps);
      if (steps.length) { st.x = steps[steps.length - 1].x; st.y = steps[steps.length - 1].y; }
    } catch { /* best-effort ambient */ }
  };

  // Held-button drag leg with the seating jiggle (settle), reachable from the module-level
  // Locator.dragTo patch (which can't see this closure) via the page object.
  (page as any)._clearcoteHeldGlide = async (x: number, y: number) => { await glide(x, y, { settle: true }); };

  // DEFAULT under humanize (no longer opt-in): fire a short ambient burst on every load.
  (page as any)._clearcoteAutoAmbient = true;
  page.on("load", () => {
    if ((page as any)._clearcoteAutoAmbient) {
      Promise.resolve((page as any).ambientMotion(rand(450, 950))).catch(() => {});
    }
  });
  // scroll easing: break into eased chunks of native wheel deltas
  mouse.wheel = async (dx: number, dy: number) => {
    const steps = Math.max(5, Math.min(24, Math.round((Math.abs(dx) + Math.abs(dy)) / 60)));
    const ease = (u: number) => 1 - Math.pow(1 - u, 2.2); // ease-OUT: fast flick -> slow inertial settle
    let px = 0, py = 0;
    for (let i = 1; i <= steps; i++) {
      const f = ease(i / steps);
      const nxw = Math.round(dx * f), nyw = Math.round(dy * f);
      await nativeWheel(nxw - px, nyw - py); px = nxw; py = nyw;
      // local sleep, NOT page.waitForTimeout: the latter is a CDP round-trip per call, so it
      // emits protocol traffic bot-detectors (e.g. reCAPTCHA) can score — a self-inflicted tell
      // inside the humanize path. setTimeout is off-protocol.
      await sleep(rand(10, 38));
      if (Math.random() < 0.07) await sleep(rand(40, 120)); // occasional mid-scroll pause (reading)
    }
    if (px !== dx || py !== dy) await nativeWheel(dx - px, dy - py);
  };

  // ----------------------------------------------------------------- keyboard
  const keyboard: any = page.keyboard;
  const nativeKbType = keyboard.type.bind(keyboard);
  const nativeKbPress = keyboard.press.bind(keyboard);

  // Emit one character with a human keydown→keyup DWELL. keyboard.press's `delay` IS the hold
  // (keyboard.type's delay is only inter-key flight), so per-char press gives the missing dwell.
  // Fall back to type() for anything press can't map (some symbols / composed chars).
  const emitKey = async (ch: string): Promise<void> => {
    try { await nativeKbPress(ch, { delay: keyDwell(persona) }); }
    catch { try { await nativeKbType(ch); } catch { /* best-effort */ } }
  };

  // Type text key-by-key with human timing. Each char goes through the native keyboard (trusted;
  // shift/symbols handled by the engine), so this stays isTrusted===true.
  const humanTypeText = async (text: string): Promise<void> => {
    const n = text.length;
    for (let i = 0; i < n; i++) {
      const ch = text[i];
      if (/[a-zA-Z0-9]/.test(ch) && Math.random() < 0.02) {
        try {
          await emitKey(nearbyKey(ch));
          await sleep(rand(120, 300));
          await nativeKbPress("Backspace", { delay: keyDwell(persona) });
          await sleep(rand(80, 200));
        } catch { /* typo path best-effort */ }
      }
      try { await emitKey(ch); } catch { break; }
      if (i < n - 1) {
        if (Math.random() < 0.06) await sleep(rand(180, 450)); // brief thinking pause
        else await sleep(rand(45, 150));
        if (/\s/.test(ch)) await sleep(rand(20, 100)); // slight extra pause at word boundaries
      }
    }
  };
  keyboard.type = async (text: string) => { await humanTypeText(text); };

  // ------------------------------------------------------- page-level targeted helpers
  const isFocused = async (selector: string): Promise<boolean> => {
    try {
      return await (page.evaluate as (fn: unknown, arg: unknown) => Promise<boolean>)((s: string) => {
        const e: any = (globalThis as any).document.querySelector(s);
        return !!e && e === (globalThis as any).document.activeElement;
      }, selector);
    } catch { return false; }
  };

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
      await nativeKbPress(key);
    } catch {
      return nativePagePress(selector, key, options);
    }
  };

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
        try {
          const handle = await loc.elementHandle();
          const covered =
            handle &&
            (await (page.evaluate as (fn: unknown, arg: unknown) => Promise<boolean>)(
              ([px, py, el]: [number, number, any]) => {
                const t: any = (globalThis as any).document.elementFromPoint(px, py);
                return !(t && (t === el || el.contains(t) || t.contains(el)));
              },
              [x, y, handle]
            ));
          if (covered) return orig(selector, options); // covered -> let PW wait for it on top
        } catch { /* covered-by check best-effort */ }
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
    oDragTo = proto.dragTo;

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
