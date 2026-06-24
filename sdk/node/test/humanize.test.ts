import { describe, it, expect } from "vitest";
import { installHumanize, CURSOR_OVERLAY } from "../src/humanize.js";

// a minimal stand-in: newPage/newContext are stable property functions so identity is comparable.
const fakeBrowser = () =>
  ({ newPage: async () => ({}), newContext: async () => ({}) }) as never;

describe("installHumanize", () => {
  it("is a no-op when humanize + showCursor are both off", () => {
    const b = fakeBrowser() as { newPage: unknown; newContext: unknown };
    const np = b.newPage;
    const nc = b.newContext;
    installHumanize(b as never, {});
    expect(b.newPage).toBe(np);
    expect(b.newContext).toBe(nc);
  });

  it("wraps newPage when humanize is on", () => {
    const b = fakeBrowser() as { newPage: unknown };
    const np = b.newPage;
    installHumanize(b as never, { humanize: true });
    expect(b.newPage).not.toBe(np);
  });

  it("wraps newContext when showCursor is on", () => {
    const b = fakeBrowser() as { newContext: unknown };
    const nc = b.newContext;
    installHumanize(b as never, { showCursor: true });
    expect(b.newContext).not.toBe(nc);
  });
});

describe("CURSOR_OVERLAY", () => {
  it("is an idempotent IIFE (runs as both addInitScript and page.evaluate)", () => {
    expect(CURSOR_OVERLAY).toContain("__clearcoteCursor");
    const body = CURSOR_OVERLAY.trim();
    expect(body.startsWith("(()")).toBe(true);
    expect(body.endsWith(")();")).toBe(true);
  });
});
