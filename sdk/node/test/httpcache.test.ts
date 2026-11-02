// src/httpcache.ts — httpCacheDir: one locked slot per concurrent launch, stale locks taken over, refused off this
// machine. Mirrors sdk/python/tests/test_httpcache.py and sdk/dotnet/tests/Clearcote.Tests/HttpCacheTests.cs.
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { acquireHttpCacheSlot, checkHttpCacheDir, withHttpCache, HTTP_CACHE_SLOTS } from "../src/httpcache.js";
import { launch, sessionBody } from "../src/index.js";
import { containerEnv } from "../src/docker.js";

let root: string;
beforeEach(() => { root = mkdtempSync(join(tmpdir(), "cc-httpcache-")); });
afterEach(() => rmSync(root, { recursive: true, force: true }));

describe("acquireHttpCacheSlot", () => {
  it("takes slot-0 and marks it with this pid", () => {
    const s = acquireHttpCacheSlot(root)!;
    expect(s.dir).toBe(join(root, "slot-0"));
    expect(existsSync(s.dir)).toBe(true);
    expect(readFileSync(join(root, "slot-0.lock"), "utf8")).toBe(String(process.pid));
    s.release();
    expect(existsSync(join(root, "slot-0.lock"))).toBe(false);
    expect(existsSync(s.dir)).toBe(true); // the cache itself stays for the next run
  });

  it("gives launches running at the same time different slots, and reuses a released one", () => {
    const a = acquireHttpCacheSlot(root)!;
    const b = acquireHttpCacheSlot(root)!;
    const c = acquireHttpCacheSlot(root)!;
    expect([a.dir, b.dir, c.dir]).toEqual([0, 1, 2].map((n) => join(root, `slot-${n}`)));
    b.release();
    expect(acquireHttpCacheSlot(root)!.dir).toBe(join(root, "slot-1"));
    a.release(); c.release();
  });

  it("takes over the lock of a process that is gone", () => {
    writeFileSync(join(root, "slot-0.lock"), "999999999");
    writeFileSync(join(root, "slot-1.lock"), "not a pid");
    const s = acquireHttpCacheSlot(root)!;
    expect(s.dir).toBe(join(root, "slot-0"));
    expect(readFileSync(join(root, "slot-0.lock"), "utf8")).toBe(String(process.pid));
    s.release();
  });

  it("leaves a live process's lock alone, and never removes a lock it no longer owns", () => {
    writeFileSync(join(root, "slot-0.lock"), String(process.ppid));
    const s = acquireHttpCacheSlot(root)!;
    expect(s.dir).toBe(join(root, "slot-1"));
    writeFileSync(join(root, "slot-1.lock"), String(process.ppid)); // taken over by someone else meanwhile
    s.release();
    expect(readFileSync(join(root, "slot-1.lock"), "utf8")).toBe(String(process.ppid));
  });

  it("returns null when every slot is held", () => {
    for (let n = 0; n < HTTP_CACHE_SLOTS; n++) writeFileSync(join(root, `slot-${n}.lock`), String(process.ppid));
    expect(acquireHttpCacheSlot(root, true)).toBeNull();
  });

  it("creates a folder that does not exist yet", () => {
    const deep = join(root, "a", "b");
    const s = acquireHttpCacheSlot(deep)!;
    expect(s.dir).toBe(join(deep, "slot-0"));
    s.release();
  });
});

describe("withHttpCache", () => {
  it("drops the option and appends --disk-cache-dir after the caller's args", () => {
    const { options, release } = withHttpCache({ httpCacheDir: root, args: ["--foo"], headless: true });
    expect(options).toEqual({ args: ["--foo", `--disk-cache-dir=${join(root, "slot-0")}`], headless: true });
    expect("httpCacheDir" in options).toBe(false);
    release();
    expect(existsSync(join(root, "slot-0.lock"))).toBe(false);
  });

  it("is a no-op without the option", () => {
    const opts = { args: ["--foo"] };
    const { options } = withHttpCache(opts);
    expect(options).toEqual(opts);
  });

  it("refuses a non-string and an explicit --disk-cache-dir", () => {
    expect(() => checkHttpCacheDir(5, [])).toThrow(TypeError);
    expect(() => checkHttpCacheDir("  ", [])).toThrow(TypeError);
    expect(() => withHttpCache({ httpCacheDir: root, args: ["--disk-cache-dir=/x"] })).toThrow(/cannot be combined/);
    expect(existsSync(join(root, "slot-0.lock"))).toBe(false); // refused before taking a slot
  });
});

describe("httpCacheDir without a profile", () => {
  it("an incognito launch (ephemeralProfile: false) refuses it before anything starts", async () => {
    await expect(launch({ cloud: false, docker: false, ephemeralProfile: false, httpCacheDir: root, executablePath: "/nonexistent/chrome" }))
      .rejects.toThrow(/httpCacheDir needs a profile/);
    expect(existsSync(join(root, "slot-0.lock"))).toBe(false);
  });
});

describe("httpCacheDir off this machine", () => {
  it("cloud refuses it", () => {
    expect(() => sessionBody({ httpCacheDir: "/tmp/c" })).toThrow("httpCacheDir is not available for cloud browsers");
  });
  it("Docker refuses it", () => {
    expect(() => containerEnv({ httpCacheDir: "/tmp/c" })).toThrow(/httpCacheDir is not available when launch\(\) runs Clearcote in Docker/);
  });
});
