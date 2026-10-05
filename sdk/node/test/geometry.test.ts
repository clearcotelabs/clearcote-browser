/**
 * Headless window-geometry tests.
 *
 * A default headless launch used to report a window LARGER than the screen it claims to be on:
 *
 *     screen 1280x720   avail 1280x720   inner 1280x720   outer 1288x851
 *
 * `outer > screen` is not a subtle statistical tell but a state no real browser can be in, readable
 * with two property lookups. These lock down the frame arithmetic, the fact that the default is
 * actually applied at both launch entry points (and never over a caller's own choice), and the
 * regime split — with `--fingerprint` the engine's persona owns screen/avail and the SDK only fits
 * the window; without it the SDK also sets the headless display (`--screen-info`).
 *
 * PARITY: the vector below is duplicated verbatim in sdk/python/tests/test_geometry.py and
 * sdk/dotnet/tests/GeometryTests.cs. A seed must select the same persona in every SDK or a persona
 * stops being portable between them.
 */
import { describe, expect, it, vi } from "vitest";
import {
  ENGINE_FRAME_HEIGHT,
  ENGINE_FRAME_WIDTH,
  HEADLESS_SCREEN_PROFILES,
  WINDOWS_TASKBAR_HEIGHT,
  applyHeadlessGeometry,
  callerSetTheDisplay,
  callerSizedTheWindow,
  fitPlan,
  fitServedWindow,
  fitWindowOverCdp,
  fitWindowToWorkArea,
  geometryIsCoherent,
  headlessGeometry,
  personaActive,
  profileScreenFromArgs,
  screenInfoSwitch,
  servedDisplay,
  servedGeometry,
} from "../src/geometry.js";
import { lightStealthScreen, lightStealthValues } from "../src/fingerprint.js";
import { gzipSync } from "node:zlib";

// headlessGeometry's viewport is what the Python and .NET SDKs size against the linux engine frame;
// this SDK's launch() takes only its screen row (the display) and fits the real window instead.
describe("frame arithmetic (the cross-SDK viewport)", () => {
  it("leaves every profile row a window that fits its screen", () => {
    for (const [sw, sh] of HEADLESS_SCREEN_PROFILES) {
      const inner = [sw - ENGINE_FRAME_WIDTH, sh - ENGINE_FRAME_HEIGHT];
      const outer = [inner[0] + ENGINE_FRAME_WIDTH, inner[1] + ENGINE_FRAME_HEIGHT];
      // A CDP screen override forces avail == screen (measured), so that is what a page will read.
      expect(geometryIsCoherent([sw, sh], [sw, sh], inner, outer)).toBe(true);
    }
  });

  it("derives the viewport from whichever screen the seed selected", () => {
    const table = new Set(HEADLESS_SCREEN_PROFILES.map(([w, h]) => `${w}x${h}`));
    for (let i = 0; i < 200; i++) {
      const g = headlessGeometry(`formula-${i}`);
      expect(table.has(`${g.screen.width}x${g.screen.height}`)).toBe(true);
      expect(g.viewport).toEqual({
        width: g.screen.width - ENGINE_FRAME_WIDTH,
        height: g.screen.height - ENGINE_FRAME_HEIGHT,
      });
    }
  });

  it("rejects the pre-fix default geometry", () => {
    // If the invariant accepted this, it would not have caught anything.
    expect(geometryIsCoherent([1280, 720], [1280, 720], [1280, 720], [1288, 851])).toBe(false);
  });

  it("lands the window flush with the screen edge", () => {
    const g = headlessGeometry("flush");
    expect([g.viewport.width + ENGINE_FRAME_WIDTH, g.viewport.height + ENGINE_FRAME_HEIGHT])
      .toEqual([g.screen.width, g.screen.height]);
  });
});

describe("selection", () => {
  it("is deterministic, and a seedless launch is stable rather than random", () => {
    expect(headlessGeometry("abc")).toEqual(headlessGeometry("abc"));
    expect(headlessGeometry(undefined)).toEqual(headlessGeometry(""));
    expect(headlessGeometry(null)).toEqual(headlessGeometry(undefined));
  });

  it("reaches every row and follows the corpus weighting", () => {
    const seen = new Map<string, number>();
    for (let i = 0; i < 4000; i++) {
      const g = headlessGeometry(`s${i}`);
      const k = `${g.screen.width}x${g.screen.height}`;
      seen.set(k, (seen.get(k) ?? 0) + 1);
    }
    expect(seen.size).toBe(HEADLESS_SCREEN_PROFILES.length);
    const counts = [...seen.values()];
    expect(seen.get("1920x1080")).toBe(Math.max(...counts));
    // the capped ultrawide must stay rare
    expect((seen.get("3440x1440") ?? 0) / 4000).toBeLessThan(0.08);
  });

  const PARITY: Array<[string | null | undefined, number, number, number, number]> = [
    [null, 1920, 1080, 1912, 949],
    ["", 1920, 1080, 1912, 949],
    ["seed-1", 1920, 1200, 1912, 1069],
    ["acct-42", 1366, 768, 1358, 637],
    ["clearcote", 2560, 1440, 2552, 1309],
    ["x", 2560, 1440, 2552, 1309],
    ["y", 3440, 1440, 3432, 1309],
    ["z", 1920, 1080, 1912, 949],
    ["12345", 1920, 1080, 1912, 949],
  ];

  it.each(PARITY)("cross-SDK parity: seed %p", (seed, sw, sh, vw, vh) => {
    expect(headlessGeometry(seed)).toEqual({
      screen: { width: sw, height: sh },
      viewport: { width: vw, height: vh },
    });
  });
});

describe("regime detection", () => {
  it("reads the persona off the command line, not the caller's options", () => {
    // lightStealth passes a seed to the SDK but deliberately drops --fingerprint, so no persona runs.
    expect(personaActive(["--fingerprint=abc", "--no-sandbox"])).toBe(true);
    expect(personaActive(["--fingerprint-platform=windows", "--fingerprint-screen-width=1920"])).toBe(false);
    expect(personaActive([])).toBe(false);
    expect(personaActive(null)).toBe(false);
  });

  it("detects every caller window flag", () => {
    expect(callerSizedTheWindow(["--window-size=1920,1080"])).toBe(true);
    expect(callerSizedTheWindow(["--window-position=0,0"])).toBe(true);
    expect(callerSizedTheWindow(["--start-maximized"])).toBe(true);
    expect(callerSizedTheWindow(["--no-sandbox", "--fingerprint=x"])).toBe(false);
  });
});

describe("apply / skip rules", () => {
  it("sets the seed's display and viewport: null with no persona, headless true or unset", () => {
    for (const base of [{ headless: true }, {}]) {
      const opts: Record<string, unknown> = { ...base };
      const applied = applyHeadlessGeometry(opts, "seed", ["--no-sandbox"], { platform: "windows" });
      const { screen } = headlessGeometry("seed");
      const display = { width: screen.width, height: screen.height, availWidth: screen.width,
        availHeight: screen.height - WINDOWS_TASKBAR_HEIGHT };
      expect(applied).toEqual({
        mode: "display",
        display,
        args: [screenInfoSwitch(display), "--window-position=0,0"],
      });
      // the window is fitted to the display, never sized against a per-platform frame
      expect(opts.viewport).toBeNull();
      expect("screen" in opts).toBe(false);
    }
  });

  it("keeps the cross-SDK screen row under lightStealth (serve() takes lightStealth's own)", () => {
    for (const seed of ["a", "b", "seed-1", "acct-42"]) {
      const applied = applyHeadlessGeometry({}, seed, [], { platform: "windows", lightStealth: true } as never);
      expect(applied?.mode === "display" && [applied.display!.width, applied.display!.height])
        .toEqual([headlessGeometry(seed).screen.width, headlessGeometry(seed).screen.height]);
    }
  });

  it("gives the display a taskbar on Windows only", () => {
    const applied = applyHeadlessGeometry({}, "seed", [], { platform: "linux" });
    expect(applied?.mode === "display" && applied.display!.availHeight).toBe(headlessGeometry("seed").screen.height);
  });

  it("keeps a caller's own display or window switch", () => {
    const own = applyHeadlessGeometry({}, "seed", ["--screen-info={1280x720}"]);
    expect(own).toEqual({ mode: "display", display: null, args: ["--window-position=0,0"] });
    // a caller-sized window still gets a real screen under it; the fit then leaves it alone
    const sized = applyHeadlessGeometry({}, "seed", ["--window-size=1440,900"], { platform: "windows" });
    expect(sized?.args).toHaveLength(1);
    expect(sized?.args[0]).toMatch(/^--screen-info=/);
  });

  it("takes viewport: null and leaves screen to the persona", () => {
    // Setting screen here would be a silent no-op: the persona's value beats the CDP override.
    const opts: Record<string, unknown> = { headless: true };
    expect(applyHeadlessGeometry(opts, "seed", ["--fingerprint=seed"])).toEqual({ mode: "persona", args: [] });
    expect(opts.viewport).toBeNull();
    expect("screen" in opts).toBe(false);
  });

  it("is skipped when headed", () => {
    const opts: Record<string, unknown> = { headless: false };
    expect(applyHeadlessGeometry(opts, "seed", ["--fingerprint=seed"])).toBeNull();
    expect(opts).toEqual({ headless: false });
  });

  it.each([
    { viewport: { width: 800, height: 600 } },
    { viewport: null },
    { screen: { width: 1024, height: 768 } },
  ])("never overrides a caller who expressed geometry intent: %p", (explicit) => {
    for (const args of [["--no-sandbox"], ["--fingerprint=seed"]]) {
      const opts: Record<string, unknown> = { headless: true, ...explicit };
      expect(applyHeadlessGeometry(opts, "seed", args)).toBeNull();
      expect(opts).toEqual({ headless: true, ...explicit });
    }
  });
});

describe("the imported profile's screen", () => {
  /** Encode a capture the way fingerprintArgs does (gzip+base64 on --fingerprint-profile). */
  const profileArg = (profile: unknown) =>
    "--fingerprint-profile=" + gzipSync(Buffer.from(JSON.stringify(profile), "utf8")).toString("base64");

  it("reads the screen off the switch", () => {
    expect(profileScreenFromArgs([profileArg({ screen: { width: 3440, height: 1440 } }), "--no-sandbox"]))
      .toEqual([3440, 1440]);
  });

  it("uses the imported display instead of a corpus pick", () => {
    // Otherwise every seedless profile launch shares one screen and the imported identity is lost.
    const opts: Record<string, unknown> = { headless: true };
    const applied = applyHeadlessGeometry(opts, "some-seed", [profileArg({ screen: { width: 2560, height: 1440 } })],
      { platform: "windows" });
    expect(applied).toMatchObject({
      mode: "display",
      display: { width: 2560, height: 1440, availWidth: 2560, availHeight: 1400 },
    });
    expect(opts.viewport).toBeNull();
  });

  it("falls back when the profile screen is too small to size a viewport against", () => {
    // profile="auto" resolved on a headless host can carry the 800x600 headless surface (measured).
    const arg = profileArg({ screen: { width: 800, height: 600 } });
    expect(profileScreenFromArgs([arg])).toBeNull();
    const applied = applyHeadlessGeometry({ headless: true }, "seed", [arg]);
    const { screen } = headlessGeometry("seed");
    expect(applied?.mode === "display" && [applied.display!.width, applied.display!.height])
      .toEqual([screen.width, screen.height]);
  });

  it.each([
    "--fingerprint-profile=not-base64!!",
    "--fingerprint-profile=",
    "--fingerprint-profile=aGVsbG8=",
  ])("never breaks the launch on an unreadable profile: %s", (arg) => {
    expect(profileScreenFromArgs([arg])).toBeNull();
  });

  it("falls back when the profile has no screen block", () => {
    expect(profileScreenFromArgs([profileArg({ navigator: { platform: "Win32" } })])).toBeNull();
  });

  it("still takes the persona regime when a seed sits beside the profile", () => {
    // With --fingerprint present the ENGINE applies the profile's screen, so the SDK keeps out.
    const opts: Record<string, unknown> = { headless: true };
    const applied = applyHeadlessGeometry(opts, "seed",
      [profileArg({ screen: { width: 2560, height: 1440 } }), "--fingerprint=seed"]);
    expect(applied).toEqual({ mode: "persona", args: [] });
    expect(opts.viewport).toBeNull();
  });
});

describe("the work-area window fit", () => {
  /** Models the engine: it reports outerHeight `heightBias` px below the bounds height it was given
   *  (33 on 149.0.7827.114). */
  function fakePage(avail: number[], opts: {
    heightBias?: number; biasFirstCallOnly?: boolean; initial?: number[]; staleReads?: number;
  } = {}) {
    const heightBias = opts.heightBias ?? 33;
    let outer = opts.initial ?? [0, 0];
    let previous = outer;
    let stale = 0;
    let boundsCalls = 0;
    const calls: Array<[string, unknown]> = [];
    const cdp = {
      send: vi.fn(async (method: string, params?: Record<string, unknown>) => {
        calls.push([method, params]);
        if (method === "Browser.getWindowForTarget") return { windowId: 7 };
        if (method === "Browser.setWindowBounds") {
          const b = params!.bounds as { width?: number; height?: number };
          // CDP accepts a partial bounds; a position-only move must not touch the size.
          if (b.width !== undefined && b.height !== undefined) {
            boundsCalls++;
            const biased = !opts.biasFirstCallOnly || boundsCalls === 1;
            previous = outer;
            outer = [b.width, b.height - (biased ? heightBias : 0)];
            stale = opts.staleReads ?? 0;
          }
        }
        return {};
      }),
    };
    const page = {
      evaluate: async (js: string) => {
        if (js.includes("availWidth")) return avail;
        if (stale > 0) { stale--; return previous; }
        return outer;
      },
      context: () => ({ newCDPSession: async () => cdp }),
    };
    return {
      page: page as never,
      bounds: () => calls.filter(([m]) => m === "Browser.setWindowBounds").map(([, p]) => (p as { bounds: unknown }).bounds),
    };
  }

  it("maximizes into the work area, correcting the reported shortfall", async () => {
    const f = fakePage([1920, 1040]);
    await expect(fitWindowToWorkArea(f.page, ["--fingerprint=x"])).resolves.toEqual([1920, 1040]);
    expect(f.bounds()).toEqual([
      { left: 0, top: 0, width: 1920, height: 1040 },   // first attempt lands 33 short
      { left: 0, top: 0, width: 1920, height: 1073 },   // + the measured shortfall
    ]);
  });

  it("needs no correction when the engine honours bounds exactly", async () => {
    const f = fakePage([1920, 1040], { heightBias: 0 });
    await expect(fitWindowToWorkArea(f.page)).resolves.toEqual([1920, 1040]);
    expect(f.bounds()).toHaveLength(1);
  });

  it("reverts rather than overshooting the work area", async () => {
    const f = fakePage([1920, 1040], { biasFirstCallOnly: true });
    await expect(fitWindowToWorkArea(f.page)).resolves.toEqual([1920, 1040]);
    const b = f.bounds();
    expect(b).toHaveLength(3);
    expect(b[2]).toEqual(b[0]);
  });

  it("measures the shortfall only once the page has the new size", async () => {
    // A read straight after the resize can still have the engine's default window: correcting from
    // it asked for 1920+975 px, and a second stale read hid the overshoot.
    const f = fakePage([1920, 1040], { initial: [945, 1020], staleReads: 2 });
    await expect(fitWindowToWorkArea(f.page, ["--fingerprint=x"])).resolves.toEqual([1920, 1040]);
    expect(f.bounds()).toEqual([
      { left: 0, top: 0, width: 1920, height: 1040 },
      { left: 0, top: 0, width: 1920, height: 1073 },
    ]);
  });

  it("defers to a caller-supplied window size", async () => {
    const f = fakePage([1920, 1040]);
    await expect(fitWindowToWorkArea(f.page, ["--window-size=1024,768"])).resolves.toBeNull();
    expect(f.bounds()).toEqual([]);
  });

  it("declines an implausible work area", async () => {
    // No display set -> the headless default. Maximizing to 800x600 is worse than leaving it.
    const f = fakePage([800, 600]);
    await expect(fitWindowToWorkArea(f.page)).resolves.toBeNull();
    expect(f.bounds()).toEqual([]);
  });

  it("never throws — a geometry improvement must not fail a launch", async () => {
    const boom = { evaluate: async () => { throw new Error("target closed"); } } as never;
    await expect(fitWindowToWorkArea(boom)).resolves.toBeNull();
  });

  it("plans no correction when the window already fills the work area", () => {
    expect(fitPlan([1920, 1040], [1920, 1040])).toBeNull();
    expect(fitPlan([1920, 1040], [1920, 1007])).toEqual([1920, 1073]);
    // never asks for more than the shortfall
    expect(fitPlan([1920, 1040], [1900, 1040])).toEqual([1940, 1040]);
  });
});

// serve() hands out a raw CDP endpoint: no Playwright context options reach its pages, so geometry is
// set browser-wide (the headless display + the real window). The live proof is cc-gateway's
// test/e2e-geometry.mjs and tools/probe-geometry.mjs; these lock down the choices and the CDP.
describe("serve(): the headless display", () => {
  it("is a lightStealth seed's own row, so its screen and DPR stay a pair", () => {
    for (const seed of ["a", "b", "c", "probe-1", "probe-7"]) {
      const d = servedDisplay({ seed, lightStealth: true, platform: "windows" });
      expect(d).toEqual(lightStealthScreen(seed));
      const dpr = lightStealthValues(seed).devicePixelRatio;
      // 1536x864 is the only non-1.0 laptop row; 2560x1440 appears at 1.0 and 1.5.
      if (d.width === 1536) expect(dpr).toBe(1.25);
    }
  });

  it("is the corpus pick launch() uses when no persona and no lightStealth", () => {
    const d = servedDisplay({ seed: "x", platform: "windows" });
    expect([d.width, d.height]).toEqual([headlessGeometry("x").screen.width, headlessGeometry("x").screen.height]);
    expect(d.availHeight).toBe(d.height - WINDOWS_TASKBAR_HEIGHT);
  });

  it("gives a taskbar to Windows only", () => {
    const d = servedDisplay({ seed: "x", platform: "linux" });
    expect([d.availWidth, d.availHeight]).toEqual([d.width, d.height]);
    const ls = servedDisplay({ seed: "x", lightStealth: true, platform: "linux" });
    expect(ls.availHeight).toBe(ls.height);
  });

  it("agrees with an explicit screen the engine already spoofs", () => {
    expect(servedDisplay({ screenWidth: 1600, screenHeight: 900, platform: "windows" }))
      .toEqual({ width: 1600, height: 900, availWidth: 1600, availHeight: 860 });
    expect(servedDisplay({ screenWidth: 1600, screenHeight: 900, availHeight: 870, platform: "windows" }).availHeight).toBe(870);
    // never a work area larger than the screen
    expect(servedDisplay({ screenWidth: 1600, screenHeight: 900, availWidth: 2000, platform: "windows" }).availWidth).toBe(1600);
  });

  it("prefers an imported profile's screen", () => {
    const flag = "--fingerprint-profile=" + gzipSync(Buffer.from(JSON.stringify({ screen: { width: 1680, height: 1050 } }))).toString("base64");
    expect(servedDisplay({ seed: "x", lightStealth: true, platform: "windows", args: [flag] }))
      .toEqual({ width: 1680, height: 1050, availWidth: 1680, availHeight: 1010 });
  });

  it("writes the taskbar as a work-area inset", () => {
    expect(screenInfoSwitch({ width: 1920, height: 1080, availWidth: 1920, availHeight: 1040 }))
      .toBe("--screen-info={1920x1080 workAreaBottom=40}");
    expect(screenInfoSwitch({ width: 1920, height: 1080, availWidth: 1920, availHeight: 1080 }))
      .toBe("--screen-info={1920x1080}");
  });
});

describe("serve(): which switches", () => {
  it("sets the display and window origin without a persona", () => {
    const g = servedGeometry(["--fingerprint-platform=windows"], { fingerprint: "probe-1", lightStealth: true, platform: "windows" })!;
    expect(g.persona).toBe(false);
    expect(g.args).toEqual([screenInfoSwitch(lightStealthScreen("probe-1")), "--window-position=0,0"]);
  });

  it("leaves the display to a persona (matched after launch)", () => {
    const g = servedGeometry(["--fingerprint=seed"], { fingerprint: "seed" })!;
    expect(g).toEqual({ persona: true, display: null, args: ["--window-position=0,0"] });
  });

  it("never passes --window-size: it would force every popup to that size", () => {
    for (const args of [[], ["--fingerprint=s"]]) {
      expect(servedGeometry(args, {})!.args.some((a) => a.startsWith("--window-size"))).toBe(false);
    }
  });

  it("stays out of the way when headed or when the caller set a window or display", () => {
    expect(servedGeometry([], {}, false)).toBeNull();
    for (const flag of ["--window-size=1024,768", "--window-position=5,5", "--start-maximized", "--screen-info={1280x720}"]) {
      expect(servedGeometry([flag], {})).toBeNull();
    }
    expect(callerSetTheDisplay(["--screen-info={1x1}"])).toBe(true);
    // The SDK's own android --window-size counts: a phone persona sizes itself.
    expect(servedGeometry(["--fingerprint=s", "--window-size=412,915"], { platform: "android" })).toBeNull();
  });
});

describe("serve(): the window fit over CDP", () => {
  /** Models a served browser: the page reads its display, the window reports the bounds it got.
   *  `staleReads`: that many reads after each resize still report the size from before it, as a page
   *  does until the new window size reaches it (the read after that, the new size). */
  function fakeBrowser(display: number[], opts: {
    updateScreen?: boolean; heightBias?: number; initial?: number[]; staleReads?: number;
  } = {}) {
    let outer = opts.initial ?? [780, 580];
    let previous = outer;
    let stale = 0;
    const calls: Array<[string, Record<string, unknown> | undefined, string | undefined]> = [];
    const cdp = {
      send: vi.fn(async (method: string, params?: Record<string, unknown>, sessionId?: string) => {
        calls.push([method, params, sessionId]);
        switch (method) {
          case "Target.getTargets": return { targetInfos: [{ targetId: "T1", type: "page" }] };
          case "Target.attachToTarget": return { sessionId: "S1" };
          case "Runtime.evaluate": {
            if (String(params!.expression).includes("availWidth")) return { result: { value: display } };
            if (stale > 0) { stale--; return { result: { value: previous } }; }
            return { result: { value: outer } };
          }
          case "Emulation.getScreenInfos": return { screenInfos: [{ id: "2300000000", isPrimary: true }] };
          case "Emulation.updateScreen":
            if (opts.updateScreen === false) throw new Error("'Emulation.updateScreen' wasn't found");
            return {};
          case "Browser.getWindowForTarget": return { windowId: 3 };
          case "Browser.setWindowBounds": {
            const b = params!.bounds as { width: number; height: number };
            previous = outer;
            outer = [b.width, b.height - (opts.heightBias ?? 0)];
            stale = opts.staleReads ?? 0;
            return {};
          }
          default: return {};
        }
      }),
    };
    const methods = () => calls.map(([m]) => m);
    const of = (m: string) => calls.filter(([x]) => x === m).map(([, p]) => p);
    return { cdp, calls, methods, of };
  }

  it("maximizes onto the work area, and leaves a regime-2 display alone", async () => {
    const f = fakeBrowser([1920, 1080, 0, 0, 1920, 1040]);
    await expect(fitWindowOverCdp(f.cdp, { persona: false })).resolves.toEqual({
      display: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040 }, outer: [1920, 1040],
    });
    expect(f.of("Browser.setWindowBounds")).toEqual([{ windowId: 3, bounds: { left: 0, top: 0, width: 1920, height: 1040 } }]);
    expect(f.methods()).not.toContain("Emulation.updateScreen");
    expect(f.methods()).not.toContain("Runtime.enable");
    expect(f.methods().at(-1)).toBe("Target.detachFromTarget");
  });

  it("makes the headless display the persona's own, taskbar included, before sizing the window", async () => {
    const f = fakeBrowser([1536, 864, 0, 0, 1536, 824]);
    await fitWindowOverCdp(f.cdp, { persona: true });
    expect(f.of("Emulation.updateScreen")).toEqual([{
      screenId: "2300000000", left: 0, top: 0, width: 1536, height: 864,
      workAreaInsets: { left: 0, top: 0, right: 0, bottom: 40 },
    }]);
    expect(f.methods().indexOf("Emulation.updateScreen")).toBeLessThan(f.methods().indexOf("Browser.setWindowBounds"));
  });

  it("leaves the window alone when the engine cannot resize a persona's display", async () => {
    // Without it the window cannot outgrow the real 800x600 display: the fit would only be clamped.
    const f = fakeBrowser([1920, 1080, 0, 0, 1920, 1040], { updateScreen: false });
    await expect(fitWindowOverCdp(f.cdp, { persona: true })).resolves.toBeNull();
    expect(f.methods()).not.toContain("Browser.setWindowBounds");
    expect(f.methods().at(-1)).toBe("Target.detachFromTarget");
  });

  it("honours a window size inside the work area and clamps one past it", async () => {
    const f = fakeBrowser([1920, 1080, 0, 0, 1920, 1040]);
    await fitWindowOverCdp(f.cdp, { persona: false, windowSize: { width: 1440, height: 900 } });
    expect(f.of("Browser.setWindowBounds")[0]).toEqual({ windowId: 3, bounds: { left: 10, top: 10, width: 1440, height: 900 } });
    const g = fakeBrowser([1366, 768, 0, 0, 1366, 728]);
    await expect(fitWindowOverCdp(g.cdp, { persona: false, windowSize: { width: 1440, height: 900 } }))
      .resolves.toMatchObject({ outer: [1366, 728] });
    expect(g.of("Browser.setWindowBounds")[0]).toEqual({ windowId: 3, bounds: { left: 0, top: 0, width: 1366, height: 728 } });
  });

  it("adds back a reported shortfall without overshooting", async () => {
    const f = fakeBrowser([1920, 1080, 0, 0, 1920, 1040], { heightBias: 33 });
    await fitWindowOverCdp(f.cdp, { persona: false });
    expect(f.of("Browser.setWindowBounds").map((p) => (p!.bounds as { height: number }).height)).toEqual([1040, 1073]);
  });

  const sizes = (f: ReturnType<typeof fakeBrowser>) => f.of("Browser.setWindowBounds").map((p) => {
    const b = p!.bounds as { left: number; top: number; width: number; height: number };
    return `${b.left},${b.top} ${b.width}x${b.height}`;
  });

  it("waits for a resize to reach the page before measuring it", async () => {
    // Measured on 150/linux-x64 (no taskbar): the first read still had the default 945x1060 window,
    // the fit "corrected" 1440 to 1440+495, and the next read had only caught up with the first
    // resize, so the 1935x900 window on a 1920x1080 screen stayed.
    const f = fakeBrowser([1920, 1080, 0, 0, 1920, 1080], { initial: [945, 1060], staleReads: 1 });
    await expect(fitWindowOverCdp(f.cdp, { persona: false, windowSize: { width: 1440, height: 900 } }))
      .resolves.toMatchObject({ outer: [1440, 900] });
    expect(sizes(f)).toEqual(["10,10 1440x900"]);
  });

  it("does not correct a maximized window from a stale read", async () => {
    const f = fakeBrowser([1920, 1080, 0, 0, 1920, 1040], { initial: [945, 1020], staleReads: 2 });
    await expect(fitWindowOverCdp(f.cdp, { persona: false })).resolves.toMatchObject({ outer: [1920, 1040] });
    expect(sizes(f)).toEqual(["0,0 1920x1040"]);
  });

  it("still adds back a real shortfall when the page is slow to see it", async () => {
    const f = fakeBrowser([1920, 1080, 0, 0, 1920, 1040], { heightBias: 89, staleReads: 2 });
    await expect(fitWindowOverCdp(f.cdp, { persona: true })).resolves.toMatchObject({ outer: [1920, 1040] });
    expect(sizes(f)).toEqual(["0,0 1920x1040", "0,0 1920x1129"]);
  });

  it("corrects nothing when the page never shows the resize", async () => {
    const f = fakeBrowser([1920, 1080, 0, 0, 1920, 1080], { initial: [945, 1060], staleReads: Infinity });
    await fitWindowOverCdp(f.cdp, { persona: false, windowSize: { width: 1440, height: 900 }, settleMs: 100 });
    expect(sizes(f)).toEqual(["10,10 1440x900"]);
  });

  it("never plans bounds past the work area for a window smaller than it", () => {
    // 1440 wide at x=10 has 1910px of room; the misread shortfall would have asked for 1935.
    expect(fitPlan([1440, 900], [945, 1060], [1910, 1070])).toEqual([1910, 900]);
    // a window that fills the work area may need bounds beyond it (the persona engine clamps)
    expect(fitPlan([1920, 1040], [1920, 951], [1920, 1040])).toEqual([1920, 1129]);
  });

  it("declines the 800x600 surface (no persona engaged)", async () => {
    const f = fakeBrowser([800, 600, 0, 0, 800, 600]);
    await expect(fitWindowOverCdp(f.cdp, { persona: true })).resolves.toBeNull();
    expect(f.methods()).not.toContain("Browser.setWindowBounds");
  });

  it("never throws: no page, a dead connection, no endpoint", async () => {
    const empty = { send: async () => ({ targetInfos: [] }) };
    await expect(fitWindowOverCdp(empty, { persona: false })).resolves.toBeNull();
    const dead = { send: async () => { throw new Error("connection closed"); } };
    await expect(fitWindowOverCdp(dead, { persona: true })).resolves.toBeNull();
    await expect(fitServedWindow(undefined, { persona: false })).resolves.toBeNull();
    await expect(fitServedWindow("ws://127.0.0.1:9/devtools/browser/x", { persona: false, timeoutMs: 500 })).resolves.toBeNull();
  });
});
