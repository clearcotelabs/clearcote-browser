import { describe, it, expect } from "vitest";
import { serve, Server } from "../src/index.js";

describe("serve (raw CDP endpoint)", () => {
  it("exports serve() + Server", () => {
    expect(typeof serve).toBe("function");
    expect(typeof Server).toBe("function");
  });

  it("Server.cdpUrl composes the endpoint URL", () => {
    // minimal stub proc; we only exercise the URL getter + isAlive
    const fakeProc = { exitCode: null, killed: false } as unknown as import("node:child_process").ChildProcess;
    const s = new Server(fakeProc, "127.0.0.1", 9222, "/tmp/x", false);
    expect(s.cdpUrl).toBe("http://127.0.0.1:9222");
    expect(s.isAlive()).toBe(true);
  });

  it.skipIf(!process.env.CLEARCOTE_TEST_BINARY)(
    "serves a stealthy CDP endpoint (webdriver stays false)",
    async () => {
      const srv = await serve({
        executablePath: process.env.CLEARCOTE_TEST_BINARY,
        headless: true, quiet: true, fingerprint: "t", platform: "windows",
        args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
      } as never);
      try {
        const j = (await (await fetch(srv.cdpUrl + "/json/version")).json()) as { Browser?: string; webSocketDebuggerUrl?: string };
        expect(j.Browser ?? "").toContain("Chrome");
        expect(j.webSocketDebuggerUrl).toBeTruthy();
      } finally {
        await srv.close();
      }
    },
    60000,
  );
});
