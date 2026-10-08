import { describe, it, expect } from "vitest";
import { attachHumanize, sendEngineCommand, POINTER_PATH, POINTER_PATH_LEGACY } from "../src/humanize.js";

// r34 renamed Browser.humanizedClick to Browser.dispatchPointerPath (the old name was listed in
// /json/protocol, and "humanizedClick" is known vocabulary). r33 and older only know the old name.
// The new name goes first; the old one is tried only on a method-not-found, and the answer is
// remembered per browser.
const NOT_FOUND_RAW = Object.assign(new Error(`'${POINTER_PATH}' wasn't found`), { code: -32601 });
// What Playwright's CDPSession actually rejects with: the code is gone, the message stays.
const NOT_FOUND_PW = new Error(`Protocol error (${POINTER_PATH}): '${POINTER_PATH}' wasn't found`);

class FakeSession {
  calls: [string, any][] = [];
  constructor(private readonly reject: (method: string) => Error | null) {}
  async send(method: string, params?: any): Promise<any> {
    this.calls.push([method, params]);
    const e = this.reject(method);
    if (e) throw e;
    return undefined;
  }
  names() { return this.calls.map(([m]) => m); }
}

const params = { targetId: "T1", x: 10, y: 20, duration: 0.3, noClick: true };

describe("sendEngineCommand: new pointer-path name first, old name on method-not-found", () => {
  it("an r34 engine answers the new name: the old one is never sent", async () => {
    const s = new FakeSession(() => null);
    const key = {};
    await sendEngineCommand(s, key, POINTER_PATH, POINTER_PATH_LEGACY, params);
    await sendEngineCommand(s, key, POINTER_PATH, POINTER_PATH_LEGACY, params);
    expect(s.names()).toEqual([POINTER_PATH, POINTER_PATH]);
    expect(s.calls[0][1]).toEqual(params);
  });

  it("an older engine (-32601): retried once under the old name, which is then used directly", async () => {
    for (const notFound of [NOT_FOUND_RAW, NOT_FOUND_PW]) {
      const s = new FakeSession((m) => (m === POINTER_PATH ? notFound : null));
      const key = {};
      await sendEngineCommand(s, key, POINTER_PATH, POINTER_PATH_LEGACY, params);
      expect(s.names()).toEqual([POINTER_PATH, POINTER_PATH_LEGACY]);
      expect(s.calls[1][1]).toEqual(params);
      await sendEngineCommand(s, key, POINTER_PATH, POINTER_PATH_LEGACY, params);
      expect(s.names()).toEqual([POINTER_PATH, POINTER_PATH_LEGACY, POINTER_PATH_LEGACY]);
    }
  });

  it("any other error propagates with no fallback and nothing cached", async () => {
    const boom = new Error(`Protocol error (${POINTER_PATH}): Invalid parameters`);
    const s = new FakeSession(() => boom);
    const key = {};
    await expect(sendEngineCommand(s, key, POINTER_PATH, POINTER_PATH_LEGACY, params)).rejects.toBe(boom);
    expect(s.names()).toEqual([POINTER_PATH]);
    await expect(sendEngineCommand(s, key, POINTER_PATH, POINTER_PATH_LEGACY, params)).rejects.toBe(boom);
    expect(s.names()).toEqual([POINTER_PATH, POINTER_PATH]);
  });

  it("an old name that also fails propagates its error and caches nothing", async () => {
    const legacyErr = new Error("Protocol error (Browser.humanizedClick): Target closed");
    const s = new FakeSession((m) => (m === POINTER_PATH ? NOT_FOUND_RAW : legacyErr));
    const key = {};
    await expect(sendEngineCommand(s, key, POINTER_PATH, POINTER_PATH_LEGACY, params)).rejects.toBe(legacyErr);
    await expect(sendEngineCommand(s, key, POINTER_PATH, POINTER_PATH_LEGACY, params)).rejects.toBe(legacyErr);
    expect(s.names()).toEqual([POINTER_PATH, POINTER_PATH_LEGACY, POINTER_PATH, POINTER_PATH_LEGACY]);
  });
});

// The wiring: page.mouse.click routes through the engine, and the name learned on one page is
// reused by the next page of the same browser.
const fakePage = (browser: any) => {
  const noop = async () => undefined;
  const target = { send: async (m: string) => (m === "Target.getTargetInfo" ? { targetInfo: { targetId: "T1" } } : undefined) };
  const ctx = { newCDPSession: async () => target, browser: () => browser };
  const locatorProto = {
    fill: noop, click: noop, type: noop, dblclick: noop, hover: noop, press: noop,
    pressSequentially: noop, clear: noop, tap: noop, check: noop, uncheck: noop,
    dragTo: noop, page: () => null,
  };
  return {
    mouse: { move: noop, click: noop, wheel: noop, down: noop, up: noop },
    keyboard: { type: noop, press: noop, down: noop, up: noop, insertText: noop },
    click: noop, hover: noop, dblclick: noop, fill: noop, press: noop, type: noop,
    focus: noop, evaluate: async () => undefined, on: () => undefined,
    mainFrame: () => ({}), waitForTimeout: noop,
    context: () => ctx,
    locator: () => Object.create(locatorProto),
  } as any;
};

describe("mouse.click through the engine", () => {
  it("an r33 engine: one miss for the whole browser, then the old name straight away", async () => {
    const session = new FakeSession((m) => (m === POINTER_PATH ? NOT_FOUND_PW : null));
    const browser = { newBrowserCDPSession: async () => session };
    const p1 = fakePage(browser), p2 = fakePage(browser);
    await attachHumanize(browser as never, p1, { humanize: true, seed: "t" });
    await attachHumanize(browser as never, p2, { humanize: true, seed: "t" });
    await p1.mouse.click(300, 200);
    await p1.mouse.click(320, 240);
    await p2.mouse.click(100, 100);
    expect(session.names()).toEqual([POINTER_PATH, POINTER_PATH_LEGACY, POINTER_PATH_LEGACY, POINTER_PATH_LEGACY]);
    expect(session.calls[1][1]).toMatchObject({ targetId: "T1", x: 300, y: 200, noClick: false });
  }, 20_000);
});
