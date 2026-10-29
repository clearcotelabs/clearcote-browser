/**
 * Humanized input for elements inside child frames (FrameLocator / Frame actions). Mirrors
 * sdk/python/clearcote/_frames.py; see there for the reasoning.
 *
 * A locator that enters an iframe used to go straight to Playwright's own action: an untrusted `change`
 * for a select (verifier B #4), a ~0 ms click, keys with no dwell. The pointer and keyboard are the
 * PAGE's, so the route only has to find -- and prove with the browser's own hit test, read in the child
 * frame's isolated world -- the viewport point a press inside the frame lands on, then drive the
 * humanized page.mouse / page.keyboard. A frame whose nodes the page's CDP session cannot see (a
 * cross-origin frame running out of process) is answered by its <iframe>: the action goes to Playwright
 * and the fallback is reported (lane A's hook when present, else page._clearcoteLastFallback).
 */
import type { Locator, Page } from "playwright-core";
import { optionsOf, reportFallback } from "./hzoptions.js";
import { classifyField, formattedEqual, planSelect, type OptionInfo } from "./forms.js";
import {
  ISO_DESCRIBE, ISO_FORM_CONTROL, ISO_HIT_IN_BOX, ISO_SCROLL_BY, ISO_VIEWPORT, nodeAtParams, resultValue, worldFor,
  type DescribedElement,
} from "./isolated.js";
import { clickPoint, type Box, type Persona, type Point } from "./motion.js";

/** What a route returns when it does not apply: the caller runs Playwright's own method. */
export const NATIVE: unique symbol = Symbol("clearcote.frames.native");

export const OWNER_ORIGIN = `function () {
  const r = this.getBoundingClientRect(), cs = getComputedStyle(this);
  return [r.x + this.clientLeft + (parseFloat(cs.paddingLeft) || 0),
          r.y + this.clientTop + (parseFloat(cs.paddingTop) || 0)]; }`;
export const FOCUSED = `function () { const a = document.activeElement;
  return !!a && (a === this || this.contains(a) || (this.shadowRoot && this.shadowRoot.activeElement != null)); }`;
export const IS_FRAME = `function () { const e = this.nodeType === 1 ? this : this.parentElement;
  return !!e && (e.tagName === 'IFRAME' || e.tagName === 'FRAME'); }`;

const PROBES: readonly [number, number][] = [[0.5, 0.5], [0.82, 0.5], [0.18, 0.5], [0.5, 0.8], [0.5, 0.2]];
const CLICK_KEYS = new Set(["button", "clickCount", "delay", "position", "timeout", "noWaitAfter", "strict"]);
const ARROW_HOLD = 40;

class NativeSignal extends Error { constructor(readonly why: string) { super(why); } }
class NativeAfter extends Error {}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
const rand = (a: number, b: number) => a + Math.random() * (b - a);
const gauss = () => { let u = 0, v = 0; while (u === 0) u = Math.random(); while (v === 0) v = Math.random(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); };

/** Does `loc` reach into a child frame (a FrameLocator's locator, or a locator of a child Frame)? */
export function frameLocatorOf(loc: any): boolean {
  try {
    if (String(loc._selector ?? "").includes("internal:control=enter-frame")) return true;
    return loc._frame !== loc.page().mainFrame();
  } catch { return false; }
}

export const humanized = (page: any): boolean => page?._clearcoteHumanized === true;

/** Strict humanize mode on `page`: the page's resolved HumanizeOptions decide. */
export function strictMode(page: any): boolean {
  return optionsOf(page)?.strict === true;
}

/** Report a native fallback: lane A's reportFallback (strict throws ClearcoteStrictError, else onFallback hears
 * it; the event is kept as page._clearcoteLastFallback). Nothing is swallowed. */
export { reportFallback };

const selectorOf = (loc: any): string | undefined => { try { return loc._selector ?? undefined; } catch { return undefined; } };
const shift = (b: Box, o: [number, number]): Box => ({ x: b.x - o[0], y: b.y - o[1], width: b.width, height: b.height });
const same = (r: readonly number[], b: Box) =>
  Math.abs(r[0] - b.x) <= 1.5 && Math.abs(r[1] - b.y) <= 1.5 && Math.abs(r[2] - b.width) <= 1.5 && Math.abs(r[3] - b.height) <= 1.5;

function parentsOf(tree: any, out: Record<string, string | null> = {}, parent: string | null = null): Record<string, string | null> {
  const id = tree?.frame?.id;
  if (id) out[id] = parent;
  for (const c of tree?.childFrames ?? []) parentsOf(c, out, id ?? null);
  return out;
}

/** Isolated worlds for every frame of one page, on the page's CDP session, one per frameId. */
export class FrameWorlds {
  private ctx = new Map<string, number>();
  constructor(private readonly page: any) {}
  private get w() { return worldFor(this.page); }
  main(): Promise<string | undefined> { return this.w.mainFrameId(); }
  send<T = any>(m: string, p?: Record<string, unknown>): Promise<T | undefined> { return this.w.send<T>(m, p); }

  private async context(fid: string): Promise<number | undefined> {
    let c = this.ctx.get(fid);
    if (c === undefined) {
      const r = await this.send("Page.createIsolatedWorld", { frameId: fid });
      c = r?.executionContextId;
      if (c === undefined) return undefined;
      this.ctx.set(fid, c);
    }
    return c;
  }

  async call<T = unknown>(fid: string, nid: number, fn: string, arg?: unknown): Promise<T | undefined> {
    for (let i = 0; i < 2; i++) {
      const c = await this.context(fid);
      if (c === undefined) return undefined;
      const obj = await this.send("DOM.resolveNode", { backendNodeId: nid, executionContextId: c });
      if (!obj?.object) { this.ctx.delete(fid); continue; } // a stale world (the frame navigated)
      const oid = obj.object.objectId;
      try {
        return resultValue<T>(await this.send("Runtime.callFunctionOn", {
          objectId: oid, functionDeclaration: fn, arguments: [{ value: arg ?? null }], returnByValue: true,
        }));
      } finally {
        await this.send("Runtime.releaseObject", { objectId: oid });
      }
    }
    return undefined;
  }

  async hit(x: number, y: number): Promise<[number, string] | null> {
    const main = await this.main();
    if (!main) return null;
    const h = await this.send("DOM.getNodeForLocation", nodeAtParams(x, y, await this.w.scroll()));
    if (!h || h.backendNodeId === undefined) return null;
    return [h.backendNodeId, h.frameId ?? main];
  }

  /** Where frame `fid`'s viewport starts in the main viewport: each owning <iframe>'s content-box origin, summed. */
  async origin(fid: string): Promise<[number, number] | null> {
    const main = await this.main();
    const tree = await this.send("Page.getFrameTree");
    const parents = parentsOf(tree?.frameTree);
    let ox = 0, oy = 0, f = fid;
    for (let i = 0; i < 16; i++) {
      if (f === main) return [ox, oy];
      const up = parents[f];
      const own = await this.send("DOM.getFrameOwner", { frameId: f });
      if (!up || own?.backendNodeId === undefined) return null;
      const o = await this.call<number[]>(up, own.backendNodeId, OWNER_ORIGIN);
      if (!Array.isArray(o) || o.length !== 2) return null;
      ox += Number(o[0]); oy += Number(o[1]);
      f = up;
    }
    return null;
  }
}

export function frameWorldsFor(page: any): FrameWorlds {
  if (!page._ccFrameWorlds) page._ccFrameWorlds = new FrameWorlds(page);
  return page._ccFrameWorlds;
}

type Target = { fid: string; nid: number; box: Box; fbox: Box; info: DescribedElement | null; probe: [number, number] };

const inViewport = (b: Box | null, vp: number[]) =>
  !!b && b.x >= 0 && b.y >= 0 && b.x + b.width <= vp[0] && b.y + b.height <= vp[1];

/** `box` brought into the viewport: the WINDOW scrolled first; Playwright's scroll (which waits for "stable",
 * measured with animation frames inside the frame -- an out-of-process frame below the fold draws none in
 * headless) only when that was not enough. */
async function intoView(page: any, loc: Locator, box: Box | null, timeout?: number): Promise<Box | null> {
  if (!box) return box;
  const w = worldFor(page);
  const vp = await w.evaluate<number[]>(ISO_VIEWPORT);
  if (!Array.isArray(vp) || !vp[0] || !vp[1]) {
    await loc.scrollIntoViewIfNeeded({ timeout });
    return loc.boundingBox();
  }
  if (inViewport(box, vp)) return box;
  const dx = box.x < 0 || box.x + box.width > vp[0] ? box.x + box.width / 2 - vp[0] / 2 : 0;
  const dy = box.y < 0 || box.y + box.height > vp[1] ? box.y + box.height / 2 - vp[1] / 2 : 0;
  await w.evaluate(ISO_SCROLL_BY, [dx, dy]);
  const again = await loc.boundingBox();
  if (inViewport(again, vp)) return again;
  await loc.scrollIntoViewIfNeeded({ timeout });
  return loc.boundingBox();
}

async function locate(page: any, loc: Locator, timeout: number | undefined, want: string | null): Promise<Target> {
  await loc.waitFor({ state: "visible", timeout });
  const box = await intoView(page, loc, await loc.boundingBox(), timeout);
  if (!box || box.width <= 0 || box.height <= 0) throw new NativeSignal("frame: the element has no box");
  const fw = frameWorldsFor(page);
  const main = await fw.main();
  let owners = 0;
  for (const [fx, fy] of PROBES) {
    const h = await fw.hit(box.x + box.width * fx, box.y + box.height * fy);
    if (!h) throw new NativeSignal("frame: no CDP hit test here");
    const [nid, fid] = h;
    if ((loc as any)._ccMainFrame) {
      if (fid !== main) continue;   // an element handle of the main frame: only a main-frame node can be it
    } else if (fid === main) {
      if (await fw.call(main!, nid, IS_FRAME)) owners++;
      continue;
    }
    const o = await fw.origin(fid);
    if (!o) continue;
    const fbox = shift(box, o);
    if (want === null) {
      if ((await fw.call(fid, nid, ISO_HIT_IN_BOX, fbox)) === true) return { fid, nid, box, fbox, info: null, probe: [fx, fy] };
      continue;
    }
    const info = await fw.call<DescribedElement>(fid, nid, ISO_DESCRIBE, { want });
    if (info?.rect && same(info.rect, fbox)) return { fid, nid, box, fbox, info, probe: [fx, fy] };
  }
  if (owners) throw new NativeSignal("frame: cross-origin frame (its document runs in another process; the SDK holds no CDP session for it)");
  throw new NativeSignal("frame: no point of the element is proven to receive the press (covered)");
}

async function reaches(page: any, t: Target, x: number, y: number): Promise<boolean> {
  const fw = frameWorldsFor(page);
  const h = await fw.hit(x, y);
  if (!h || h[1] !== t.fid) return false;
  return (await fw.call(t.fid, h[0], ISO_HIT_IN_BOX, t.fbox)) === true;
}

async function point(page: any, t: Target, position?: { x: number; y: number }): Promise<[number, number]> {
  const b = t.box;
  if (position) return [Math.round(b.x + (t.info?.bl ?? 0) + position.x), Math.round(b.y + (t.info?.bt ?? 0) + position.y)];
  const persona: Persona | undefined = page._clearcotePersona;
  const pointer: (() => Point) | undefined = page._clearcotePointer;
  if (persona && pointer) {
    const p = clickPoint(b, pointer(), persona);
    const x = Math.round(p.x), y = Math.round(p.y);
    if (await reaches(page, t, x, y)) return [x, y];
  }
  return [Math.round(b.x + b.width * t.probe[0]), Math.round(b.y + b.height * t.probe[1])];
}

const focused = async (page: any, t: Target) => (await frameWorldsFor(page).call(t.fid, t.nid, FOCUSED)) === true;
const valueOf = async (page: any, t: Target) =>
  (await frameWorldsFor(page).call<DescribedElement>(t.fid, t.nid, ISO_DESCRIBE, { want: ISO_FORM_CONTROL }))?.value ?? null;

async function clickInto(page: Page, t: Target): Promise<void> {
  if (await focused(page, t)) return;
  const [x, y] = await point(page, t);
  await page.mouse.click(x, y);
  await sleep(rand(40, 120));
  if (!(await focused(page, t))) throw new NativeSignal("frame: the click did not put the caret in the field");
}

async function textTarget(page: any, loc: Locator, timeout?: number): Promise<Target> {
  const t = await locate(page, loc, timeout, ISO_FORM_CONTROL);
  const i = t.info!;
  if (classifyField(i.tag, i.type, i.ce) !== "text" || i.disabled || i.readonly) {
    throw new NativeSignal("frame: not a text field (Playwright's own call)");
  }
  return t;
}

async function doClick(page: Page, loc: Locator, kw: any, noClick: boolean, count?: number): Promise<void> {
  if (kw.force || kw.trial || (kw.modifiers && kw.modifiers.length)) {
    throw new NativeSignal("frame: force/trial/modifiers are Playwright's own inside frames");
  }
  const opts: any = {};
  if (!noClick) {
    const extra = Object.keys(kw).filter((k) => kw[k] !== undefined && kw[k] !== false && !CLICK_KEYS.has(k));
    if (extra.length) throw new NativeSignal(`frame: ${extra.sort().join(", ")} is not humanized inside frames`);
    for (const k of ["button", "clickCount", "delay"]) if (kw[k] !== undefined) opts[k] = kw[k];
  }
  if (count) opts.clickCount = count;
  const t = await locate(page, loc, kw.timeout, null);
  const [x, y] = await point(page, t, kw.position);
  if (noClick) await page.mouse.move(x, y); else await page.mouse.click(x, y, opts);
}

async function doFill(page: any, loc: Locator, value: string, kw: any): Promise<void> {
  if (typeof value !== "string" || kw.force) throw new NativeSignal("frame: fill(force / non-string) is Playwright's own");
  const t = await textTarget(page, loc, kw.timeout);
  if (t.info!.value === value) return;
  await clickInto(page, t);
  if (await valueOf(page, t)) {
    if (typeof page._clearcoteSelectAll === "function") await page._clearcoteSelectAll();
    else await page.keyboard.press("ControlOrMeta+a");
    await sleep(rand(60, 160));
    await page.keyboard.press("Backspace");
    await sleep(rand(60, 160));
    if (await valueOf(page, t)) throw new NativeSignal("frame: the field would not clear");
  }
  if (value) await page.keyboard.type(value);
  const got = await valueOf(page, t);
  if (got !== value && !formattedEqual(got, value)) throw new NativeAfter("frame: the typed value did not land; Playwright's fill corrected it");
}

async function doType(page: Page, loc: Locator, text: string, kw: any): Promise<void> {
  const t = await textTarget(page, loc, kw.timeout);
  await clickInto(page, t);
  await page.keyboard.type(text);
}

async function doPress(page: Page, loc: Locator, key: string, kw: any): Promise<void> {
  const t = await locate(page, loc, kw.timeout, null);
  const info = await frameWorldsFor(page).call<DescribedElement>(t.fid, t.nid, ISO_DESCRIBE, { want: ISO_FORM_CONTROL });
  if (info && classifyField(info.tag, info.type, info.ce) === "text" && same(info.rect, t.fbox)) {
    t.info = info;
    await clickInto(page, t);
  } else if (!(await focused(page, t))) {
    await loc.focus({ timeout: kw.timeout }); // a button / link: focused without a click, as Playwright does
    await sleep(rand(40, 120));
  }
  await page.keyboard.press(key, kw.delay ? { delay: kw.delay } : {});
}

async function doSelect(page: Page, loc: Locator, values: any, kw: any): Promise<string[]> {
  const { commitSelect, selectDeadline, selectWants, wantIndex } = await import("./humanize.js");
  if (kw.force) throw new NativeSignal("frame: force is Playwright's own");
  const wants = selectWants(values);
  if (!wants) throw new NativeSignal("frame: nothing to select by value/label/index");
  const dl = selectDeadline(kw, page);
  const t = await locate(page, loc, kw.timeout, "select");
  const info = t.info!;
  if (info.tag !== "select" || info.disabled) throw new NativeSignal("frame: not an enabled select");
  if (info.multiple || (info.size ?? 0) > 1) throw new NativeSignal("frame: list boxes inside frames are Playwright's own");
  const opts: readonly OptionInfo[] = info.options ?? [];
  const idx: number[] = [];
  for (const w of wants) {
    const i = wantIndex(opts, w);
    if (i < 0 || opts[i].disabled) throw new NativeSignal("frame: option missing or disabled (Playwright waits / reports it)");
    idx.push(i);
  }
  const tgt = Math.min(...idx);
  if (info.selectedIndex === tgt) return [opts[tgt].value ?? ""];
  const labels = opts.map((o) => (o.disabled ? null : (o.label || "")));
  const plan = planSelect(labels, info.selectedIndex ?? -1, tgt);
  if (plan === null || Math.abs(plan.arrows) > ARROW_HOLD) throw new NativeSignal("frame: no short key route to the option");
  const [x, y] = await point(page, t);
  await page.mouse.click(x, y);                 // opens the drop-down (the browser's own popup)
  await sleep(rand(220, 480));
  for (const ch of plan.keys) {
    await page.keyboard.press(ch);
    await sleep(Math.max(40, 100 + gauss() * 35)); // inside type-ahead's 1 s window
  }
  if (plan.arrows) {
    await sleep(rand(120, 300));
    for (let i = 0; i < Math.abs(plan.arrows); i++) {
      await page.keyboard.press(plan.arrows > 0 ? "ArrowDown" : "ArrowUp");
      await sleep(rand(60, 150));
    }
  }
  await sleep(rand(150, 400));
  // commits: ONE trusted input + change; a document that is navigating away cannot answer the read-back
  if (await commitSelect(page, selectorOf(loc) ?? "the select", dl, () => page.keyboard.press("Enter"), rand(80, 160))) {
    return [opts[tgt].value ?? ""];
  }
  const again = await frameWorldsFor(page).call<DescribedElement>(t.fid, t.nid, ISO_DESCRIBE, { want: "select" });
  if (again?.selectedIndex === tgt) return [opts[tgt].value ?? ""];
  throw new NativeAfter("frame: the drop-down did not commit the option");
}

async function doSetChecked(page: Page, loc: Locator, checked: boolean, kw: any): Promise<void> {
  if (kw.force || kw.trial) throw new NativeSignal("frame: force/trial are Playwright's own inside frames");
  if ((await loc.isChecked({ timeout: kw.timeout })) === checked) return;
  const t = await locate(page, loc, kw.timeout, "input");
  if (!["checkbox", "radio"].includes(t.info!.type) || t.info!.disabled) throw new NativeSignal("frame: not an enabled checkbox / radio");
  const [x, y] = await point(page, t, kw.position);
  await page.mouse.click(x, y);
  if ((await loc.isChecked()) === checked) return;
  const { sdkPlaywrightError } = await import("./humanize.js");
  throw sdkPlaywrightError("Clicking the checkbox did not change its state");
}

/**
 * An ElementHandle seen as the locator the routes above read (waitFor, boundingBox, scrollIntoViewIfNeeded,
 * focus, isChecked, page(), _frame). A main-frame handle is hit-tested against main-frame nodes only.
 */
export function handleAsLocator(handle: any, frame: any, page: any): any {
  return {
    _frame: frame,
    _selector: undefined,
    _ccMainFrame: frame === page.mainFrame(),
    page: () => page,
    waitFor: (o: any = {}) => handle.waitForElementState(o.state ?? "visible", { timeout: o.timeout }),
    boundingBox: () => handle.boundingBox(),
    scrollIntoViewIfNeeded: (o: any = {}) => handle.scrollIntoViewIfNeeded(o),
    focus: () => handle.focus(),
    isChecked: () => handle.isChecked(),
  };
}

/** Run `action` on frame locator `loc` through the humanized page input; NATIVE (after reporting why) for the
 * caller to run Playwright's own method. Playwright's errors (a timeout, strict mode) propagate. */
export async function frameRoute(loc: any, action: string, args: any[], kw: any = {}, apiPrefix = "locator"): Promise<any> {
  const page = loc.page();
  const api = apiPrefix + "." + action;
  kw = kw ?? {};
  try {
    switch (action) {
      case "click": return await doClick(page, loc, kw, false);
      case "dblclick": return await doClick(page, loc, kw, false, 2);
      case "hover": return await doClick(page, loc, kw, true);
      case "fill": return await doFill(page, loc, args[0], kw);
      case "clear": return await doFill(page, loc, "", kw);
      case "type": case "pressSequentially": return await doType(page, loc, args[0], kw);
      case "press": return await doPress(page, loc, args[0], kw);
      case "selectOption": return await doSelect(page, loc, args[0], kw);
      case "check": return await doSetChecked(page, loc, true, kw);
      case "uncheck": return await doSetChecked(page, loc, false, kw);
      case "setChecked": return await doSetChecked(page, loc, Boolean(args[0]), kw);
    }
  } catch (e: any) {
    if (e instanceof NativeSignal) { reportFallback(page, api, e.why, selectorOf(loc)); return NATIVE; }
    if (e instanceof NativeAfter) { reportFallback(page, api, e.message, selectorOf(loc)); return NATIVE; }
    // Playwright's own errors are the caller's; a TypeError is this route failing on an object it cannot read.
    if (e && (e.name === "TypeError" || e.name === "ReferenceError")) {
      reportFallback(page, api, `frame: ${e.name}: ${e.message}`, selectorOf(loc));
      return NATIVE;
    }
    throw e;
  }
  return NATIVE;
}
