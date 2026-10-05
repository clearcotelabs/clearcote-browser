/**
 * serve()'s window fit and the WebSocket client it runs on (src/cdpws.ts).
 *
 * serve() puts its browser's first window onto the display's work area over a short CDP connection
 * of its own. That connection used to need Node's global WebSocket, which only exists from Node 22
 * on, so on Node 20 (which the SDK supports) the fit was skipped without a word: the display was
 * right, the window stayed the engine's default ~1000px, and sites served their tablet layout. The
 * first block runs the fit in a Node process that has no global WebSocket; the rest drive the client
 * against an endpoint that speaks raw frames.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { connectCdp } from "../src/cdpws.js";
import { fitServedWindow } from "../src/geometry.js";
import { fakeCdpServer, servedBrowser, type FakeCdp } from "./helpers/fake-cdp-ws.js";
import { runFitChild } from "./helpers/fit-child.js";

const fakes: FakeCdp[] = [];
afterEach(async () => {
  for (const f of fakes.splice(0)) await f.close();
});
async function fake(...a: Parameters<typeof fakeCdpServer>): Promise<FakeCdp> {
  const f = await fakeCdpServer(...a);
  fakes.push(f);
  return f;
}

describe("serve(): the window fit in a Node without a global WebSocket", () => {
  it("maximizes the window onto the work area", async () => {
    const f = await fake(servedBrowser([1920, 1080, 0, 0, 1920, 1040]));
    const r = await runFitChild({ FIT_MODE: "fit", FIT_WS_URL: f.wsUrl, FIT_OPTS: JSON.stringify({ persona: false }) });
    expect(r.webSocket).toBe("undefined");   // the child really had none
    expect(r.fit).toEqual({ display: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040 }, outer: [1920, 1040] });
    expect(f.methods).toContain("Browser.setWindowBounds");
    expect(f.methods).not.toContain("Runtime.enable");
  }, 60_000);

  it("clamps windowSize into the work area and gives a persona its display", async () => {
    const f = await fake(servedBrowser([1366, 768, 0, 0, 1366, 728]));
    const r = await runFitChild({
      FIT_MODE: "fit", FIT_WS_URL: f.wsUrl,
      FIT_OPTS: JSON.stringify({ persona: true, windowSize: { width: 1920, height: 1080 } }),
    });
    expect(r.webSocket).toBe("undefined");
    expect(r.fit).toMatchObject({ outer: [1366, 728] });
    expect(f.methods).toContain("Emulation.updateScreen");
  }, 60_000);
});

describe("the SDK's CDP WebSocket client", () => {
  const targets = () => ({ targetInfos: [{ targetId: "T1", type: "page" }] });

  it("masks every frame it sends, and answers a ping with its payload", async () => {
    const f = await fake(targets, { noise: true });
    const cdp = await connectCdp(f.wsUrl, 5000);
    try {
      await expect(cdp.send("Target.getTargets")).resolves.toEqual(targets());
      expect(f.allMasked).toBe(true);
      // The pong goes out when the ping is read, so it can reach the endpoint just after the reply.
      await vi.waitFor(() => expect(f.pongs).toEqual(["are you there"]));
    } finally {
      cdp.close();
    }
  });

  it("picks its own reply out of events and stale replies, across fragments and split reads", async () => {
    for (const opts of [{ noise: true, fragment: 7 }, { trickle: true }, { noise: true, trickle: true, fragment: 5 }]) {
      const f = await fake(targets, opts);
      const cdp = await connectCdp(f.wsUrl, 5000);
      try {
        await expect(cdp.send("Target.getTargets")).resolves.toEqual(targets());
        await expect(cdp.send("Target.getTargets", {}, "S1")).resolves.toEqual(targets());
      } finally {
        cdp.close();
      }
    }
  });

  it("reads and writes 16-bit and 64-bit payload lengths", async () => {
    for (const padding of [300, 70_000]) {
      const f = await fake((_m, params) => ({ echoed: String(params.big ?? "").length }), { padding });
      const cdp = await connectCdp(f.wsUrl, 5000);
      try {
        await expect(cdp.send("Echo", { big: "y".repeat(padding) })).resolves.toEqual({ echoed: padding });
      } finally {
        cdp.close();
      }
    }
  });

  it("rejects a protocol error with the browser's message", async () => {
    const f = await fake(() => { throw new Error("'Emulation.updateScreen' wasn't found"); });
    const cdp = await connectCdp(f.wsUrl, 5000);
    try {
      await expect(cdp.send("Emulation.updateScreen")).rejects.toThrow("'Emulation.updateScreen' wasn't found");
    } finally {
      cdp.close();
    }
  });

  it("connects to a bracketed IPv6 literal", async () => {
    const f = await fake(targets, { host: "::1" });
    expect(f.wsUrl).toContain("ws://[::1]:");
    const cdp = await connectCdp(f.wsUrl, 5000);
    try {
      await expect(cdp.send("Target.getTargets")).resolves.toEqual(targets());
    } finally {
      cdp.close();
    }
  });

  it("refuses an endpoint that turns the upgrade down or answers it wrongly", async () => {
    const refused = await fake(targets, { refuseUpgrade: 403 });
    await expect(connectCdp(refused.wsUrl, 5000)).rejects.toThrow(/upgrade refused: HTTP\/1\.1 403/);
    const wrong = await fake(targets, { wrongAccept: true });
    await expect(connectCdp(wrong.wsUrl, 5000)).rejects.toThrow(/accept key/);
    await expect(connectCdp("http://127.0.0.1:9/json/version", 5000)).rejects.toThrow(/expected a ws:\/\/ CDP URL/);
    await expect(connectCdp("ws://127.0.0.1:9/devtools/browser/x", 5000)).rejects.toThrow(/CDP connection (failed|closed)/);
  });

  it("fails a call at once when the browser closes the connection, and times out one it ignores", async () => {
    const f = await fake(targets, { closeOn: "Browser.close", silentOn: "Browser.getVersion" });
    let cdp = await connectCdp(f.wsUrl, 300);
    await expect(cdp.send("Browser.getVersion")).rejects.toThrow("Browser.getVersion timed out");
    cdp.close();
    cdp = await connectCdp(f.wsUrl, 30_000);
    const t0 = Date.now();
    await expect(cdp.send("Browser.close")).rejects.toThrow("CDP connection closed");
    expect(Date.now() - t0).toBeLessThan(2000);
    await expect(cdp.send("Target.getTargets")).rejects.toThrow("CDP connection closed");
  });

  it("close() says goodbye with a close frame and lets the socket go", async () => {
    const f = await fake(targets);
    const cdp = await connectCdp(f.wsUrl, 5000);
    await cdp.send("Target.getTargets");
    cdp.close();
    cdp.close();   // idempotent
    await f.ended;
    expect(f.closes).toBe(1);
    await expect(cdp.send("Target.getTargets")).rejects.toThrow("CDP connection closed");
  });

  it("the fit never throws over a refused or dead endpoint", async () => {
    const refused = await fake(targets, { refuseUpgrade: 403 });
    await expect(fitServedWindow(refused.wsUrl, { persona: false, timeoutMs: 2000 })).resolves.toBeNull();
    await expect(fitServedWindow("not a url", { persona: false })).resolves.toBeNull();
  });
});
