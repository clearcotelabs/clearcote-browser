import { describe, it, expect, vi } from "vitest";

// Key rollover in humanized typing (mirrors the Python rollover tests). Before it, every character
// was one press() -- down, dwell, up -- so 0 of 32 keydowns overlapped the previous key (measured on
// r32/r33). The persona's rolloverRate is forced here so the overlap is not left to chance.
const forced = vi.hoisted(() => ({ rate: 1 }));
vi.mock("../src/motion.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("../src/motion.js")>();
  return { ...real, rolloverRate: () => forced.rate };
});

import { attachHumanize } from "../src/humanize.js";

type Ev = [string, string];

const fakePage = (opts: { failOverlap?: boolean } = {}) => {
  const noop = async () => undefined;
  const events: Ev[] = [];
  const held = new Set<string>();
  const keyboard = {
    type: async (t: string) => { events.push(["type", t]); },
    press: async (k: string) => { events.push(["press", k]); },
    down: async (k: string) => {
      // failOverlap: the second keydown of a roll throws, as on a target that closed mid-pair
      if (opts.failOverlap && [...held].some((h) => h !== "Shift")) throw new Error("Target closed");
      held.add(k);
      events.push(["down", k]);
    },
    up: async (k: string) => { held.delete(k); events.push(["up", k]); },
    insertText: noop,
  };
  const locatorProto = {
    fill: noop, click: noop, type: noop, dblclick: noop, hover: noop, press: noop,
    pressSequentially: noop, clear: noop, tap: noop, check: noop, uncheck: noop,
    dragTo: noop, page: () => null,
  };
  const page = {
    mouse: { move: noop, click: noop, wheel: noop, down: noop, up: noop },
    keyboard,
    click: noop, hover: noop, dblclick: noop, fill: noop, press: noop, type: noop,
    focus: noop, evaluate: async () => undefined, on: () => undefined,
    mainFrame: () => ({}), waitForTimeout: noop,
    locator: () => Object.create(locatorProto),
  };
  return { page: page as any, events };
};

const typeWith = async (rate: number, text: string, opts: { failOverlap?: boolean } = {}) => {
  forced.rate = rate;
  const f = fakePage(opts);
  await attachHumanize({} as never, f.page, { humanize: true, seed: "t" });
  await f.page.keyboard.type(text);
  return f.events;
};

/** The field's value after these events: down/press of a character inserts it (Playwright's
 * keyboard.down inserts text exactly as press does), Backspace deletes (the typo path). */
const typedValue = (events: Ev[]): string => {
  let s = "";
  for (const [op, k] of events) {
    if (op === "press" && k === "Backspace") s = s.slice(0, -1);
    else if ((op === "down" || op === "press" || op === "type") && Array.from(k).length === 1) s += k;
  }
  return s;
};

/** Keydowns that arrive while another (non-modifier) key is still down. */
const overlaps = (events: Ev[]): number => {
  const held = new Set<string>();
  let n = 0;
  for (const [op, k] of events) {
    if (op === "down") { if ([...held].some((h) => h !== "Shift")) n++; held.add(k); }
    if (op === "up") held.delete(k);
  }
  return n;
};

/** Every down has exactly one later up, and no key goes down twice before its up. */
const balanced = (events: Ev[]): boolean => {
  const held = new Set<string>();
  for (const [op, k] of events) {
    if (op === "down") { if (held.has(k)) return false; held.add(k); }
    if (op === "up") { if (!held.has(k)) return false; held.delete(k); }
  }
  return held.size === 0;
};

describe("humanized typing: key rollover", () => {
  it("rolls fast pairs over: a keydown while the previous key is still down", async () => {
    const events = await typeWith(1, "asdf jkl");
    expect(overlaps(events)).toBeGreaterThanOrEqual(1);
    expect(balanced(events)).toBe(true);
    expect(typedValue(events)).toBe("asdf jkl");
    // a rolled pair is down(a) down(b) up(a) up(b): the first key comes up after the second goes down
    const i = events.findIndex(([op], j) => op === "down" && events[j + 1]?.[0] === "down");
    expect(i).toBeGreaterThanOrEqual(0);
    const [a, b] = [events[i][1], events[i + 1][1]];
    expect(events.slice(i, i + 4)).toEqual([["down", a], ["down", b], ["up", a], ["up", b]]);
  }, 20_000);

  it("down keys plus press keys, in order, are exactly the input when no typo fired", async () => {
    const events = await typeWith(1, "asdf jkl");
    const keys = events.filter(([op]) => op === "down" || op === "press").map(([, k]) => k);
    if (!keys.includes("Backspace")) expect(keys.join("")).toBe("asdf jkl");
    expect(typedValue(events)).toBe("asdf jkl");
  }, 20_000);

  it("rate 0 types as before: one press per character, no overlaps", async () => {
    const events = await typeWith(0, "asdf jkl");
    expect(overlaps(events)).toBe(0);
    expect(events.filter(([op]) => op === "down" || op === "up")).toEqual([]);
    expect(typedValue(events)).toBe("asdf jkl");
  }, 20_000);

  it("never rolls a key with itself, a shifted key, or a key outside the roll set", async () => {
    const events = await typeWith(1, "aaB\n!b");
    expect(overlaps(events)).toBe(0);
    expect(balanced(events)).toBe(true);
    expect(typedValue(events)).toBe("aaB\n!b");
  }, 20_000);

  it("keeps the Shift sequence for capitals and symbols", async () => {
    const events = await typeWith(1, "Hi!");
    expect(events.filter(([, k]) => ["Shift", "H", "i", "!"].includes(k))).toEqual([
      ["down", "Shift"], ["press", "H"], ["up", "Shift"],
      ["press", "i"],
      ["down", "Shift"], ["press", "!"], ["up", "Shift"],
    ]);
  }, 20_000);

  it("a failure inside a roll releases both keys and stops typing", async () => {
    const text = "asdf";
    const events = await typeWith(1, text, { failOverlap: true });
    // the roll's first key went down; its partner's keydown threw
    const downs = events.filter(([op]) => op === "down").map(([, k]) => k);
    expect(downs.length).toBe(1);
    const x = downs[0], y = text[text.indexOf(x) + 1];
    // best-effort release of BOTH keys, then nothing more
    expect(events.slice(-3)).toEqual([["down", x], ["up", x], ["up", y]]);
    expect(typedValue(events)).toBe(text.slice(0, text.indexOf(x) + 1));
  }, 20_000);
});
