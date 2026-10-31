/**
 * humanize options (lane A, SC 232): the options object, strict mode, the fallback report, per-call
 * { humanize: false }, presets and the seed -> persona guarantee, against the fake page of
 * humanize-forms.test.ts (test/helpers/humanize-fake.ts). Mirrors python/tests/test_humanize_options.py.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import { attached, protoCalls } from "./helpers/humanize-fake.js";
import { ClearcoteStrictError, applyToPersona, humanizePayload, resolveHumanize } from "../src/hzoptions.js";
import { perCallOff } from "../src/humanize.js";
import { clickHold, dblclickGap, dragDwell, hashSeed, keyDwell, makePersona, moveOffDelayMs, mulberry32, pauseMs } from "../src/motion.js";
import { readPauseMs } from "../src/attention.js";
import { prepareLaunch } from "../src/cloud.js";

const SLOW = 60_000;
const here = dirname(fileURLToPath(import.meta.url));
const PINS = JSON.parse(readFileSync(join(here, "..", "..", "testdata", "humanize_persona_pins.json"), "utf8"));
const ENV = "CLEARCOTE_HUMANIZE_TYPO_RATE";
const saved = process.env[ENV];
afterEach(() => {
  if (saved === undefined) delete process.env[ENV]; else process.env[ENV] = saved;
  protoCalls.length = 0;
});

const COUNTRIES = ["Afghanistan", "Albania", "Algeria", "Germany", "Ghana", "Greece"];
const countryField = () => ({
  tag: "select", type: "", value: "afg", selectedIndex: 0,
  options: COUNTRIES.map((c, i) => ({ value: c.slice(0, 3).toLowerCase(), label: c, selected: i === 0 })),
});

describe("the options object", () => {
  it("accepts true, \"strict\" and an object; an unknown key or a bad value throws", () => {
    expect(resolveHumanize(false)).toBeNull();
    expect(resolveHumanize(undefined)).toBeNull();
    expect(resolveHumanize(true)).toMatchObject({ preset: "default", strict: false, engine: "auto", tempo: 1 });
    expect(resolveHumanize("strict")).toMatchObject({ strict: true });
    expect(resolveHumanize({ preset: "quick", typo_rate: 0.01 })).toMatchObject({ preset: "quick", typoRate: 0.01, tempo: 0.7 });
    expect(resolveHumanize({ preset: "careful" })!.tempo).toBe(1.4);
    expect(() => resolveHumanize({ speed: 2 } as never)).toThrow(/unknown humanize option "speed"/);
    for (const bad of [{ preset: "fast" }, { engine: "maybe" }, { ambient: "always" }, { typoRate: 2 }, { handedness: "both" },
      { deviceHz: 5 }, { inputDevice: "pen" }, { strict: "yes" }, { onFallback: 1 }, "loud"]) {
      expect(() => resolveHumanize(bad as never)).toThrow(TypeError);
    }
    expect(humanizePayload(resolveHumanize({ preset: "quick", onFallback: () => {} })!))
      .toEqual({ preset: "quick", strict: false, engine: "auto" });
    // attention (SC 299): unset = on; false turns it off; anything but a boolean throws
    expect(resolveHumanize({})!.attention).toBeUndefined();
    expect(resolveHumanize({ attention: false })!.attention).toBe(false);
    expect(humanizePayload(resolveHumanize({ attention: false })!)).toEqual({ preset: "default", strict: false, engine: "auto", attention: false });
    expect(() => resolveHumanize({ attention: "no" } as never)).toThrow(TypeError);
  });

  it("ClearcoteStrictError is a Playwright-style Error with api, reason and selector", () => {
    const e = new ClearcoteStrictError("page.click", "covered", "#b");
    expect(e).toBeInstanceOf(Error);
    expect(e).toMatchObject({ name: "ClearcoteStrictError", api: "page.click", reason: "covered", selector: "#b" });
  });

  it("lands on the page and the persona as plain fields", async () => {
    const f = await attached({}, { ambient: "load", handedness: "left", deviceHz: 250, inputDevice: "trackpad", engine: "off" });
    const p = f.page._clearcotePersona;
    expect([p.handedness, p.inputDevice, p.deviceHz, p.tempo]).toEqual(["left", "trackpad", 250, 1]);
    expect(f.page._clearcoteAutoAmbient).toBe(true);
    expect(f.page._clearcoteAmbient).toBe("load");
    const g = await attached();
    expect(g.page._clearcoteAutoAmbient).toBe(false);
    expect(g.page._clearcoteAmbient).toBe("off");
  });
});

describe("fallback report and strict mode", () => {
  it("onFallback hears a covered target, then the stock click runs once", async () => {
    const events: any[] = [];
    const f = await attached({ covered: true }, { onFallback: (e: any) => events.push(e) });
    await f.page.click("#btn");
    expect(f.natives.map((n) => n.m)).toEqual(["click"]);
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ api: "page.click", selector: "#btn" });
    expect(events[0].reason).toMatch(/does not reach the element/);
  }, SLOW);

  it("strict throws on a covered target and runs nothing", async () => {
    const f = await attached({ covered: true }, "strict");
    const err = await f.page.click("#btn").catch((e: any) => e);
    expect(err).toBeInstanceOf(ClearcoteStrictError);
    expect(err.api).toBe("page.click");
    expect(String(err.message)).toMatch(/^page\.click: humanize strict/);
    expect(f.natives).toEqual([]);
    expect(f.of("down")).toEqual([]);
  }, SLOW);

  it("strict throws on an option object the humane route cannot take", async () => {
    const f = await attached({ field: countryField() as never }, { strict: true });
    const err = await f.page.selectOption("#country", { value: "ger", label: "Germany" }).catch((e: any) => e);
    expect(err).toBeInstanceOf(ClearcoteStrictError);
    expect(err.api).toBe("page.selectOption");
    expect(err.reason).toMatch(/option object/);
    expect(f.natives).toEqual([]);
  }, SLOW);

  it("strict throws on a frame locator, worded by the locator API", async () => {
    const f = await attached({}, "strict");
    const err = await f.page.locator("#fr >> internal:control=enter-frame >> #fi").fill("new").catch((e: any) => e);
    expect(err).toBeInstanceOf(ClearcoteStrictError);
    expect(err.api).toBe("locator.fill");
    // lane F: a frame locator takes the frame route; strict throws when that route cannot run (the fake proves no point)
    expect(err.reason).toMatch(/^frame: /);
    expect(String(err.message)).toMatch(/^locator\.fill: /);
    expect(protoCalls).toEqual([]);
  }, SLOW);

  it("onFallback for a forced hidden fill and a press on a button", async () => {
    const events: any[] = [];
    // SC 232 triage: a button is now also reached by a click into a text stop within 3 of it or, with nothing
    // focused, by Tab from the top when it comes within 3 stops of the ring -- so the fallback case needs a button
    // with no text stop near it and 4 stops from the top (b1-b3 are buttons; #t is the fake's first stop)
    const b = (y: number) => ({ tag: "button", type: "", box: { x: 100, y, width: 60, height: 30 } });
    const f = await attached({ fields: { "#t": { visible: false }, "#b1": b(120), "#b2": b(160), "#b3": b(200),
      "#b": { tag: "button", type: "", box: { x: 100, y: 260, width: 160, height: 40 } } } },
      { onFallback: (e: any) => events.push(e) });
    await f.page.fill("#t", "x", { force: true });
    await f.page.press("#b", "Enter");
    expect(events.map((e) => [e.api, e.reason])).toEqual([
      // lane D: a button press focuses by Tab (within 3 stops) or a focus click; on this fake the button is out of
      // Tab reach, so the pointer glides onto it and Playwright's focus is used -- reported -- then the key is held
      ["page.fill", "forced on a hidden target"], ["page.press", "focus: an activatable target out of Tab reach"]]);
    expect(f.natives.map((n) => n.m)).toEqual(["fill", "focus"]);
  }, SLOW);

  it("a throwing onFallback stops the stock call", async () => {
    const f = await attached({ covered: true }, { onFallback: () => { throw new Error("stop"); } });
    await expect(f.page.click("#btn")).rejects.toThrow(/stop/);
    expect(f.natives).toEqual([]);
  }, SLOW);

  it("a humanized action reports nothing", async () => {
    const events: any[] = [];
    process.env[ENV] = "0";
    const f = await attached({}, { onFallback: (e: any) => events.push(e) });
    await f.page.click("#btn");
    await f.page.fill("#in", "hello");
    expect(events).toEqual([]);
    expect(f.natives).toEqual([]);
  }, SLOW);
});

describe("per-call { humanize: false }", () => {
  it("is the stock call, the keyword stripped, on page, mouse, keyboard and locator methods", async () => {
    const f = await attached();
    await f.page.click("#btn", { humanize: false, timeout: 500 });
    await f.page.fill("#btn", "v", { humanize: false });
    expect(f.natives).toEqual([{ m: "click", args: ["#btn", { timeout: 500 }] }, { m: "fill", args: ["#btn", "v", {}] }]);
    expect(f.of("move")).toEqual([]);
    await f.page.mouse.move(50, 60, { humanize: false });
    expect(f.of("move").map((e) => e.a)).toEqual([{ x: 50, y: 60 }]);
    await f.page.keyboard.press("Tab", { humanize: false });
    expect(f.of("down").length + f.of("move").length).toBe(1);
    await f.page.locator("#btn").click({ humanize: false, timeout: 9 });
    expect(protoCalls).toEqual([{ m: "click", args: [{ timeout: 9 }] }]);
    // without the keyword: humanized again (a glide of many samples)
    await f.page.mouse.move(600, 400);
    expect(f.of("move").length).toBeGreaterThan(5);
  }, SLOW);

  it("perCallOff only strips an options object that says humanize", async () => {
    const calls: any[] = [];
    const g = perCallOff((...a: any[]) => calls.push(["stock", ...a]), (...a: any[]) => calls.push(["humane", ...a]));
    g("#a", { humanize: false, timeout: 1 });
    g("#a", { humanize: true, timeout: 2 });
    g("#a", { timeout: 3 });
    g({ label: "x" });
    expect(calls).toEqual([["stock", "#a", { timeout: 1 }], ["humane", "#a", { timeout: 2 }], ["humane", "#a", { timeout: 3 }],
      ["humane", { label: "x" }]]);
  });
});

describe("presets", () => {
  // The PLANNED time of one glide + one typed sentence on the fake page: every timer the humanizer asks for
  // is summed (and fired at once), so the measure does not depend on how loaded the machine is. Math.random
  // is seeded so every preset plans the same path; a preset also changes which thinking pauses are drawn,
  // so several runs are summed. The pacer (pacing.ts walk) asks for `due - elapsed`, so performance.now is a
  // VIRTUAL clock that only the summed timers advance: under full-suite load the real clock made the pacer
  // ask for less (and skip samples) by an amount that differed between the presets (SC 232 integration).
  const planned = async (humanize: unknown): Promise<number> => {
    process.env[ENV] = "0";
    const realRandom = Math.random;
    const realTimeout = globalThis.setTimeout;
    const realPerfNow = performance.now;
    let total = 0;
    let vt = realPerfNow.call(performance);
    performance.now = () => vt;
    (globalThis as any).setTimeout = (fn: (...a: any[]) => void, ms?: number, ...a: any[]) => {
      total += Number(ms) || 0;
      vt += Number(ms) || 0;
      return realTimeout(fn, 0, ...a);
    };
    try {
      for (let i = 0; i < 6; i++) {
        Math.random = mulberry32(1000 + i);
        const f = await attached({}, humanize);
        await f.page.mouse.move(700, 500);
        await f.page.keyboard.type("the quick brown fox jumps over the lazy dog");
        total += f.of("press").reduce((a, e) => a + (Number(e.a?.delay) || 0), 0);   // key holds go to Playwright
      }
    } finally {
      Math.random = realRandom;
      (globalThis as any).setTimeout = realTimeout;
      performance.now = realPerfNow;
    }
    return total;
  };

  it("quick is ~0.7x and careful ~1.4x the default's durations (+-15 %)", async () => {
    const base = await planned(true);
    const quick = await planned({ preset: "quick" });
    const careful = await planned({ preset: "careful" });
    expect(base).toBeGreaterThan(1000);
    expect(Math.abs(quick / base - 0.7)).toBeLessThanOrEqual(0.7 * 0.15);
    expect(Math.abs(careful / base - 1.4)).toBeLessThanOrEqual(1.4 * 0.15);
  }, 120_000);

  // Integration (lanes A x B/C/E; mirrors test_presets_scale_the_timing_medians_and_the_after_action_pauses): the
  // preset's tempo scales the persona's timing MEDIANS (click hold, key dwell, grab/release, double-click gap,
  // deliberation pauses) and the after-action pauses (move-off delay, read pause), not only the glide samples. The
  // same draw (rng 0.5 = the median) on the default and on the careful / quick copies gives an exact ratio.
  it("scales the timing medians and the after-action pauses, not only the glide samples", () => {
    const mid = () => 0.5;
    const plannedMs = (h: unknown): number[] => {
      const p = applyToPersona(makePersona("tempo"), resolveHumanize(h)!);
      const d = dragDwell(p, mid);
      return [clickHold(p, mid), keyDwell(p, mid), d.grabMs, d.releaseMs, dblclickGap(p, mid), pauseMs(p, 200, 400, mid),
        moveOffDelayMs(p, mid), readPauseMs(5000, p, mid)];
    };
    const base = plannedMs(true);
    expect(base[0]).toBeGreaterThan(0);
    expect(base[7]).toBeGreaterThan(0);
    for (const [preset, t] of [["quick", 0.7], ["careful", 1.4]] as const) {
      plannedMs({ preset }).forEach((v, i) => expect(v, `${preset} #${i}`).toBeCloseTo(base[i] * t, 9));
    }
  });
});

describe("same seed, same persona", () => {
  const BASE = ["seed", "deviceHz", "tremorHz", "tremorAmp", "driftAmp", "driftTau", "jitter", "fittsA", "fittsB",
    "primaryFrac", "overshoot", "maxCorrections", "approachBias"];
  const PY = ["seed", "device_hz", "tremor_hz", "tremor_amp", "drift_amp", "drift_tau", "jitter", "fitts_a", "fitts_b",
    "primary_frac", "overshoot", "max_corrections", "approach_bias"];
  const pick = (p: any) => BASE.map((k) => p[k]);

  it("pins the first 20 RNG draws", () => {
    expect(hashSeed(PINS.rng_seed)).toBe(PINS.hash);
    const r = mulberry32(hashSeed(PINS.rng_seed));
    expect(Array.from({ length: 20 }, () => r())).toEqual(PINS.draws);
  });

  it("pins the seed -> persona map (shared with Python and .NET)", () => {
    for (const pin of PINS.personas) {
      expect(pick(makePersona(pin.seed_input))).toEqual(PY.map((k) => pin.fields[k]));
    }
  });

  it("no option changes the persona's draws", async () => {
    for (const h of [true, "strict", { preset: "careful", handedness: "right", typoRate: 0.1, ambient: "idle", engine: "all" }]) {
      const f = await attached({}, h);
      // handedness fixes the SIGN of the drawn approach bias (integration of lanes A + B); the draw is unchanged
      const abs = (v: number[]) => v.map((x, i) => (BASE[i] === "approachBias" ? Math.abs(x) : x));
      expect(abs(pick(f.page._clearcotePersona))).toEqual(abs(pick(makePersona("forms-unit"))));
      if (typeof h === "object" && h.handedness === "right") expect(f.page._clearcotePersona.approachBias).toBeGreaterThanOrEqual(0);
    }
  });
});

describe("cloud launch", () => {
  it("forwards the options object as humanizeOptions next to humanize: false", () => {
    expect(prepareLaunch({ humanize: true, country: "us" }, false).body).toEqual({ country: "us", humanize: false });
    expect(prepareLaunch({ country: "us" }, false).body).toEqual({ country: "us", humanize: false });
    expect(prepareLaunch({ humanize: false }, false).body).toEqual({ humanize: false });
    expect(prepareLaunch({ humanize: { preset: "quick", typoRate: 0.01, onFallback: () => {} } }, false).body).toEqual({
      humanize: false, humanizeOptions: { preset: "quick", strict: false, engine: "auto", typoRate: 0.01 } });
    expect((prepareLaunch({ humanize: "strict" }, false).body as any).humanizeOptions.strict).toBe(true);
    expect(() => prepareLaunch({ humanize: { bogus: 1 } }, false)).toThrow(TypeError);
  });
});
