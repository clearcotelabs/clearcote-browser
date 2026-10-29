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

/** @internal The JSON value of a Runtime.evaluate / callFunctionOn answer, or undefined. */
export const resultValue = <T>(r: any): T | undefined => {
  if (!r || typeof r !== "object" || r.exceptionDetails) return undefined;
  return r.result?.value as T;
};

/** @internal exported for tests. DOM.getNodeForLocation hit-tests in DOCUMENT coordinates (the
 * viewport point plus the scroll offset); Playwright's boxes, CDP's box model and the mouse are all
 * VIEWPORT coordinates. Measured on r37: a field scrolled 547 px down answered "No node found" at its
 * viewport centre and the input itself at centre + scroll.
 *
 * No ignorePointerEventsNone: in CDP `true` means "hit-test pointer-events:none elements TOO", so a
 * decorative layer with pointer-events:none over a control was returned instead of the control and
 * every form route fell back to stock Playwright (SC 209: 6/6 untrusted change events). The default
 * (false) skips such layers, exactly as a real press does. */
export function nodeAtParams(x: number, y: number, scroll?: readonly [number, number] | null): Record<string, unknown> {
  const [sx, sy] = scroll ?? [0, 0];
  return { x: roundHalfEven(x + sx), y: roundHalfEven(y + sy), includeUserAgentShadowDOM: false };
}

/** @internal exported for tests. The frame directly under `main` that contains `frameId` (itself for a
 * direct child), from a Page.getFrameTree answer; null when it is not below the main frame. */
export function topChildFrame(tree: any, frameId: string, main: string): string | null {
  const parents = new Map<string, string | null>();
  const walk = (t: any, parent: string | null) => {
    const id = t?.frame?.id;
    if (!id) return;
    parents.set(id, parent);
    for (const c of t.childFrames ?? []) walk(c, id);
  };
  walk(tree, null);
  let fid = frameId;
  for (let n = 0; n < 64; n++) {
    const up = parents.get(fid);
    if (up === undefined || up === null) return null;
    if (up === main) return fid;
    fid = up;
  }
  return null;
}

/** Python's round(): half to even (60.5 -> 60), so the hit test picks the same pixel in every SDK. */
/** @internal Python's round(): half to even (the pixel DOM.getNodeForLocation is asked about). */
export function roundHalfEven(v: number): number {
  const f = Math.floor(v), d = v - f;
  if (d > 0.5) return f + 1;
  if (d < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

/** One isolated world in the page's main frame, plus the frame id it lives in. */
type WorldCtx = { id: number; frame: string };

export class IsolatedWorld {
  // Promises, not values: two humanized calls racing on one page (Promise.all of two fills) must share
  // ONE CDP session and ONE isolated world. With plain fields both saw "none yet" across the await and
  // each created its own.
  private cdp: Promise<CdpLike> | null = null;
  private ctx: Promise<WorldCtx> | null = null;
  private frame: string | null = null;

  constructor(private readonly page: any) {}

  private session(): Promise<CdpLike> {
    if (!this.cdp) {
      const p: Promise<CdpLike> = Promise.resolve().then(() => this.page.context().newCDPSession(this.page));
      this.cdp = p;
      p.catch(() => { if (this.cdp === p) this.cdp = null; }); // a failed attach may be retried later
    }
    return this.cdp;
  }

  private async ensure(): Promise<{ cdp: CdpLike; world: Promise<WorldCtx> }> {
    const cdp = await this.session();
    if (!this.ctx) {
      const p: Promise<WorldCtx> = (async () => {
        const tree = await cdp.send("Page.getFrameTree");
        const frame = tree.frameTree.frame.id as string;
        const created = await cdp.send("Page.createIsolatedWorld", { frameId: frame });
        this.frame = frame;
        return { id: created.executionContextId as number, frame };
      })();
      this.ctx = p;
      p.catch(() => this.drop(p));
    }
    const world = this.ctx;
    await world;
    return { cdp, world };
  }

  /** Forget `world` (a navigation destroyed it) -- only if no other call has already replaced it. */
  private drop(world: Promise<WorldCtx> | null | undefined): void {
    if (world && this.ctx === world) this.ctx = null;
  }

  /** Run `(fnSource)(arg)` (`arg` JSON-serialisable) and resolve to its JSON value, or undefined.
   *  `awaitPromise`: a function that returns a promise answers with what the promise resolves to. */
  async evaluate<T = unknown>(fnSource: string, arg?: unknown, awaitPromise = false): Promise<T | undefined> {
    for (let attempt = 0; attempt < 2; attempt++) { // a stale context (navigation) is re-created once
      let world: Promise<WorldCtx> | undefined;
      try {
        const e = await this.ensure();
        world = e.world;
        return resultValue<T>(await e.cdp.send("Runtime.evaluate", {
          expression: `(${fnSource})(${JSON.stringify(arg ?? null)})`, contextId: (await world).id, returnByValue: true,
          ...(awaitPromise ? { awaitPromise: true } : {}),
        }));
      } catch {
        this.drop(world);
      }
    }
    return undefined;
  }

  /** A raw CDP call on this page's session (DOM reads that run no script), or undefined. */
  async send<T = any>(method: string, params?: Record<string, unknown>): Promise<T | undefined> {
    try {
      const cdp = await this.session();
      return (await cdp.send(method, params ?? {})) as T;
    } catch {
      return undefined;
    }
  }

  /** The main frame's id (the world is made on first use), or undefined without CDP. */
  async mainFrameId(): Promise<string | undefined> {
    try {
      const { world } = await this.ensure();
      return (await world).frame;
    } catch {
      return undefined;
    }
  }

  /** The main frame's [scrollX, scrollY], read in the isolated world; [0, 0] if unknown. */
  async scroll(): Promise<[number, number]> {
    const s = await this.evaluate<unknown>(ISO_SCROLL);
    return Array.isArray(s) && s.length === 2 ? [Number(s[0]), Number(s[1])] : [0, 0];
  }

  /** backendNodeId of the MAIN-FRAME element at viewport point (x, y) -- for a point inside a child
   * frame, the <iframe> element that holds it --, or undefined. `scroll`: the page's [scrollX, scrollY]
   * if the caller already has it (read here otherwise).
   *
   * CDP's hit test (DOM.getNodeForLocation) runs in the renderer with no script at all, so this is how
   * humanize finds the element a Playwright locator means -- getByRole, getByLabel, `>> nth=`,
   * .filter() -- without document.querySelector, which cannot parse those selectors (the old helpers
   * threw on them and silently dropped to stock Playwright input). */
  async nodeAt(x: number, y: number, scroll?: readonly [number, number] | null): Promise<number | undefined> {
    try {
      await this.ensure();
    } catch {
      return undefined;
    }
    const hit = await this.send("DOM.getNodeForLocation", nodeAtParams(x, y, scroll ?? await this.scroll()));
    if (!hit || hit.backendNodeId === undefined || hit.backendNodeId === null) return undefined;
    if (hit.frameId && this.frame && hit.frameId !== this.frame) return this.ownerInMain(hit.frameId);
    return hit.backendNodeId as number;
  }

  /** A point inside a child frame: the <iframe> element in the MAIN document that holds it (through
   * every nesting level), or undefined. To the main document a press there lands on that element: a
   * control the iframe covers is covered, a target that is (or contains) the iframe is reached. This
   * replaced the only Playwright trial humanize still ran (it moved the pointer and pressed, and page
   * listeners registered first saw it). */
  private async ownerInMain(frameId: string): Promise<number | undefined> {
    const tree = await this.send("Page.getFrameTree");
    const top = this.frame ? topChildFrame(tree?.frameTree, frameId, this.frame) : null;
    if (!top) return undefined;
    const own = await this.send("DOM.getFrameOwner", { frameId: top });
    return own && typeof own.backendNodeId === "number" ? own.backendNodeId : undefined;
  }

  /** Run `fnSource` -- a `function (arg) { ... }` whose `this` is the node -- in the isolated world and
   * resolve to its JSON value, or undefined. */
  async callOnNode<T = unknown>(backendNodeId: number, fnSource: string, arg?: unknown): Promise<T | undefined> {
    for (let attempt = 0; attempt < 2; attempt++) {
      let world: Promise<WorldCtx> | undefined;
      try {
        const e = await this.ensure();
        world = e.world;
        const obj = (await e.cdp.send("DOM.resolveNode", { backendNodeId, executionContextId: (await world).id })).object;
        try {
          return resultValue<T>(await e.cdp.send("Runtime.callFunctionOn", {
            objectId: obj.objectId, functionDeclaration: fnSource, arguments: [{ value: arg ?? null }],
            returnByValue: true,
          }));
        } finally {
          try { await e.cdp.send("Runtime.releaseObject", { objectId: obj.objectId }); } catch { /* best effort */ }
        }
      } catch (err) {
        // A stale world (a navigation destroyed it) is re-created once. A node that is simply gone
        // ("No node with given id") says nothing about the world, which stays.
        if (world && !/context/i.test(String((err as any)?.message ?? err))) return undefined;
        this.drop(world);
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

/** How long a covered click point is re-checked (an overlay fading out) before the native path takes over. */
export const COVER_CHECK_MS = 400;
/** How long a click waits for a target that is still moving (a scroll settling, a layout shift) to stop
 *  before it re-aims; and how many times it re-aims before the native path takes over. */
export const SETTLE_MS = 1500;
export const SETTLE_TRIES = 3;
/** What covers viewport point (a.x, a.y) over the element filling box a.b: the union of the rects from the
 *  topmost element up to (not including) the first ancestor that holds the target, and whether any of
 *  them is fixed or sticky. null when nothing covers it. Read in the isolated world: the page sees nothing. */
export const ISO_COVER = `(a) => {
  const same = r => Math.abs(r.x - a.b.x) <= 1.5 && Math.abs(r.y - a.b.y) <= 1.5 &&
    Math.abs(r.width - a.b.width) <= 1.5 && Math.abs(r.height - a.b.height) <= 1.5;
  const stack = document.elementsFromPoint(a.x, a.y);
  if (!stack.length) return null;
  const tgt = stack.find(e => same(e.getBoundingClientRect())) || null;
  if (tgt && (stack[0] === tgt || tgt.contains(stack[0]))) return null;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, fixed = false;
  for (let e = stack[0], n = 0; e && e !== document.body && e !== document.documentElement && n < 64; e = e.parentElement, n++) {
    if (tgt && e.contains(tgt)) break;
    const r = e.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      x0 = Math.min(x0, r.x); y0 = Math.min(y0, r.y); x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom);
    }
    const p = getComputedStyle(e).position;
    if (p === 'fixed' || p === 'sticky') fixed = true;
  }
  return isFinite(x0) ? { rect: [x0, y0, x1 - x0, y1 - y0], fixed } : null;
}`;
/** The timeout of the utility-world value reads that prove a fill landed. (It was also the one longer
 *  look of the Playwright trial that checked a point inside a child frame; no Playwright trial runs in
 *  humanized mode any more: such a point is answered by its <iframe> element, see nodeAt.) */
export const COVER_RETRY_MS = 2000;

// The reads humanize makes, as functions of one JSON argument (run by IsolatedWorld.evaluate).
export const ISO_VIEWPORT = "() => [innerWidth, innerHeight]";
// Resolves once the document has drawn two animation frames (at once after the first time; false after
// `ms` without them). The engine pointer path walks in the browser without waiting for the page, and a
// fresh headless browser's first page drew one or two frames, then stalled until something asked for one:
// the walk sent meanwhile was lost (SC 216: no press on 2 of 8 first clicks, 1-9 moves on the others).
export const ISO_FRAMES = `(ms) => window.__ccFramed ? true : new Promise((done) => {
  const t = setTimeout(() => { window.__ccFramed = 1; done(false); }, ms);
  requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(t); window.__ccFramed = 1; done(true); }));
})`;
// The platform the page believes (the persona's), for the wheel notch size.
export const ISO_PLATFORM = "() => { const d = navigator.userAgentData; return (d && d.platform) || navigator.platform || ''; }";
// The main frame's scroll offset: DOM.getNodeForLocation wants document coordinates.
export const ISO_SCROLL = "() => [scrollX, scrollY]";

// The value of whatever has focus (input/textarea value, or a contenteditable's text): how the typing
// path checks that a fat-finger key actually landed before it deletes anything.
export const ISO_ACTIVE_VALUE = `() => { let e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e || e.tagName === 'IFRAME' || e.tagName === 'FRAME') return null;
  if (typeof e.value === 'string' && e.tagName !== 'BUTTON') return e.value;
  return e.isContentEditable ? e.textContent : null; }`;
// What the typo gate needs about the focused field (type / inputmode / autocomplete), or null.
// Focus inside an open shadow root is followed to the real field; focus inside a frame cannot be read
// from here, so it reports {frame: true} and the gate fails CLOSED (no slips in a card or login form
// that lives in an iframe).
export const ISO_ACTIVE_FIELD = `() => { let e = document.activeElement; if (!e || e === document.body) return null;
  while (e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (e.tagName === 'IFRAME' || e.tagName === 'FRAME') return { frame: true };
  return { type: e.tagName === 'INPUT' ? e.type : '', inputmode: e.getAttribute('inputmode'),
           autocomplete: e.getAttribute('autocomplete') }; }`;
// Everything the form routes need about ONE element, read from the node the hit test found (a
// CDP callFunctionOn target: `this` is that node). `want` walks up to the control the locator means
// (a click on a <select>'s padding hits the select; one on a label's text does not, and then the
// caller tries another point). Returns null when no such ancestor exists.
export const ISO_DESCRIBE = `function (a) {
  let e = this.nodeType === 1 ? this : this.parentElement;
  if (!e) return null;
  if (a && a.want) { const c = e.closest(a.want); if (!c) return null; e = c; }
  const r = e.getBoundingClientRect(), cs = getComputedStyle(e), tag = e.tagName.toLowerCase();
  const o = { tag, type: tag === 'input' ? e.type : '', ce: !!e.isContentEditable,
    rect: [r.x, r.y, r.width, r.height], focused: e === document.activeElement,
    value: typeof e.value === 'string' ? e.value : (e.isContentEditable ? e.textContent : null),
    min: e.getAttribute('min'), max: e.getAttribute('max'), step: e.getAttribute('step'),
    maxlength: typeof e.maxLength === 'number' ? e.maxLength : -1,
    inputmode: e.getAttribute('inputmode'), autocomplete: e.getAttribute('autocomplete'),
    readonly: !!e.readOnly, disabled: !!e.disabled, dir: cs.direction, wm: cs.writingMode,
    bl: e.clientLeft || 0, bt: e.clientTop || 0 };
  if (tag === 'select') {
    o.multiple = e.multiple; o.size = e.size; o.selectedIndex = e.selectedIndex;
    o.options = Array.from(e.options, x => ({ value: x.value, label: x.label,
      disabled: x.disabled || !!(x.parentElement && x.parentElement.tagName === 'OPTGROUP' && x.parentElement.disabled),
      selected: x.selected }));
    if (e.multiple || e.size > 1)
      o.optRects = Array.from(e.options, x => { const q = x.getBoundingClientRect(); return [q.x, q.y, q.width, q.height]; });
  }
  return o; }`;
// The controls a locator can mean for the form routes.
export const ISO_FORM_CONTROL = "input, textarea, select, [contenteditable=''], [contenteditable='true'], [contenteditable='plaintext-only']";
// Does a press at a click point reach the element the caller holds? `this` = the node the browser's
// hit test found there (DOM.getNodeForLocation, pointer-events:none skipped, as for a real press; for a
// point inside a child frame, the <iframe> element that holds it); `b` = the element's box, plus `sel`
// when the caller has a plain-CSS selector for it.
//
// With `sel` (its first match, which must still fill `b`), the answer is exact, as Playwright's own
// hit-target check decides it: the hit is the element or inside it (across open shadow roots), or inside
// a <label> whose control it is (the label passes the press on). Anything else on top -- an overlay
// with exactly the element's box, a badge -- covers it.
//
// Without one, by the box: the hit element itself, or an ancestor that has exactly that box, with no
// OTHER element of that same box beside the hit's path inside it that the press would miss. That twin
// is a wrapper's control under an overlay (<span class=wrap><input><span overlay></span></span>: the
// wrap has the input's box, the press lands on the overlay). A twin the press reaches anyway, or that no
// press could be meant for, does not count (SC 210 fix 2; counting every twin made a MUI-style button
// "covered" and sent its click to Playwright): one that takes no pointer events (pointer-events:none,
// not visibility:visible -- a ripple or focus-ring span, an icon layer), a control whose <label> holds
// the hit (a custom checkbox), and, inside an element that is itself a control, a decorative twin with
// no control in it (a background span under a button's text).
//
// The covered-point check without a Playwright trial: a trial MOVES the real pointer onto the element
// and back and performs a press whose events Playwright blocks, so a page saw a mousemove inside the
// target, a jump back and a pointerdown/click before every humanized click (verifier B).
export const ISO_HIT_IN_BOX = `function (b) {
  const same = r => Math.abs(r.x - b.x) <= 1.5 && Math.abs(r.y - b.y) <= 1.5 &&
    Math.abs(r.width - b.width) <= 1.5 && Math.abs(r.height - b.height) <= 1.5;
  const up = n => { const p = n.parentNode; return n.parentElement || (p && p.nodeType === 11 && p.host) || null; };
  const h = this.nodeType === 1 ? this : this.parentElement;
  let t = null;
  if (b.sel) { try { t = document.querySelector(b.sel); } catch (x) { t = null; } }
  if (t && same(t.getBoundingClientRect())) {
    for (let e = h, n = 0; e && n < 128; e = up(e), n++)
      if (e === t || (e.tagName === 'LABEL' && e.control === t)) return true;
    return false;
  }
  const CTL = 'a[href], area[href], button, input, select, textarea, label, summary, iframe, [contenteditable], ' +
    '[tabindex], [role=button], [role=link], [role=checkbox], [role=radio], [role=switch], [role=tab], ' +
    '[role=menuitem], [role=option], [role=combobox], [role=textbox]';
  const takes = d => { const s = getComputedStyle(d); return s.pointerEvents !== 'none' && s.visibility === 'visible'; };
  const path = new Set();
  for (let e = h, n = 0; e && n < 128; e = up(e), n++) {
    path.add(e);
    if (same(e.getBoundingClientRect())) {
      const all = e.getElementsByTagName('*'), ctl = e.matches(CTL);
      if (all.length <= 4000)
        for (const d of all) {
          if (path.has(d) || !same(d.getBoundingClientRect()) || !takes(d)) continue;
          if (d.labels && Array.prototype.some.call(d.labels, l => l.contains(h))) continue;
          if (ctl && !d.matches(CTL) && !d.querySelector(CTL)) continue;
          return false;
        }
      return true;
    }
  }
  return false; }`;
// A list box's scroll state and one row's box, from any node inside the <select> (`this`): where its
// visible rows are (top / bottom of the client area), how far it is scrolled (st, of max), the row's
// rect, and which rows are selected.
// A checkbox / radio and its <label>, from the input node (`this`): mirrors _isolated.py LABEL_OF (is the input
// itself clickable -- `hidden` covers every "visually hidden" recipe a custom checkbox uses --, and the label the
// browser associates with it, with the label's box and the box of its text).
export const ISO_LABEL_OF = `function () {
  const e = this.nodeType === 1 ? this : this.parentElement;
  if (!e || e.tagName !== 'INPUT' || (e.type !== 'checkbox' && e.type !== 'radio')) return null;
  const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
  const clip = /rect\\(\\s*0(px)?[ ,]+0(px)?[ ,]+0(px)?[ ,]+0(px)?\\s*\\)/.test(cs.clip || '');
  const hidden = cs.display === 'none' || cs.visibility !== 'visible' || parseFloat(cs.opacity) < 0.05 ||
    r.width <= 1 || r.height <= 1 || clip || r.right < 0 || r.bottom < 0 ||
    r.left > innerWidth + 1 || r.top > innerHeight + 1;
  const o = { tag: 'input', type: e.type, rect: [r.x, r.y, r.width, r.height], hidden,
    checked: !!e.checked, disabled: !!e.disabled, label: null };
  const lab = (e.labels && e.labels.length) ? e.labels[0] : e.closest('label');
  if (!lab) return o;
  const lr = lab.getBoundingClientRect(), ls = getComputedStyle(lab);
  if (ls.display === 'none' || ls.visibility !== 'visible' || lr.width < 2 || lr.height < 2) return o;
  // The TEXT's box: the union of the label's content rects clear of the control's own box. A label that WRAPS its
  // control lists the control's border box first (SC 239 box probe: text == the input's rect), and a click there is an
  // input click, not a label click. Text wrapped to a line under the control: the first clear rect alone.
  const wraps = lab.contains(e);
  const over = q => wraps && q.left < r.right - 0.5 && q.right > r.left + 0.5 && q.top < r.bottom - 0.5 && q.bottom > r.top + 0.5;
  let text = null;
  try {
    const rg = document.createRange(); rg.selectNodeContents(lab);
    const clear = Array.from(rg.getClientRects()).filter(q => q.width >= 2 && q.height >= 2 && !over(q));
    if (clear.length) {
      const u = { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity };
      for (const q of clear) {
        u.left = Math.min(u.left, q.left); u.top = Math.min(u.top, q.top);
        u.right = Math.max(u.right, q.right); u.bottom = Math.max(u.bottom, q.bottom);
      }
      const t = over(u) ? clear[0] : u;
      text = [t.left, t.top, t.right - t.left, t.bottom - t.top];
    }
  } catch (x) { text = null; }
  o.label = { rect: [lr.x, lr.y, lr.width, lr.height], text, contains: wraps };
  return o; }`;
// ISO_LABEL_OF for the first match of a plain-CSS selector, plus how many match; null when it is not CSS.
export const ISO_LABEL_BY_SEL = `(a) => { let l;
  try { l = document.querySelectorAll(a.sel); } catch (e) { return null; }
  if (!l.length) return { n: 0 };
  const o = (${ISO_LABEL_OF}).call(l[0]);
  if (!o) return { n: l.length };
  o.n = l.length; return o; }`;
export const ISO_LISTBOX_ROW = `function (a) {
  const n = this.nodeType === 1 ? this : this.parentElement;
  const e = n && n.closest('select');
  if (!e || !e.options[a.i]) return null;
  const r = e.getBoundingClientRect(), q = e.options[a.i].getBoundingClientRect();
  return { box: [r.x, r.y, r.width, r.height], row: [q.x, q.y, q.width, q.height],
    top: r.y + e.clientTop, bottom: r.y + e.clientTop + e.clientHeight,
    st: e.scrollTop, max: e.scrollHeight - e.clientHeight,
    sel: Array.from(e.options, x => x.selected) }; }`;
// The first match of a plain-CSS selector, by the browser's own querySelectorAll in this world (the
// page's hooks on its prototypes never see it): how many match, and the first one's box -- scrolled
// into view first when `scroll` is set (centred, only if needed: what Playwright's
// scrollIntoViewIfNeeded does). null when the selector is not CSS the browser accepts.
export const ISO_CSS_BOX = `(a) => { let l;
  try { l = document.querySelectorAll(a.sel); } catch (e) { return null; }
  if (!l.length) return { n: 0 };
  const e = l[0];
  if (a.scroll) { if (e.scrollIntoViewIfNeeded) e.scrollIntoViewIfNeeded(true);
                  else e.scrollIntoView({ block: 'center', inline: 'center' }); }
  const r = e.getBoundingClientRect();
  return { n: l.length, rect: [r.x, r.y, r.width, r.height] }; }`;
// Scroll the window by [dx, dy] at once (no smooth-scroll animation to wait out) and return how far
// it actually moved: the box humanize already holds moves by exactly that much.
export const ISO_SCROLL_BY = `(d) => { const x = scrollX, y = scrollY;
  scrollBy({ left: d[0], top: d[1], behavior: 'instant' });
  return [scrollX - x, scrollY - y]; }`;

/** What ISO_DESCRIBE returns for one element (plus `node`, the backendNodeId it was read from). */
export interface DescribedElement {
  tag: string;
  type: string;
  ce: boolean;
  rect: [number, number, number, number];
  focused: boolean;
  value: string | null;
  min: string | null;
  max: string | null;
  step: string | null;
  maxlength: number;
  inputmode: string | null;
  autocomplete: string | null;
  readonly: boolean;
  disabled: boolean;
  dir: string;
  wm: string;
  bl: number;
  bt: number;
  multiple?: boolean;
  size?: number;
  selectedIndex?: number;
  options?: { value: string; label: string; disabled: boolean; selected: boolean }[];
  optRects?: [number, number, number, number][];
  node?: number;
  /** The probe point (fractions of the box) that proved the control; set by humanize. */
  probe?: [number, number];
}
