// A real Chromium with a CDP endpoint, for the cloud tests that must talk to an actual browser (the
// hosted API hands the SDK a CDP WebSocket URL; a local Chromium stands in for the hosted one).
// Which binary: CLEARCOTE_TEST_BINARY (a Clearcote build), else a Chromium Playwright has downloaded.
// Neither -> the tests skip. Mirrors sdk/python/tests/_chromium.py.
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";

export function findChromium(): string | null {
  const env = process.env.CLEARCOTE_TEST_BINARY;
  if (env && existsSync(env)) return env;
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH
    || (process.platform === "win32" ? join(homedir(), "AppData", "Local", "ms-playwright") : join(homedir(), ".cache", "ms-playwright"));
  const exe = process.platform === "win32" ? "chrome.exe" : "chrome";
  const found: string[] = [];
  try {
    for (const d of readdirSync(root).filter((n) => n.startsWith("chromium-"))) {
      for (const sub of readdirSync(join(root, d))) {
        const p = join(root, d, sub, exe);
        if (existsSync(p)) found.push(p);
      }
    }
  } catch { /* no Playwright browsers */ }
  return found.sort().reverse()[0] ?? null;
}

export class LocalChromium {
  proc?: ChildProcess;
  wsUrl = "";
  httpUrl = "";
  private udd = "";

  async start(exe = findChromium()): Promise<this> {
    if (!exe) throw new Error("no Chromium");
    this.udd = mkdtempSync(join(tmpdir(), "cc-cloud-test-"));
    const args = ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${this.udd}`, "--no-first-run",
      "--no-default-browser-check", "--disable-gpu", "about:blank"];
    if (process.platform === "linux" && process.getuid?.() === 0) args.unshift("--no-sandbox");
    this.proc = spawn(exe, args, { stdio: ["ignore", "ignore", "pipe"] });
    this.wsUrl = await new Promise<string>((resolve, reject) => {
      let buf = "";
      const timer = setTimeout(() => reject(new Error(`${exe} did not open a CDP endpoint`)), 30_000);
      this.proc!.stderr!.on("data", (c) => {
        buf += String(c);
        const m = /DevTools listening on (ws:\/\/\S+)/.exec(buf);
        if (m) {
          clearTimeout(timer);
          resolve(m[1]);
        }
      });
    });
    this.httpUrl = `http://127.0.0.1:${/:(\d+)\//.exec(this.wsUrl)![1]}`;
    return this;
  }

  alive(): boolean {
    return !!this.proc && this.proc.exitCode === null && !this.proc.killed;
  }

  async stop(): Promise<void> {
    if (this.proc && this.alive()) {
      const exited = new Promise((r) => this.proc!.once("exit", r));
      this.proc.kill("SIGTERM");
      await Promise.race([exited, new Promise((r) => setTimeout(r, 10_000))]);
    }
    // Chromium's helper processes can still be writing under the profile for a moment after exit.
    try { if (this.udd) rmSync(this.udd, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }); } catch { /* temp sweeper gets it */ }
  }
}
