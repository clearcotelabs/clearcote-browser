// Separate processes installing one build at once, through the public PRO download path against a local
// fake of the download route (a small fake archive, no real browser). Field failure this guards: right after
// a new PRO build shipped, a Python server and a Node gateway on one host both found it missing and installed
// into the same folder at the same time; the gateway's removal of browser\ hit EBUSY on chrome.dll, which the
// other process was already running, and the gateway died at startup.
//
// The lock is shared with the Python and .NET SDKs, so the second test plays the other SDK's part by writing
// the lock file exactly as they do.

import { mkdirSync, readFileSync, readdirSync, readlinkSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { TAG, startFakeBuild, verifiedTree } from "./helpers/fake-build.js";
import { startInstallChild } from "./helpers/install-child.js";
import { tempDir } from "./helpers/temp.js";

const proPlatform = process.platform === "win32" || process.platform === "linux"; // the PRO route serves these

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

function bootId(): string | null {
  try { return readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim() || null; } catch { return null; }
}

function pidNamespace(): string | null {
  try { return readlinkSync("/proc/self/ns/pid"); } catch { return null; }
}

describe.runIf(proPlatform)("install lock across processes", () => {
  it("two processes install one build once", async () => {
    const cache = tempDir("cc-lockp-");
    const srv = await startFakeBuild({ metaBarrier: 2, archiveDelayMs: 1000 }); // both reach the cache check together
    try {
      const results = await Promise.all([startInstallChild(srv.url, cache), startInstallChild(srv.url, cache)]);
      for (const r of results) expect(r.code, r.err).toBe(0);
      expect(results[0].path).toBeTruthy();
      expect(results[1].path).toBe(results[0].path);
      expect(srv.archiveHits).toBe(1); // one download; the other process waited and used it
      expect(readdirSync(path.join(cache, TAG)).sort()).toEqual([".manifest.json", ".verified", "browser"]); // no lock or temp left
    } finally {
      await srv.close();
    }
  }, 180_000);

  it("waits for another SDK's install and uses it", async () => {
    const cache = tempDir("cc-lockp-");
    const base = path.join(cache, TAG);
    mkdirSync(base, { recursive: true });
    const lock = path.join(base, ".install-lock");
    // what the Python / .NET SDK writes; this process plays it
    writeFileSync(lock, JSON.stringify({ pid: process.pid, start: startMarker(), host: os.hostname(), boot: bootId(), pidns: pidNamespace(), nonce: "a".repeat(32), sdk: "python", created: Date.now() }));
    const srv = await startFakeBuild({ archiveDelayMs: 500 });
    try {
      const child = startInstallChild(srv.url, cache);
      await srv.metaSeen;
      await new Promise((r) => setTimeout(r, 1000)); // the child has found the build missing by now
      const exe = verifiedTree(base); // the other SDK finishes its install...
      rmSync(lock); // ...and releases the lock
      const r = await child;
      expect(r.code, r.err).toBe(0);
      expect(r.path).toBe(exe);
      expect(srv.archiveHits).toBe(0); // it waited instead of downloading over the other install
      expect(readdirSync(base).sort()).toEqual([".manifest.json", ".verified", "browser"]);
    } finally {
      await srv.close();
    }
  }, 180_000);
});
