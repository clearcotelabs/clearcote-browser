// A real Chromium with a CDP endpoint, for the cloud tests that must talk to an actual browser (the
// hosted API hands the SDK a CDP WebSocket URL; a local Chromium stands in for the hosted one).
// Which binary: CLEARCOTE_TEST_BINARY (a Clearcote build), else a Chromium Playwright has downloaded.
// Neither -> the tests skip. Mirrors sdk/python/tests/_chromium.py.
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
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
    // The browser's own temp files go inside its profile, so stop() takes them too: its singleton-socket
    // directory on Linux (orphaned whenever the browser is killed) and its component-updater downloads on
    // Windows (chromiumcrx_*) would otherwise be left in the machine's temp directory.
    const temp = join(this.udd, "tmp");
    mkdirSync(temp);
    const env = { ...process.env, TMPDIR: temp, TMP: temp, TEMP: temp };
    // Its own process group on Linux/macOS, so stop() can end the helper processes along with it.
    this.proc = spawn(exe, args, { stdio: ["ignore", "ignore", "pipe"], env, detached: process.platform !== "win32" });
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
    const proc = this.proc;
    const group = process.platform !== "win32" && proc?.pid !== undefined ? -proc.pid : null;
    if (proc && this.alive()) {
      const exited = new Promise((r) => proc.once("exit", r));
      // The whole process group on Linux/macOS: the browser exits within ~50 ms of a SIGTERM, but its helper
      // processes went on writing Default/ for 10+ s, so the delete below kept failing (ENOTEMPTY) and left
      // the profile behind in 2-3 of 7 runs.
      if (group !== null) {
        try { process.kill(group, "SIGKILL"); } catch { /* already gone */ }
      } else {
        proc.kill("SIGTERM");
      }
      await Promise.race([exited, new Promise((r) => setTimeout(r, 10_000))]);
    }
    if (group !== null) {
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline) {
        try { process.kill(group, 0); } catch { break; } // ESRCH: nobody left in the group
        await new Promise((r) => setTimeout(r, 50));
      }
    }
    // On Windows the browser can still hold handles under the profile for a moment after exit.
    try { if (this.udd) rmSync(this.udd, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }); } catch { /* temp sweeper gets it */ }
  }
}
