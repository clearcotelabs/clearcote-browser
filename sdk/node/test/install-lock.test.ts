// Two installers of the same build at once must not download over each other: one installs, the other
// waits and uses that tree. The lock is one protocol shared with the Python and .NET SDKs (see
// download.ts): an O_EXCL lock file with an owner record, a heartbeat, and stale-lock recovery.

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, readlinkSync, utimesSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { INSTALL_LOCK, acquireInstallLock, installLockTiming, proEnsureBinary } from "../src/download.js";
import { BINARY, TAG, startFakeBuild, verifiedTree } from "./helpers/fake-build.js";
import { tempDir } from "./helpers/temp.js";

const proPlatform = process.platform === "win32" || process.platform === "linux"; // the PRO route serves these

function bootId(): string | null {
  try { return readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim() || null; } catch { return null; }
}

function pidNamespace(): string | null {
  try { return readlinkSync("/proc/self/ns/pid"); } catch { return null; }
}

const lockPath = (base: string) => path.join(base, INSTALL_LOCK);

/** A lock file as any of the three SDKs writes it; `ageMs` back-dates its heartbeat. */
function writeLock(base: string, fields: Record<string, unknown> = {}, ageMs = 0): void {
  const rec = { pid: process.pid, host: os.hostname(), boot: bootId(), pidns: pidNamespace(), nonce: "f".repeat(32), sdk: "python", created: 0, ...fields };
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

  it("takes over a lock whose heartbeat stopped (a holder elsewhere cannot be asked)", async () => {
    const base = path.join(tempDir("cc-lock-"), TAG);
    writeLock(base, { host: "another-machine", pid: 1 }, installLockTiming.staleMs + 5000);
    const lock = await acquireInstallLock(base, { timeoutMs: 10_000 });
    expect(lock.held()).toBe(true);
    await lock.release();
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
    const then = new Date(Date.now() - installLockTiming.unreadableMs - 5000);
    utimesSync(lockPath(base), then, then);
    await (await acquireInstallLock(base, { timeoutMs: 10_000 })).release();
  });

  it("clears a breaker left by a crashed process", async () => {
    const base = path.join(tempDir("cc-lock-"), TAG);
    writeLock(base, { host: "another-machine" }, installLockTiming.staleMs + 5000);
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
    writeLock(base, { host: "another-machine" }, installLockTiming.staleMs + 5000); // its installer died
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
});
