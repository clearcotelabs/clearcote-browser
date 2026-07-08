import { describe, it, expect } from "vitest";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, basename } from "node:path";
import { warmFiles } from "../src/download.js";
import { isWinLaunchRace, winAvRetry } from "../src/index.js";

// The Windows first-launch antivirus-scan race work-around (SDK 0.12.1): a freshly-extracted,
// unsigned chrome.exe can fail its first launch with "spawn UNKNOWN" / "side-by-side configuration
// is incorrect" while AV scans chrome_elf.dll, and Windows caches that against the path. warmFiles
// pre-scans to prevent it; winAvRetry re-scans + retries, then relaunches from a fresh copy.
describe("Windows first-launch AV race work-around", () => {
  it("warmFiles reads a tree (forcing the AV scan) without throwing", () => {
    const d = mkdtempSync(join(tmpdir(), "cc-warm-"));
    writeFileSync(join(d, "chrome.exe"), Buffer.alloc(1000));
    mkdirSync(join(d, "locales"));
    writeFileSync(join(d, "locales", "en-US.pak"), Buffer.alloc(500));
    expect(() => warmFiles(d)).not.toThrow();
    expect(() => warmFiles(join(d, "missing"))).not.toThrow(); // missing dir is a no-op
  });

  it("isWinLaunchRace classifies the race errors", () => {
    expect(isWinLaunchRace(new Error("browserType.launch: spawn UNKNOWN"))).toBe(true);
    expect(isWinLaunchRace(new Error("failed to start ... side-by-side configuration is incorrect"))).toBe(true);
    expect(isWinLaunchRace(new Error("Timeout 30000ms exceeded"))).toBe(false);
    expect(isWinLaunchRace("net::ERR_CONNECTION_REFUSED")).toBe(false);
  });

  it("winAvRetry is a pass-through off Windows (one call, no retry)", async () => {
    // process.platform is 'linux' in CI -> pass-through.
    let n = 0;
    const r = await winAvRetry(async (exe) => {
      n++;
      return `browser:${exe}`;
    }, "/x/chrome");
    expect(r).toBe("browser:/x/chrome");
    expect(n).toBe(1);
  });

  it("winAvRetry retries then recovers from a fresh copy on Windows", async () => {
    const orig = Object.getOwnPropertyDescriptor(process, "platform")!;
    Object.defineProperty(process, "platform", { value: "win32", configurable: true });
    try {
      const bdir = join(mkdtempSync(join(tmpdir(), "cc-recov-")), "browser");
      mkdirSync(bdir, { recursive: true });
      writeFileSync(join(bdir, "chrome.exe"), Buffer.from("stub"));
      const exe = join(bdir, "chrome.exe");
      const result = (await winAvRetry(async (e: string) => {
        if (e === exe) throw new Error("BrowserType.launch: spawn UNKNOWN"); // poisoned path always fails
        return { ok: true, exe: e }; // a fresh path launches cleanly
      }, exe)) as { ok: boolean; exe: string };
      expect(result.ok).toBe(true);
      expect(result.exe).not.toBe(exe); // launched from a recovered copy on a different path
      expect(basename(result.exe)).toBe("chrome.exe");
    } finally {
      Object.defineProperty(process, "platform", orig);
    }
  }, 15000);

  it("winAvRetry re-raises non-race errors immediately", async () => {
    const orig = Object.getOwnPropertyDescriptor(process, "platform")!;
    Object.defineProperty(process, "platform", { value: "win32", configurable: true });
    try {
      await expect(
        winAvRetry(async () => {
          throw new Error("Timeout 30000ms exceeded");
        }, "/x/chrome.exe"),
      ).rejects.toThrow("Timeout");
    } finally {
      Object.defineProperty(process, "platform", orig);
    }
  });
});
