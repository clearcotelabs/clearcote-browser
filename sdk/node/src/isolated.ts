/**
 * Evaluate humanize's DOM reads in an isolated JavaScript world, not the page's own.
 *
 * `page.evaluate` runs in the page's main world: every DOM call it makes goes through the page's
 * prototypes, so a page that wraps `Document.prototype.querySelector` or `elementFromPoint` sees each
 * read, with a Playwright `UtilityScript.evaluate` frame on its stack (measured on r28 with humanize
 * on: 3 hook hits per session; 0 with humanize off and on genuine Chrome).
 *
 * An isolated world (CDP `Page.createIsolatedWorld`, the mechanism extensions' content scripts use)
 * shares the DOM but has its own JavaScript globals and prototypes, so the page cannot observe what
 * runs there. The world is created lazily per page and again after a navigation destroys it.
 *
 * `evaluate()` resolves to `undefined` when the read is impossible (no CDP session, page closed, a
 * navigation mid-call). Callers fall back to their safe default -- never to the page world.
 *
 * Mirrors sdk/python/clearcote/_isolated.py and sdk/dotnet/src/Clearcote/IsolatedWorld.cs.
 */

type CdpLike = { send(method: string, params?: Record<string, unknown>): Promise<any> };

export class IsolatedWorld {
  private cdp: CdpLike | null = null;
  private ctx: number | null = null;

  constructor(private readonly page: any) {}

  /** Run `(fnSource)(arg)` (`arg` JSON-serialisable) and resolve to its JSON value, or undefined. */
  async evaluate<T = unknown>(fnSource: string, arg?: unknown): Promise<T | undefined> {
    for (let attempt = 0; attempt < 2; attempt++) { // a stale context (navigation) is re-created once
      try {
        if (!this.cdp) this.cdp = await this.page.context().newCDPSession(this.page);
        const cdp = this.cdp as CdpLike;
        if (this.ctx === null) {
          const tree = await cdp.send("Page.getFrameTree");
          const created = await cdp.send("Page.createIsolatedWorld", { frameId: tree.frameTree.frame.id });
          this.ctx = created.executionContextId as number;
        }
        const r = await cdp.send("Runtime.evaluate", {
          expression: `(${fnSource})(${JSON.stringify(arg ?? null)})`, contextId: this.ctx, returnByValue: true,
        });
        if (!r || r.exceptionDetails) return undefined;
        return r.result?.value as T;
      } catch {
        this.ctx = null;
      }
    }
    return undefined;
  }
}

const worlds = new WeakMap<object, IsolatedWorld>();

/** The page's IsolatedWorld (created on first use). */
export function worldFor(page: object): IsolatedWorld {
  let w = worlds.get(page);
  if (!w) { w = new IsolatedWorld(page); worlds.set(page, w); }
  return w;
}

/** How long a trial action may look for a covered click point before the native path takes over. */
export const COVER_CHECK_MS = 400;

// The reads humanize makes, as functions of one JSON argument (run by IsolatedWorld.evaluate).
export const ISO_VIEWPORT = "() => [innerWidth, innerHeight]";
// The platform the page believes (the persona's), for the wheel notch size.
export const ISO_PLATFORM = "() => { const d = navigator.userAgentData; return (d && d.platform) || navigator.platform || ''; }";
export const ISO_IS_FOCUSED =
  "(s) => { const e = document.querySelector(s); return !!e && e === document.activeElement; }";
export const ISO_SELECT_PLAN = `(a) => { const s = document.querySelector(a.sel);
     if (!s || s.multiple || s.disabled) return null;
     const os = [...s.options];
     let i = -1;
     if (a.by === 'index') i = (a.want >= 0 && a.want < os.length) ? a.want : -1;
     else if (a.by === 'label') i = os.findIndex(o => (o.label || o.textContent || '').trim() === String(a.want).trim());
     else i = os.findIndex(o => o.value === a.want);
     if (i < 0 || os[i].disabled) return null;
     return { to: i, from: s.selectedIndex, ret: os[i].value }; }`;
export const ISO_SELECTED_INDEX = "(s) => { const e = document.querySelector(s); return e ? e.selectedIndex : -1; }";
