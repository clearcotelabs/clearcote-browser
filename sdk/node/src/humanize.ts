// Humanized input + cursor visualization.
//
// `humanize: true`  — routes page.click/hover/mouse.move/mouse.click through the engine's
//   `Browser.humanizedClick` CDP command (real trusted WebMouseEvent along a cubic-bezier path,
//   isTrusted===true, navigator.webdriver stays false), and eases mouse.wheel SDK-side.
// `showCursor: true` — injects a red cursor dot that follows the real mousemove events the engine
//   fires, so you can watch the motion (cross-platform; no native overlay needed).
//
// Requires a Clearcote binary that exposes Browser.humanizedClick (the humanized-input engine
// build). On an older binary the command is absent; humanize falls back to native Playwright input.

import type { Browser, BrowserContext, CDPSession, Page } from "playwright-core";

export interface HumanizeOptions {
  /** Route clicks/moves/scroll through the engine's humanized (bezier, trusted) input. */
  humanize?: boolean;
  /** Show a red cursor dot that tracks the motion (visualization). */
  showCursor?: boolean;
}

/** Overlay: a red dot that follows real mousemove events. Idempotent IIFE so it works both as
 * an addInitScript source string AND when passed to page.evaluate (Node treats a bare arrow
 * string as an uncalled expression — an IIFE actually runs). */
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

async function pageTargetId(page: Page): Promise<string> {
  const s = await page.context().newCDPSession(page);
  const info: any = await s.send("Target.getTargetInfo");
  return info.targetInfo.targetId;
}

/** Wrap a single page's input methods + (optionally) inject the cursor overlay. */
export async function attachHumanize(browser: Browser, page: Page, opts: HumanizeOptions): Promise<void> {
  if (opts.showCursor) {
    const inject = () => page.evaluate(CURSOR_OVERLAY).catch(() => {});
    inject();
    page.on("load", () => { inject(); });
    page.on("framenavigated", (f) => { if (f === page.mainFrame()) inject(); });
  }
  if (!opts.humanize) return;

  let bsession: CDPSession | null = null;
  let tid: string | null = null;
  let supported = true;

  const humanizedMove = async (x: number, y: number, noClick: boolean, duration?: number): Promise<boolean> => {
    if (!supported) return false;
    try {
      bsession ??= await browser.newBrowserCDPSession();
      tid ??= await pageTargetId(page);
      await bsession.send("Browser.humanizedClick" as any, {
        targetId: tid, x: Math.round(x), y: Math.round(y),
        duration: duration ?? rand(0.45, 0.95), noClick,
      } as any);
      return true;
    } catch (e) {
      // older binary without the command -> stop trying, fall back to native input
      supported = false;
      return false;
    }
  };

  const mouse: any = page.mouse;
  const nativeMove = mouse.move.bind(mouse);
  const nativeClick = mouse.click.bind(mouse);
  const nativeWheel = mouse.wheel.bind(mouse);

  mouse.move = async (x: number, y: number, options?: any) => {
    if (!(await humanizedMove(x, y, true))) return nativeMove(x, y, options);
  };
  mouse.click = async (x: number, y: number, options?: any) => {
    if (!(await humanizedMove(x, y, false))) return nativeClick(x, y, options);
  };
  // scroll easing (engine humanizedClick doesn't cover wheel): break into eased chunks
  mouse.wheel = async (dx: number, dy: number) => {
    const steps = Math.max(5, Math.min(20, Math.round((Math.abs(dx) + Math.abs(dy)) / 80)));
    const ease = (u: number) => u * u * (3 - 2 * u);
    let px = 0, py = 0;
    for (let i = 1; i <= steps; i++) {
      const f = ease(i / steps);
      const nx = Math.round(dx * f), ny = Math.round(dy * f);
      await nativeWheel(nx - px, ny - py); px = nx; py = ny;
      // local sleep, NOT page.waitForTimeout: the latter is a CDP round-trip per call, so it
      // emits protocol traffic bot-detectors (e.g. reCAPTCHA) can score — a self-inflicted tell
      // inside the humanize path. setTimeout is off-protocol.
      await new Promise((r) => setTimeout(r, rand(12, 45)));
    }
    if (px !== dx || py !== dy) await nativeWheel(dx - px, dy - py);
  };

  const wrapTargeted = (name: "click" | "hover", noClick: boolean) => {
    const orig = (page as any)[name].bind(page);
    (page as any)[name] = async (selector: string, options: any = {}) => {
      // Actionability pre-flight before a TRUSTED humanized click: a native Playwright click waits
      // for visible+enabled+stable+receives-events; the humanized path dispatches a real OS-level
      // event at a point, so without these checks it could fire under a cookie banner/overlay or
      // mid-animation. Each check falls back to the native click (which has its own actionability
      // waits), so this only improves — it never regresses.
      try {
        const timeout = options.timeout ?? 30000;
        const loc = page.locator(selector).first();
        await loc.waitFor({ state: "visible", timeout });
        await loc.scrollIntoViewIfNeeded({ timeout });
        if (!(await loc.isEnabled())) return orig(selector, options);
        let box = await loc.boundingBox();
        if (!box) return orig(selector, options);
        // stability: a box that's still moving = an animation in flight -> let PW settle it
        await new Promise((r) => setTimeout(r, 50));
        const box2 = await loc.boundingBox();
        if (!box2 || Math.abs(box2.x - box.x) > 1 || Math.abs(box2.y - box.y) > 1) {
          return orig(selector, options);
        }
        box = box2;
        const x = box.x + box.width * rand(0.3, 0.7);
        const y = box.y + box.height * rand(0.3, 0.7);
        // covered-by: don't fire a trusted click at a point some overlay owns. The callback runs
        // in the browser; this is a Node tsconfig (no DOM lib), so reach document via globalThis.
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
        } catch {
          /* covered-by check best-effort */
        }
        if (await humanizedMove(x, y, noClick)) return;
        return orig(selector, options);
      } catch {
        return orig(selector, options);
      }
    };
  };
  wrapTargeted("click", false);
  wrapTargeted("hover", true);
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
