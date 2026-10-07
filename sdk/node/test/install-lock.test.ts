// Two installers of the same build at once must not download over each other: one installs, the other
// waits and uses that tree. The lock is one protocol shared with the Python and .NET SDKs (see
// download.ts): an O_EXCL lock file with an owner record, a heartbeat, and stale-lock recovery.

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, readlinkSync, rmSync, statSync, utimesSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { INSTALL_LOCK, acquireInstallLock, installLockTiming, installTestHooks, proEnsureBinary } from "../src/download.js";
import { BINARY, TAG, startFakeBuild, verifiedTree } from "./helpers/fake-build.js";
import { tempDir } from "./helpers/temp.js";

const proPlatform = process.platform === "win32" || process.platform === "linux"; // the PRO route serves these

function bootId(): string | null {
  try { return readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim() || null; } catch { return null; }
}

function pidNamespace(): string | null {
  try { return readlinkSync("/proc/self/ns/pid"); } catch { return null; }
}


/** This process's start marker as the SDK records it (Linux), else null. */
function startMarker(): string | null {
  try {
    const stat = readFileSync("/proc/self/stat", "utf8");
    const f = stat.slice(stat.lastIndexOf(")") + 2).split(/\s+/);
    return f[19] ? `linux:${f[19]}` : null;
  } catch {
    return null;
  }
}

const lockPath = (base: string) => path.join(base, INSTALL_LOCK);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const timing = { ...installLockTiming };
afterEach(() => {
  Object.assign(installLockTiming, timing);
  if (installTestHooks) {
    delete installTestHooks.beforeVerified;
    delete (installTestHooks as Record<string, unknown>).heartbeatWorkerSource;
  }
});

/** A lock file as any of the three SDKs writes it; `ageMs` back-dates its heartbeat. */
function writeLock(base: string, fields: Record<string, unknown> = {}, ageMs = 0): void {
  const rec = {
    pid: process.pid, start: startMarker(), host: os.hostname(), boot: bootId(), pidns: pidNamespace(), nonce: "f".repeat(32), sdk: "python",
    created: Date.now(), ...fields,
  };
  mkdirSync(base, { recursive: true });
  writeFileSync(lockPath(base), JSON.stringify(rec));
  if (ageMs) {
    const then = new Date(Date.now() - ageMs);
    utimesSync(lockPath(base), then, then);
  }
}

describe("install lock", () => {
  it("writes an owner record and removes it on release", async () => {
    const base = path.join(tempDir("cc-lock-"), TAG);
    const lock = await acquireInstallLock(base, { timeoutMs: 1000 });
    const rec = JSON.parse(readFileSync(lockPath(base), "utf8"));
    expect([rec.pid, rec.nonce, rec.sdk, rec.host]).toEqual([process.pid, lock.nonce, "node", os.hostname()]);
    expect(lock.held()).toBe(true);
    await lock.release();
    expect(readdirSync(base)).toEqual([]); // no lock file, no breaker
    await (await acquireInstallLock(base, { timeoutMs: 1000 })).release(); // free again
  });

  it("makes the next installer wait, then give up with a plain message", async () => {
    const base = path.join(tempDir("cc-lock-"), TAG);
    const lock = await acquireInstallLock(base, { timeoutMs: 1000 });
    try {
      const started = Date.now();
      const err = await acquireInstallLock(base, { timeoutMs: 600 }).then(() => null, (e: Error) => e);
      expect(Date.now() - started).toBeGreaterThanOrEqual(500); // it waited
      expect(err?.message).toMatch(/^Gave up after 1 second waiting for another program to finish installing/);
      expect(err?.message).toContain(`process ${process.pid} on ${os.hostname()} (Clearcote Node SDK)`);
      expect(err?.message).toContain(lockPath(base)); // what to delete if nothing else is installing
    } finally {
      await lock.release();
    }
  });

  it("takes over a lock left by a dead process on this machine at once", async () => {
    const gone = spawnSync(process.execPath, ["-e", ""]).pid as number;
    const base = path.join(tempDir("cc-lock-"), TAG);
    writeLock(base, { pid: gone }); // fresh heartbeat: only the pid tells it is dead
    const started = Date.now();
    const lock = await acquireInstallLock(base, { timeoutMs: 10_000 });
    expect(Date.now() - started).toBeLessThan(3000);
    expect(lock.held()).toBe(true);
    await lock.release();
    expect(readdirSync(base)).toEqual([]);
  });

  it("takes over a lock from elsewhere that stops changing (timed by the waiter's own clock)", async () => {
    installLockTiming.staleMs = 1000;
    const base = path.join(tempDir("cc-lock-"), TAG);
    writeLock(base, { host: "another-machine", pid: 1 });
    const started = Date.now();
    const lock = await acquireInstallLock(base, { timeoutMs: 10_000 });
    expect(Date.now() - started).toBeGreaterThanOrEqual(900); // watched it stay unchanged first
    expect(lock.held()).toBe(true);
    await lock.release();
  });

  it("does not break a lock from elsewhere that keeps changing, whatever its clock says", async () => {
    installLockTiming.staleMs = 1000;
    installLockTiming.unreadableMs = 1000;
    const base = path.join(tempDir("cc-lock-"), TAG);
    for (const offset of [-3_600_000, 3_600_000]) { // its clock an hour behind ours, then an hour ahead
      writeLock(base, { host: "another-machine", pid: 1 });
      let i = 0;
      const toucher = setInterval(() => {
        const then = new Date(Date.now() + offset + ++i * 10);
        try { utimesSync(lockPath(base), then, then); } catch { /* next time */ }
      }, 100);
      try {
        await expect(acquireInstallLock(base, { timeoutMs: 2500 })).rejects.toThrow("Gave up");
      } finally {
        clearInterval(toucher);
      }
      expect(JSON.parse(readFileSync(lockPath(base), "utf8")).nonce).toBe("f".repeat(32)); // not broken
    }
    const started = Date.now();
    await (await acquireInstallLock(base, { timeoutMs: 10_000 })).release(); // once it stops changing
    expect(Date.now() - started).toBeGreaterThanOrEqual(900);
  }, 30_000);

  it.runIf(process.platform === "linux")("never takes over the lock of a live process here, however old its heartbeat", async () => {
    // Its heartbeat can stop while it is alive: a debugger, a paused container, a laptop asleep.
    installLockTiming.staleMs = 1000;
    const base = path.join(tempDir("cc-lock-"), TAG);
    writeLock(base, {}, 600_000); // this very process: alive
    const err = await acquireInstallLock(base, { timeoutMs: 2000 }).then(() => null, (e: Error) => e);
    expect(err?.message).toContain(`process ${process.pid} on ${os.hostname()} (Clearcote Python SDK), which is still running`);
    expect(JSON.parse(readFileSync(lockPath(base), "utf8")).nonce).toBe("f".repeat(32)); // not broken
  });

  it.runIf(process.platform === "linux")("takes over at once a lock whose pid now belongs to another process", async () => {
    const base = path.join(tempDir("cc-lock-"), TAG);
    writeLock(base, { start: "linux:1" }); // our pid, but a process that started at another time
    const started = Date.now();
    await (await acquireInstallLock(base, { timeoutMs: 10_000 })).release();
    expect(Date.now() - started).toBeLessThan(3000);
  });

  it("keeps the heartbeat going while the event loop is blocked", async () => {
    installLockTiming.heartbeatMs = 100;
    const base = path.join(tempDir("cc-lock-"), TAG);
    const lock = await acquireInstallLock(base, { timeoutMs: 1000 });
    try {
      await sleep(300);
      const before = statSync(lockPath(base)).mtimeMs;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1500); // a long synchronous install step
      expect(statSync(lockPath(base)).mtimeMs).toBeGreaterThan(before + 500);
    } finally {
      await lock.release();
    }
  });

  it("respects a live lock from another machine", async () => {
    const base = path.join(tempDir("cc-lock-"), TAG);
    writeLock(base, { host: "another-machine", pid: 1, sdk: "dotnet" }, 5000);
    await expect(acquireInstallLock(base, { timeoutMs: 600 })).rejects.toThrow("process 1 on another-machine (Clearcote .NET SDK)");
    expect(JSON.parse(readFileSync(lockPath(base), "utf8")).host).toBe("another-machine"); // not broken
  });

  it("respects the lock of a live process here", async () => {
    const base = path.join(tempDir("cc-lock-"), TAG);
    writeLock(base); // this very process: alive
    await expect(acquireInstallLock(base, { timeoutMs: 600 })).rejects.toThrow("Clearcote Python SDK");
  });

  it("takes over a half-written lock only after its grace period", async () => {
    const base = path.join(tempDir("cc-lock-"), TAG);
    mkdirSync(base, { recursive: true });
    writeFileSync(lockPath(base), ""); // created, owner record not written yet
    await expect(acquireInstallLock(base, { timeoutMs: 600 })).rejects.toThrow("a process that left no details");
    installLockTiming.unreadableMs = 1000;
    await (await acquireInstallLock(base, { timeoutMs: 10_000 })).release();
  });

  it("clears a breaker left by a crashed process", async () => {
    installLockTiming.staleMs = 1000;
    installLockTiming.breakerStaleMs = 1000;
    const base = path.join(tempDir("cc-lock-"), TAG);
    writeLock(base, { host: "another-machine" });
    const breaker = `${lockPath(base)}.break`;
    writeFileSync(breaker, "");
    const then = new Date(Date.now() - installLockTiming.breakerStaleMs - 5000);
    utimesSync(breaker, then, then);
    await (await acquireInstallLock(base, { timeoutMs: 10_000 })).release();
    expect(readdirSync(base)).toEqual([]);
  });

  it("never deletes the lock of the process that took over from it", async () => {
    const base = path.join(tempDir("cc-lock-"), TAG);
    const lock = await acquireInstallLock(base, { timeoutMs: 1000 });
    writeLock(base, { nonce: "e".repeat(32) }); // another process judged ours stale and took over
    expect(lock.held()).toBe(false);
    await lock.release();
    expect(JSON.parse(readFileSync(lockPath(base), "utf8")).nonce).toBe("e".repeat(32));
  });
});

describe("install lock: process numbers, restarts, breakers and the heartbeat", () => {
  it("takes over at once a lock written before this machine started", async () => {
    // Its process number may belong to an unrelated program since the restart.
    const base = path.join(tempDir("cc-lock-"), TAG);
    writeLock(base, { created: 0 }); // our pid, alive -- but the record is older than this boot
    const started = Date.now();
    await (await acquireInstallLock(base, { timeoutMs: 3000 })).release();
    expect(Date.now() - started).toBeLessThan(2000);
  });

  it("judges a live pid it cannot confirm by the waiter's own clock, and names it with care", async () => {
    // Node cannot read another process's start time on Windows, and this record carries none: the pid may
    // have been reused by an unrelated program, so the lock is taken over once it stops changing.
    installLockTiming.staleMs = 1000;
    const base = path.join(tempDir("cc-lock-"), TAG);
    writeLock(base, { start: null }); // our pid, alive
    const err = await acquireInstallLock(base, { timeoutMs: 600 }).then(() => null, (e: Error) => e);
    expect(err?.message).toContain(`process ${process.pid} on ${os.hostname()} (Clearcote Python SDK); that process number may now belong to another program`);
    expect(err?.message).toContain("If no Clearcote program is installing this build, delete this file");
    const started = Date.now();
    await (await acquireInstallLock(base, { timeoutMs: 10_000 })).release();
    expect(Date.now() - started).toBeGreaterThanOrEqual(900);
  });

  it("removes a breaker that looks old only after it stays unchanged for the breaker window", async () => {
    installLockTiming.staleMs = 500;
    installLockTiming.breakerStaleMs = 1500;
    const base = path.join(tempDir("cc-lock-"), TAG);
    writeLock(base, { host: "another-machine", pid: 1 });
    const breaker = `${lockPath(base)}.break`;
    writeFileSync(breaker, "");
    const then = new Date(Date.now() - 3_600_000); // an hour old by its timestamp: that alone proves nothing
    utimesSync(breaker, then, then);
    const started = Date.now();
    await (await acquireInstallLock(base, { timeoutMs: 10_000 })).release();
    expect(Date.now() - started).toBeGreaterThanOrEqual(1400);
    expect(readdirSync(base)).toEqual([]);
  });

  it("keeps beating from the main thread when the heartbeat worker dies", async () => {
    installLockTiming.heartbeatMs = 100;
    (installTestHooks as Record<string, unknown>).heartbeatWorkerSource =
      'require("node:fs").writeFileSync(require("node:worker_threads").workerData.path + ".died", ""); throw new Error("heartbeat worker died");';
    const base = path.join(tempDir("cc-lock-"), TAG);
    const lock = await acquireInstallLock(base, { timeoutMs: 1000 });
    try {
      await sleep(400);
      expect(existsSync(`${lockPath(base)}.died`)).toBe(true); // the worker ran and died
      const before = statSync(lockPath(base)).mtimeMs;
      await sleep(500);
      expect(statSync(lockPath(base)).mtimeMs).toBeGreaterThan(before);
    } finally {
      await lock.release();
    }
  });
});

describe.runIf(proPlatform)("installs under the install lock", () => {
  it("waits for the holder, then uses its build without downloading", async () => {
    const cache = tempDir("cc-lock-");
    const base = path.join(cache, TAG);
    const lock = await acquireInstallLock(base, { timeoutMs: 1000 }); // another installer is busy with this build
    const srv = await startFakeBuild();
    try {
      const install = proEnsureBinary("test-key", { apiBase: srv.url, cacheDir: cache, quiet: true });
      await srv.metaSeen;
      await new Promise((r) => setTimeout(r, 700)); // the installer is waiting on the lock by now
      const exe = verifiedTree(base); // the holder finishes...
      await lock.release(); // ...and lets go
      expect(await install).toBe(exe);
      expect(srv.archiveHits).toBe(0); // used the holder's build: no second download
      expect(readdirSync(base).sort()).toEqual([".manifest.json", ".verified", "browser"]);
    } finally {
      await srv.close();
    }
  }, 30_000);

  it("leaves nothing behind after a failed install", async () => {
    // The browser binary fails its own hash check, after the archive was extracted: no lock, no temp
    // files, and no unverified tree at browser/ for anything to pick up.
    const cache = tempDir("cc-lock-");
    const srv = await startFakeBuild({ exeSha: "0".repeat(64) });
    try {
      await expect(proEnsureBinary("test-key", { apiBase: srv.url, cacheDir: cache, quiet: true })).rejects.toThrow(`${BINARY} SHA-256 mismatch`);
      expect(readdirSync(path.join(cache, TAG))).toEqual([]);
    } finally {
      await srv.close();
    }
  }, 30_000);

  it("clears what an install that stopped part-way left behind", async () => {
    const cache = tempDir("cc-lock-");
    const base = path.join(cache, TAG);
    for (const leftover of [".tmp-0123", ".incoming", path.join("browser", "half")]) mkdirSync(path.join(base, leftover), { recursive: true });
    installLockTiming.staleMs = 1000;
    writeLock(base, { host: "another-machine" }); // its installer died
    const srv = await startFakeBuild();
    try {
      const exe = await proEnsureBinary("test-key", { apiBase: srv.url, cacheDir: cache, quiet: true });
      expect(existsSync(exe)).toBe(true);
      expect(readdirSync(base).sort()).toEqual([".manifest.json", ".verified", "browser"]);
      expect(existsSync(path.join(base, "browser", "half"))).toBe(false);
    } finally {
      await srv.close();
    }
  }, 30_000);

  it("uses a verified build that appeared during the install and leaves it as it is", async () => {
    // Another installer finished this build after this one's cache check: the tree it marked verified may
    // already be running, so it is used, never moved.
    const cache = tempDir("cc-lock-");
    const base = path.join(cache, TAG);
    const finished: string[] = [];
    const srv = await startFakeBuild({ onArchive: () => { finished.push(verifiedTree(base)); } });
    try {
      const exe = await proEnsureBinary("test-key", { apiBase: srv.url, cacheDir: cache, quiet: true });
      expect(exe).toBe(finished[0]);
      expect(readFileSync(path.join(base, ".verified"), "utf8")).toBe(`${"0".repeat(64)}\n`); // its marker
      expect(readFileSync(exe, "utf8")).toBe("y".repeat(64)); // its files, not replaced by ours
      expect(readdirSync(base).sort()).toEqual([".manifest.json", ".verified", "browser"]);
    } finally {
      await srv.close();
    }
  }, 30_000);

  it("never marks its tree verified once it lost the lock", async () => {
    const cache = tempDir("cc-lock-");
    const base = path.join(cache, TAG);
    let taken = false;
    installTestHooks.beforeVerified = () => { // another process takes the lock over right after the tree is in place
      if (!taken) writeLock(base, { nonce: "e".repeat(32), sdk: "node" });
      taken = true;
    };
    const srv = await startFakeBuild();
    try {
      const install = proEnsureBinary("test-key", { apiBase: srv.url, cacheDir: cache, quiet: true });
      for (let i = 0; i < 300 && !taken; i++) await sleep(50);
      expect(taken).toBe(true);
      await sleep(700);
      const verifiedWhileLost = existsSync(path.join(base, ".verified"));
      const nonce = JSON.parse(readFileSync(lockPath(base), "utf8")).nonce;
      rmSync(lockPath(base), { force: true }); // the other process lets go without installing
      const exe = await install;
      expect(verifiedWhileLost).toBe(false); // never marked verified without the lock
      expect(nonce).toBe("e".repeat(32)); // and the new holder's lock was left alone
      expect(existsSync(exe)).toBe(true);
      expect(srv.archiveHits).toBe(2); // it waited, then installed under the lock again
      expect(readdirSync(base).sort()).toEqual([".manifest.json", ".verified", "browser"]);
    } finally {
      await srv.close();
    }
  }, 30_000);

  it("clears a breaker and trash a hard kill left behind", async () => {
    installLockTiming.breakerStaleMs = 300; // the install below takes longer than this
    const cache = tempDir("cc-lock-");
    const base = path.join(cache, TAG);
    mkdirSync(path.join(base, ".trash-0123", "locales"), { recursive: true });
    const breaker = `${lockPath(base)}.break`;
    writeFileSync(breaker, ""); // killed between deleting a stale lock and deleting its breaker
    const then = new Date(Date.now() - 60_000);
    utimesSync(breaker, then, then);
    const srv = await startFakeBuild({ archiveDelayMs: 600 });
    try {
      await proEnsureBinary("test-key", { apiBase: srv.url, cacheDir: cache, quiet: true });
      expect(readdirSync(base).sort()).toEqual([".manifest.json", ".verified", "browser"]);
    } finally {
      await srv.close();
    }
  }, 30_000);

  it.each(["binary removed", "browser folder removed"])("installs again a verified build whose browser is gone (%s)", async (damage) => {
    // Antivirus quarantined chrome.exe, or someone deleted browser/ by hand, and .verified stayed behind: the
    // next install repairs it with one download instead of giving up.
    const cache = tempDir("cc-lock-");
    const base = path.join(cache, TAG);
    const exe = verifiedTree(base);
    if (damage === "binary removed") rmSync(exe);
    else rmSync(path.dirname(exe), { recursive: true });
    const srv = await startFakeBuild();
    try {
      const got = await proEnsureBinary("test-key", { apiBase: srv.url, cacheDir: cache, quiet: true });
      expect(existsSync(got)).toBe(true);
      expect(srv.archiveHits).toBe(1);
      expect(readFileSync(path.join(base, ".verified"), "utf8")).toBe(`${srv.sha}\n`);
      expect(readdirSync(base).sort()).toEqual([".manifest.json", ".verified", "browser"]);
    } finally {
      await srv.close();
    }
  }, 30_000);

  it("leaves alone a breaker that changes during the install", async () => {
    // Its timestamp may come from a machine whose clock is far behind: only one that stayed exactly the same
    // for the breaker window, by this process's own clock, is removed.
    const cache = tempDir("cc-lock-");
    const base = path.join(cache, TAG);
    mkdirSync(base, { recursive: true });
    const breaker = `${lockPath(base)}.break`;
    writeFileSync(breaker, "");
    const then = new Date(Date.now() - 3_600_000);
    utimesSync(breaker, then, then);
    const srv = await startFakeBuild({
      onArchive: () => {
        const later = new Date(Date.now() - 7_200_000);
        try { utimesSync(breaker, later, later); } catch { /* removed already */ }
      },
    });
    try {
      await proEnsureBinary("test-key", { apiBase: srv.url, cacheDir: cache, quiet: true });
      expect(existsSync(breaker)).toBe(true);
    } finally {
      await srv.close();
    }
  }, 30_000);
});
