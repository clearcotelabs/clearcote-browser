import { describe, it, expect } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import {
  IsolatedWorld, worldFor, nodeAtParams, ISO_ACTIVE_FIELD, ISO_ACTIVE_VALUE, ISO_DESCRIBE, ISO_FORM_CONTROL, ISO_PLATFORM,
  ISO_SCROLL, ISO_VIEWPORT, ISO_HIT_IN_BOX, ISO_LABEL_OF, ISO_LISTBOX_ROW, ISO_CSS_BOX, ISO_SCROLL_BY, ISO_MOVE_OFF_SAFE, topChildFrame,
} from "../src/isolated.js";

// A read that takes an argument (the CSS-only ISO_IS_FOCUSED this used went with the hit-test port).
const ISO_IS_FOCUSED = "(s) => { const e = document.querySelector(s); return !!e && e === document.activeElement; }";

// Humanize's DOM reads run in an isolated world, never the page's (mirrors
// sdk/python/tests/test_isolated.py).
class FakeCdp {
  calls: [string, any][] = [];
  nextCtx = 7;
  constructor(public values: unknown[] = [], public staleOnce = false, public throwOn = "") {}
  async send(method: string, params?: any): Promise<any> {
    this.calls.push([method, params]);
    if (method === this.throwOn) throw new Error("boom");
    if (method === "Page.getFrameTree") return { frameTree: { frame: { id: "MAIN" } } };
    if (method === "Page.createIsolatedWorld") return { executionContextId: ++this.nextCtx };
    if (method === "Runtime.evaluate") {
      if (this.staleOnce) { this.staleOnce = false; throw new Error("Cannot find context with specified id"); }
      return { result: { value: this.values.shift() } };
    }
    throw new Error(method);
  }
}

const fakePage = (cdp: FakeCdp) => {
  let sessions = 0;
  return {
    sessions: () => sessions,
    context: () => ({ newCDPSession: async () => { sessions++; return cdp; } }),
    evaluate: () => { throw new Error("humanize must not evaluate in the page's world"); },
  };
};

describe("IsolatedWorld", () => {
  it("evaluates in an isolated world of the main frame, without Runtime.enable", async () => {
    const cdp = new FakeCdp([[1280, 720], true]);
    const w = new IsolatedWorld(fakePage(cdp));
    expect(await w.evaluate(ISO_VIEWPORT)).toEqual([1280, 720]);
    expect(await w.evaluate(ISO_IS_FOCUSED, "#q")).toBe(true);
    expect(cdp.calls.map(([m]) => m)).toEqual(
      ["Page.getFrameTree", "Page.createIsolatedWorld", "Runtime.evaluate", "Runtime.evaluate"]);
    expect(cdp.calls[1][1]).toEqual({ frameId: "MAIN" });
    const ev = cdp.calls[3][1];
    expect(ev.contextId).toBe(8);
    expect(ev.returnByValue).toBe(true);
    expect(ev.expression).toBe(`(${ISO_IS_FOCUSED})(${JSON.stringify("#q")})`);
  });

  it("re-creates the world after a navigation", async () => {
    const cdp = new FakeCdp([[800, 600]], true);
    expect(await new IsolatedWorld(fakePage(cdp)).evaluate(ISO_VIEWPORT)).toEqual([800, 600]);
    expect(cdp.calls.filter(([m]) => m === "Page.createIsolatedWorld").length).toBe(2);
    expect(cdp.calls.at(-1)![1].contextId).toBe(9);
  });

  it("resolves undefined instead of touching the page world", async () => {
    const noCdp = { context: () => ({ newCDPSession: async () => { throw new Error("no cdp"); } }),
      evaluate: () => { throw new Error("must not fall back to page.evaluate"); } };
    expect(await new IsolatedWorld(noCdp).evaluate(ISO_VIEWPORT)).toBeUndefined();
    const scriptError = new FakeCdp();
    scriptError.send = async (m: string) =>
      m === "Runtime.evaluate" ? { exceptionDetails: { text: "SyntaxError" } }
        : m === "Page.getFrameTree" ? { frameTree: { frame: { id: "MAIN" } } } : { executionContextId: 1 };
    expect(await new IsolatedWorld(fakePage(scriptError)).evaluate(ISO_VIEWPORT)).toBeUndefined();
  });

  it("keeps one world (and one CDP session) per page", async () => {
    const page = fakePage(new FakeCdp([1, 2]));
    expect(worldFor(page)).toBe(worldFor(page));
    await worldFor(page).evaluate(ISO_VIEWPORT);
    await worldFor(page).evaluate(ISO_VIEWPORT);
    expect(page.sessions()).toBe(1);
  });
});

// ---------------------------------------------------------------- hit test + node calls (forms)
// Mirrors the node_at / call_on_node tests in sdk/python/tests/test_isolated.py.
class HitCdp extends FakeCdp {
  constructor(public hit: any = null, public callValue: unknown = null, values: unknown[] = []) { super(values); }
  async send(method: string, params?: any): Promise<any> {
    if (method === "DOM.getNodeForLocation") { this.calls.push([method, params]); return this.hit; }
    if (method === "DOM.resolveNode") { this.calls.push([method, params]); return { object: { objectId: "obj-1" } }; }
    if (method === "Runtime.callFunctionOn") { this.calls.push([method, params]); return { result: { value: this.callValue } }; }
    if (method === "Runtime.releaseObject") { this.calls.push([method, params]); return {}; }
    return super.send(method, params);
  }
}

describe("IsolatedWorld hit test and node calls", () => {
  it("nodeAt uses the renderer hit test and rounds to pixels", async () => {
    const cdp = new HitCdp({ backendNodeId: 42, frameId: "MAIN" });
    const w = new IsolatedWorld(fakePage(cdp));
    expect(await w.nodeAt(10.6, 20.4)).toBe(42);
    const hit = cdp.calls.find(([m]) => m === "DOM.getNodeForLocation")![1];
    expect(hit).toEqual({ x: 11, y: 20, includeUserAgentShadowDOM: false });
  });

  it("nodeAt skips pointer-events:none layers, as a real press does (no ignorePointerEventsNone)", async () => {
    // `true` makes CDP hit-test pointer-events:none elements TOO: a decorative layer over a control
    // answered instead of the control and every form route fell back to stock Playwright (SC 209).
    expect(nodeAtParams(5, 5).ignorePointerEventsNone ?? false).toBe(false);
  });

  it("nodeAt answers a point inside a child frame with the main document's <iframe> element", async () => {
    const tree = { frameTree: { frame: { id: "MAIN" }, childFrames: [
      { frame: { id: "C1", parentId: "MAIN" }, childFrames: [{ frame: { id: "C2", parentId: "C1" } }] }] } };
    class FrameCdp extends HitCdp {
      constructor(hitFrame: string, private owners: Record<string, number> = { C1: 501, C2: 777 }) {
        super({ backendNodeId: 42, frameId: hitFrame });
      }
      async send(method: string, params?: any): Promise<any> {
        if (method === "Page.getFrameTree") { this.calls.push([method, params]); return tree; }
        if (method === "DOM.getFrameOwner") {
          this.calls.push([method, params]);
          if (!(params.frameId in this.owners)) throw new Error("Frame with the given id was not found.");
          return { backendNodeId: this.owners[params.frameId] };
        }
        return super.send(method, params);
      }
    }
    const direct = new FrameCdp("C1");
    expect(await new IsolatedWorld(fakePage(direct)).nodeAt(1, 1)).toBe(501);
    const nested = new FrameCdp("C2");
    expect(await new IsolatedWorld(fakePage(nested)).nodeAt(1, 1)).toBe(501);   // C1's <iframe>, not C2's
    expect(nested.calls.filter(([m]) => m === "DOM.getFrameOwner").map(([, p]) => p)).toEqual([{ frameId: "C1" }]);
    expect(await new IsolatedWorld(fakePage(new FrameCdp("C1", {}))).nodeAt(1, 1)).toBeUndefined();
    expect(await new IsolatedWorld(fakePage(new FrameCdp("GONE"))).nodeAt(1, 1)).toBeUndefined();
    expect(topChildFrame(tree.frameTree, "C2", "MAIN")).toBe("C1");
    expect(topChildFrame(tree.frameTree, "MAIN", "MAIN")).toBeNull();
  });

  it("nodeAt hit-tests in DOCUMENT coordinates: the viewport point plus the scroll offset", async () => {
    // read here when the caller has no scroll yet ...
    const cdp = new HitCdp({ backendNodeId: 7, frameId: "MAIN" }, null, [[0, 547]]);
    const w = new IsolatedWorld(fakePage(cdp));
    expect(await w.nodeAt(100, 50)).toBe(7);
    expect(cdp.calls.find(([m]) => m === "Runtime.evaluate")![1].expression).toBe(`(${ISO_SCROLL})(null)`);
    expect(cdp.calls.find(([m]) => m === "DOM.getNodeForLocation")![1]).toMatchObject({ x: 100, y: 597 });
    // ... and taken as given when it does (no second read)
    const cdp2 = new HitCdp({ backendNodeId: 8, frameId: "MAIN" });
    expect(await new IsolatedWorld(fakePage(cdp2)).nodeAt(10, 10, [30, 200])).toBe(8);
    expect(cdp2.calls.filter(([m]) => m === "Runtime.evaluate")).toEqual([]);
    expect(cdp2.calls.find(([m]) => m === "DOM.getNodeForLocation")![1]).toMatchObject({ x: 40, y: 210 });
    expect(nodeAtParams(1.5, 2.5, [0.4, 0.4])).toMatchObject({ x: 2, y: 3 });
    // Python's round(): half to even, so all SDKs hit-test the same pixel
    expect(nodeAtParams(60.5, 61.5)).toMatchObject({ x: 60, y: 62 });
  });

  it("nodeAt is undefined for a frame it cannot place, and when nothing is there", async () => {
    expect(await new IsolatedWorld(fakePage(new HitCdp({ backendNodeId: 42, frameId: "IFRAME-7" }))).nodeAt(1, 1)).toBeUndefined();
    expect(await new IsolatedWorld(fakePage(new HitCdp({}))).nodeAt(1, 1)).toBeUndefined();
    expect(await new IsolatedWorld(fakePage(new HitCdp(null))).nodeAt(1, 1)).toBeUndefined();
  });

  it("scroll() reads [scrollX, scrollY] in the isolated world, [0, 0] when it cannot", async () => {
    expect(await new IsolatedWorld(fakePage(new FakeCdp([[12, 345]]))).scroll()).toEqual([12, 345]);
    expect(await new IsolatedWorld(fakePage(new FakeCdp([null]))).scroll()).toEqual([0, 0]);
  });

  it("callOnNode resolves into the isolated world and releases the handle", async () => {
    const cdp = new HitCdp(null, { tag: "select" });
    const w = new IsolatedWorld(fakePage(cdp));
    expect(await w.callOnNode(42, ISO_DESCRIBE, { want: "select" })).toEqual({ tag: "select" });
    const methods = cdp.calls.map(([m]) => m);
    const res = cdp.calls.find(([m]) => m === "DOM.resolveNode")![1];
    expect(res).toEqual({ backendNodeId: 42, executionContextId: 8 }); // the isolated world
    const call = cdp.calls.find(([m]) => m === "Runtime.callFunctionOn")![1];
    expect(call.objectId).toBe("obj-1");
    expect(call.functionDeclaration).toBe(ISO_DESCRIBE);
    expect(call.arguments).toEqual([{ value: { want: "select" } }]);
    expect(call.returnByValue).toBe(true);
    expect(methods.indexOf("Runtime.releaseObject")).toBeGreaterThan(methods.indexOf("Runtime.callFunctionOn"));
    expect(methods).not.toContain("Runtime.enable");
    expect(methods).not.toContain("DOM.enable");
  });

  it("concurrent calls share ONE CDP session and ONE isolated world", async () => {
    const cdp = new HitCdp({ backendNodeId: 3, frameId: "MAIN" }, { tag: "input" }, [[0, 0], [0, 0], [0, 0]]);
    const page = fakePage(cdp);
    const w = new IsolatedWorld(page);
    const got = await Promise.all([w.evaluate(ISO_VIEWPORT), w.nodeAt(1, 1), w.callOnNode(3, ISO_DESCRIBE), w.scroll()]);
    expect(got[1]).toBe(3);
    expect(got[2]).toEqual({ tag: "input" });
    expect(page.sessions()).toBe(1);
    expect(cdp.calls.filter(([m]) => m === "Page.createIsolatedWorld").length).toBe(1);
  });

  it("a stale world is re-created once; a node that is gone leaves the world alone", async () => {
    class Flaky extends HitCdp {
      resolveErrors: string[] = [];
      async send(method: string, params?: any): Promise<any> {
        if (method === "DOM.resolveNode" && this.resolveErrors.length) {
          this.calls.push([method, params]);
          throw new Error(this.resolveErrors.shift());
        }
        return super.send(method, params);
      }
    }
    const stale = new Flaky(null, { tag: "a" });
    stale.resolveErrors = ["Cannot find context with specified id"];
    const w1 = new IsolatedWorld(fakePage(stale));
    expect(await w1.callOnNode(5, ISO_DESCRIBE)).toEqual({ tag: "a" });
    expect(stale.calls.filter(([m]) => m === "Page.createIsolatedWorld").length).toBe(2);
    const gone = new Flaky(null, { tag: "b" });
    gone.resolveErrors = ["No node with given id found"];
    const w2 = new IsolatedWorld(fakePage(gone));
    expect(await w2.callOnNode(5, ISO_DESCRIBE)).toBeUndefined();
    expect(await w2.callOnNode(5, ISO_DESCRIBE)).toEqual({ tag: "b" });
    expect(gone.calls.filter(([m]) => m === "Page.createIsolatedWorld").length).toBe(1);
  });

  it("callOnNode and send resolve undefined instead of touching the page world", async () => {
    const noCdp = { context: () => ({ newCDPSession: async () => { throw new Error("no cdp"); } }),
      evaluate: () => { throw new Error("must not fall back to page.evaluate"); } };
    expect(await new IsolatedWorld(noCdp).callOnNode(1, ISO_DESCRIBE)).toBeUndefined();
    expect(await new IsolatedWorld(noCdp).send("DOM.describeNode", {})).toBeUndefined();
    expect(await new IsolatedWorld(noCdp).nodeAt(1, 1)).toBeUndefined();
  });

  it("the form reads are functions of their argument, not page globals", () => {
    for (const src of [ISO_DESCRIBE, ISO_ACTIVE_VALUE, ISO_ACTIVE_FIELD, ISO_SCROLL]) {
      expect(src).not.toContain("__");
      expect(src).not.toContain("window.");
    }
    expect(ISO_DESCRIBE.trimStart().startsWith("function (a)")).toBe(true);
  });

  // The reads are copied VERBATIM from sdk/python/clearcote/_isolated.py: one page-side contract.
  const PY = fileURLToPath(new URL("../../python/clearcote/_isolated.py", import.meta.url));
  it.runIf(existsSync(PY))("the read sources are the Python reference's, verbatim", () => {
    const src = readFileSync(PY, "utf8").replace(/\r\n/g, "\n");
    const py = (name: string): string => {
      const triple = new RegExp(`^${name} = """([\\s\\S]*?)"""`, "m").exec(src);
      if (triple) return triple[1];
      const plain = new RegExp(`^${name} = "(.*)"$`, "m").exec(src);
      if (!plain) throw new Error(`${name} not found in _isolated.py`);
      return plain[1];
    };
    expect(ISO_DESCRIBE).toBe(py("DESCRIBE"));
    expect(ISO_ACTIVE_VALUE).toBe(py("ACTIVE_VALUE"));
    expect(ISO_ACTIVE_FIELD).toBe(py("ACTIVE_FIELD"));
    expect(ISO_SCROLL).toBe(py("SCROLL"));
    expect(ISO_FORM_CONTROL).toBe(py("FORM_CONTROL"));
    expect(ISO_VIEWPORT).toBe(py("VIEWPORT"));
    expect(ISO_PLATFORM).toBe(py("PLATFORM"));
    expect(ISO_HIT_IN_BOX).toBe(py("HIT_IN_BOX"));
    expect(ISO_LISTBOX_ROW).toBe(py("LISTBOX_ROW"));
    expect(ISO_CSS_BOX).toBe(py("CSS_BOX"));
    expect(ISO_SCROLL_BY).toBe(py("SCROLL_BY"));
    expect(ISO_MOVE_OFF_SAFE).toBe(py("MOVE_OFF_SAFE"));   // SC 299 eager move-off
    // LABEL_OF holds a regex: the Python source escapes its backslashes the way the template literal does
    expect(ISO_LABEL_OF).toBe(py("LABEL_OF").replace(/\\\\/g, "\\"));
  });
});

// ISO_LABEL_OF run against a stand-in DOM (node:vm, no jsdom): the input, its <label>, and the label's content rects
// the way Range.getClientRects() lists them. SC 239 box probe (10-17), `<label><input type=checkbox> Visible checkbox
// with a label</label>`: the first content rect IS the input's border box ([44, 75, 13, 13]) and the helper took it as
// "the text", so every label click aimed at the input (lab F_label_visible 0/60, verifier B 0/20). The live proof of
// the fix is the lab's F_label_visible scenario.
describe("ISO_LABEL_OF on a stand-in DOM", () => {
  type R = { x: number; y: number; width: number; height: number; left: number; top: number; right: number; bottom: number };
  const rect = (x: number, y: number, w: number, h: number): R => ({ x, y, width: w, height: h, left: x, top: y, right: x + w, bottom: y + h });
  const visible = { display: "inline-block", visibility: "visible", opacity: "1", clip: "" };
  const INPUT = rect(44, 75, 13, 13), LABEL = rect(40, 74, 214.8, 17), TEXT = rect(61, 75, 194, 15);
  const labelOf = (wraps: boolean, contents: R[]) => {
    const label: any = { tagName: "LABEL", getBoundingClientRect: () => LABEL, style: visible, contains: (n: unknown) => wraps && n === input };
    const input: any = {
      nodeType: 1, tagName: "INPUT", type: "checkbox", checked: false, disabled: false, style: visible,
      getBoundingClientRect: () => INPUT, labels: [label], closest: () => label,
    };
    const sandbox = {
      input, innerWidth: 1280, innerHeight: 800,
      getComputedStyle: (el: any) => el.style,
      document: { createRange: () => ({ selectNodeContents() { /* the label */ }, getClientRects: () => contents }) },
    };
    return vm.runInNewContext(`(${ISO_LABEL_OF}).call(input)`, sandbox);
  };

  it("a wrapping label: the text's rect is its content clear of the control's box, contains = true", () => {
    const o = labelOf(true, [INPUT, TEXT]);
    expect(o.rect).toEqual([44, 75, 13, 13]);
    expect(o.hidden).toBe(false);
    expect(o.label).toEqual({ rect: [40, 74, 214.8, 17], text: [61, 75, 194, 15], contains: true });
  });

  it("a wrapping label holding only the control has no text rect", () => {
    expect(labelOf(true, [INPUT]).label).toEqual({ rect: [40, 74, 214.8, 17], text: null, contains: true });
  });

  it("a wrapping label whose text wraps under the control keeps the first clear line", () => {
    const o = labelOf(true, [INPUT, TEXT, rect(40, 92, 120, 15)]);
    expect(o.label.text).toEqual([61, 75, 194, 15]);
  });

  it("a <label for> beside the input: the union of its content rects, contains = false", () => {
    const o = labelOf(false, [rect(61, 75, 100, 15), rect(161, 75, 94, 15)]);
    expect(o.label).toEqual({ rect: [40, 74, 214.8, 17], text: [61, 75, 194, 15], contains: false });
  });

  it("not a checkbox or radio: null", () => {
    const input: any = { nodeType: 1, tagName: "INPUT", type: "text" };
    expect(vm.runInNewContext(`(${ISO_LABEL_OF}).call(input)`, { input })).toBeNull();
  });
});
