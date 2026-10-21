// The ~/.clearcote files this SDK shares with the Python (and .NET) SDK: each must be readable whichever SDK
// wrote it, and replacing one must not silently lose the newer content on Windows. Mirrors the Python side's
// host.json / local-index.json / grace-file tests. Hermetic: temp dirs only, no browser, no network.
import { describe, it, expect, afterAll } from "vitest";
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// PROFILE_DIR is read at module load, so the env must be set before the imports below.
const root = mkdtempSync(join(tmpdir(), "cc-cacheparity-"));
process.env.CLEARCOTE_PROFILE_DIR = join(root, "profiles");

const { readCachedHost, writeCachedHost, hostExeKey } = await import("../src/profilesource.js");
const { loadLocalIndex } = await import("../src/profileauto.js");
const { writeFileAtomic } = await import("../src/license.js");

afterAll(() => rmSync(root, { recursive: true, force: true }));

const hostJson = () => join(root, "profiles", "library", "host.json");
const FACTS = { os_family: "windows", gpu_vendor: "intel", screen_width: 1920, screen_height: 1080 } as never;

function fakeExe(name: string): string {
  const p = join(root, name);
  writeFileSync(p, "");
  return p;
}

describe("host.json exe key matches the Python SDK", () => {
  it("is normcase(realpath(exe)): lower-case backslashes on Windows, the resolved path elsewhere", () => {
    const exe = fakeExe("Chrome-Key.exe");
    const real = realpathSync.native(exe);
    expect(hostExeKey(exe)).toBe(process.platform === "win32" ? real.toLowerCase() : real);
  });

  it("one entry serves every spelling of the same binary", () => {
    const exe = fakeExe("Chrome-Spell.exe");
    writeCachedHost(exe, FACTS);
    const other = process.platform === "win32" ? exe.toUpperCase().replace(/\\/g, "/") : join(root, ".", "Chrome-Spell.exe");
    expect(readCachedHost(other)).not.toBeNull();
    expect(readCachedHost(fakeExe("Other.exe"))).toBeNull();
  });

  it("resolves a symlink to its target", (ctx) => {
    const exe = fakeExe("Chrome-Target.exe");
    const link = join(root, "chrome-link.exe");
    try {
      symlinkSync(exe, link, "file");
    } catch {
      ctx.skip(); // Windows without the symlink privilege
    }
    writeCachedHost(link, FACTS);
    expect(readCachedHost(exe)).not.toBeNull();
  });

  it("still reads an entry an older Node SDK keyed by the literal path", () => {
    const exe = fakeExe("Chrome-Legacy.exe");
    writeFileSync(hostJson(), JSON.stringify({ ...(FACTS as object), _exe: exe, _at: new Date().toISOString() }));
    expect(readCachedHost(exe)).not.toBeNull();
  });
});

describe("host.json _at in both formats", () => {
  const exe = fakeExe("Chrome-At.exe");
  const write = (at: unknown) => writeFileSync(hostJson(), JSON.stringify({ ...(FACTS as object), _exe: hostExeKey(exe), _at: at }));

  it("reads the ISO time this SDK writes", () => {
    write(new Date().toISOString());
    expect(readCachedHost(exe)).not.toBeNull();
    write(new Date(Date.now() - 31 * 86_400_000).toISOString());
    expect(readCachedHost(exe)).toBeNull();
  });

  it("reads the epoch seconds the Python and .NET SDKs write (and expires them)", () => {
    write(Date.now() / 1000);
    expect(readCachedHost(exe)).not.toBeNull();
    // Before: Date.parse(number) was NaN, so a Python-written entry never expired.
    write(Date.now() / 1000 - 31 * 86_400);
    expect(readCachedHost(exe)).toBeNull();
  });

  it("treats a missing or garbage _at as expired", () => {
    write(undefined);
    expect(readCachedHost(exe)).toBeNull();
    write("not a time");
    expect(readCachedHost(exe)).toBeNull();
  });
});

describe("local-index.json mtime in both formats", () => {
  const dir = join(root, "corpus");
  mkdirSync(dir, { recursive: true });
  const cache = () => join(root, "profiles", "library", "local-index.json");
  const SENTINEL = [{ id: "cached-not-reindexed" }];

  it("reuses an index the Python SDK wrote with `mtime` in seconds", async () => {
    const { statSync } = await import("node:fs");
    const st = statSync(dir, { bigint: true });
    // Python writes os.stat().st_mtime: float seconds.
    writeFileSync(cache(), JSON.stringify({ dir, mtime: Number(st.mtimeNs) / 1e9, index: SENTINEL }));
    expect(loadLocalIndex(dir)).toEqual(SENTINEL);
  });

  it("reuses an index written with `mtimeMs`, and re-indexes when the directory changed", async () => {
    const { statSync } = await import("node:fs");
    writeFileSync(cache(), JSON.stringify({ dir, mtimeMs: statSync(dir).mtimeMs, index: SENTINEL }));
    expect(loadLocalIndex(dir)).toEqual(SENTINEL);
    writeFileSync(cache(), JSON.stringify({ dir, mtimeMs: statSync(dir).mtimeMs - 5000, index: SENTINEL }));
    expect(loadLocalIndex(dir)).toEqual([]);
  });

  it("writes both fields, so an older Python SDK reuses it too", () => {
    writeFileSync(cache(), "{}");
    loadLocalIndex(dir);
    const c = JSON.parse(readFileSync(cache(), "utf8"));
    expect(typeof c.mtimeMs).toBe("number");
    expect(c.mtime).toBeCloseTo(c.mtimeMs / 1000, 9);
  });
});

describe("writeFileAtomic (lease and grace files)", () => {
  const busy = (code: string) => Object.assign(new Error(code), { code });

  it("retries a rename Windows refuses while a reader holds the file, instead of dropping the write", () => {
    const p = join(root, "atomic", "grace-x.json");
    writeFileAtomic(p, "old");
    let fails = 3;
    const t0 = Date.now();
    writeFileAtomic(p, "new", (from, to) => {
      if (fails-- > 0) throw busy(["EPERM", "EBUSY", "EACCES"][fails % 3]!);
      writeFileSync(to, readFileSync(from));
      rmSync(from);
    });
    expect(readFileSync(p, "utf8")).toBe("new");
    expect(Date.now() - t0).toBeGreaterThanOrEqual(50); // it actually waited between attempts
    expect(readdirSync(join(root, "atomic"))).toEqual(["grace-x.json"]);
  });

  it("gives up after 10 attempts, removes its temporary file and throws", () => {
    const p = join(root, "atomic2", "lease-x.json");
    let calls = 0;
    expect(() => writeFileAtomic(p, "v", () => { calls++; throw busy("EPERM"); })).toThrow("EPERM");
    expect(calls).toBe(10);
    expect(readdirSync(join(root, "atomic2"))).toEqual([]);
  });

  it("does not retry other errors", () => {
    const p = join(root, "atomic3", "lease-x.json");
    let calls = 0;
    expect(() => writeFileAtomic(p, "v", () => { calls++; throw busy("ENOENT"); })).toThrow("ENOENT");
    expect(calls).toBe(1);
  });
});
