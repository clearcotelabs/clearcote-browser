// engineSupportsSwitch remembers where it found a name.
//
// A launch asks the engine binary for about seven switch names. Each search read the binary from its start, and
// the names sit together near the end of a 290 MB chrome.dll: measured on r60 (2026-11-16), 7 probes of 130 to
// 185 ms, 1.0 of the 2.0 s the first launch of a process took. The probe now keeps the offset of every name it
// found (in memory, and in ~/.clearcote/engine-switches.json for the next process), checks a known name where it
// was found, and searches for a new one from just before the known ones. The answers must stay exactly what a
// search of the whole file gives, whatever the positions file says.
//
// Mirrors sdk/python/tests/test_switch_probe.py.
import { describe, it, expect, vi, beforeAll, afterAll, beforeEach, afterEach } from "vitest";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const io = vi.hoisted(() => ({ bytesRead: 0 }));

// Count what the probe reads, to tell a search from a check at a known offset.
vi.mock("node:fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:fs")>();
  const readSync = ((...a: Parameters<typeof actual.readSync>) => {
    const n = (actual.readSync as (...x: unknown[]) => number)(...a);
    io.bytesRead += n;
    return n;
  }) as typeof actual.readSync;
  return { ...actual, default: { ...actual, readSync }, readSync };
});

import { engineSupportsSwitch, resetSwitchProbe, switchPositions } from "../src/launchopts.js";

const MB = 1024 * 1024;
const CHUNK = 8 * MB;
const SIZE = 20 * MB;
const needle = (name: string) => Buffer.from(`\0${name}\0`, "latin1");
// Where each name sits in the stand-in binary. "near-end" is found first in most tests; the others are before
// it, across a read boundary, or across the place a later search starts from.
const AT: Record<string, number> = {
  "near-end": 18 * MB,
  "also-near-end": 19 * MB + 123,
  "early": 1 * MB + 7,
  "across-a-chunk": CHUNK - 5,
  "across-the-start": 2 * CHUNK - 3,
  "last-bytes": SIZE - needle("last-bytes").length,
};

const saved = { ...switchPositions };
let root: string;
let exe: string;
let positions: string;

/** The file the probe reads for `exe`: chrome.dll next to it on Windows, the executable elsewhere. */
function writeEngine(dir: string, body: Buffer): string {
  const p = join(dir, "chrome");
  writeFileSync(p, body);
  return p;
}

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "cc-switch-probe-"));
  const body = Buffer.alloc(SIZE, 0x41);
  for (const [name, at] of Object.entries(AT)) needle(name).copy(body, at);
  // a longer name that contains a shorter one, and a header-like string that is not NUL-delimited
  Buffer.from("\0near-end-extended\0", "latin1").copy(body, 3 * MB);
  Buffer.from("proxy-authenticate", "latin1").copy(body, 4 * MB);
  exe = writeEngine(root, body);
});

afterAll(() => {
  rmSync(root, { recursive: true, force: true, maxRetries: 5 });
});

beforeEach(() => {
  positions = join(root, `positions-${Math.random().toString(36).slice(2)}.json`);
  switchPositions.file = () => positions;
  switchPositions.minSize = saved.minSize;
  switchPositions.lookBack = 0;       // a search for a new name starts at the chunk of the first known one
  switchPositions.maxFiles = saved.maxFiles;
  resetSwitchProbe();
  io.bytesRead = 0;
});

afterEach(() => {
  Object.assign(switchPositions, saved);
  resetSwitchProbe();
});

const stored = (): Record<string, Record<string, number>> => JSON.parse(readFileSync(positions, "utf8")).files;
const keyOf = (file: string) => { const st = statSync(file); return `${resolve(file)}|${st.size}|${Math.floor(st.mtimeMs / 1000)}`; };
/** A new process: nothing in memory, the positions file as it is. */
const newProcess = () => { resetSwitchProbe(); io.bytesRead = 0; };

describe("answers", () => {
  it("finds every name wherever it sits, in any order, and no name that is not there", () => {
    for (const order of [Object.keys(AT), Object.keys(AT).reverse(), ["early", "near-end", "across-the-start", "across-a-chunk", "last-bytes", "also-near-end"]]) {
      resetSwitchProbe();
      rmSync(positions, { force: true });
      for (const name of order) expect(engineSupportsSwitch(exe, name), name).toBe(true);
      for (const name of ["missing", "near-en", "ear-end", "proxy-auth", "proxy-authenticate", ""]) expect(engineSupportsSwitch(exe, name), name).toBe(false);
      expect(engineSupportsSwitch(exe, "near-end-extended")).toBe(true);
    }
  });

  it("a missing file, no path and a directory answer false", () => {
    expect(engineSupportsSwitch(join(root, "nope"), "near-end")).toBe(false);
    expect(engineSupportsSwitch(undefined, "near-end")).toBe(false);
    expect(engineSupportsSwitch(root, "near-end")).toBe(false);
    expect(existsSync(positions)).toBe(false);
  });
});

describe("what is remembered", () => {
  it("the offset of each name found, per binary; nothing for a name that is not there", () => {
    for (const name of Object.keys(AT)) engineSupportsSwitch(exe, name);
    engineSupportsSwitch(exe, "missing");
    expect(stored()).toEqual({ [keyOf(exe)]: AT });
    expect(readdirSync(root).filter((f) => f.endsWith(".tmp"))).toEqual([]);   // written whole, then renamed
  });

  it("nothing for a small binary: it is searched whole every time", () => {
    const dir = mkdtempSync(join(root, "small-"));
    const small = writeEngine(dir, Buffer.from("\x7fELF\0proxy-auth\0--remote-debugging-port\0", "latin1"));
    expect(engineSupportsSwitch(small, "proxy-auth")).toBe(true);
    expect(engineSupportsSwitch(small, "missing")).toBe(false);
    expect(existsSync(positions)).toBe(false);
  });

  it("only the newest binaries are kept", () => {
    switchPositions.minSize = 0;
    switchPositions.maxFiles = 2;
    const keys: string[] = [];
    for (const n of ["a", "b", "c"]) {
      const dir = mkdtempSync(join(root, `engine-${n}-`));
      const p = writeEngine(dir, Buffer.from(`\x7fELF\0switch-${n}\0`, "latin1"));
      expect(engineSupportsSwitch(p, `switch-${n}`)).toBe(true);
      keys.push(keyOf(p));
    }
    expect(Object.keys(stored())).toEqual(keys.slice(1));
  });
});

describe("the next process", () => {
  it("checks a known name where it was found instead of searching", () => {
    expect(engineSupportsSwitch(exe, "near-end")).toBe(true);
    expect(io.bytesRead).toBeGreaterThanOrEqual(18 * MB);   // the first search reads up to the name
    newProcess();
    expect(engineSupportsSwitch(exe, "near-end")).toBe(true);
    expect(io.bytesRead).toBe(needle("near-end").length);
  });

  it("searches for a new name from the known ones on, then in the rest of the file", () => {
    engineSupportsSwitch(exe, "near-end");
    newProcess();
    expect(engineSupportsSwitch(exe, "also-near-end")).toBe(true);
    expect(io.bytesRead).toBeLessThanOrEqual(4 * MB + 64);   // from 16 MB to the end
    newProcess();
    expect(engineSupportsSwitch(exe, "early")).toBe(true);    // before the known ones: the rest of the file
    expect(engineSupportsSwitch(exe, "missing")).toBe(false);
    expect(stored()[keyOf(exe)]).toEqual({ "near-end": AT["near-end"], "also-near-end": AT["also-near-end"], early: AT.early });
  });

  it("a name the binary lacks is searched for in the whole file, once per process", () => {
    engineSupportsSwitch(exe, "near-end");
    newProcess();
    expect(engineSupportsSwitch(exe, "missing")).toBe(false);
    expect(io.bytesRead).toBeGreaterThanOrEqual(SIZE);
    io.bytesRead = 0;
    expect(engineSupportsSwitch(exe, "missing")).toBe(false);
    expect(io.bytesRead).toBe(0);
  });
});

describe("the positions file is never the answer", () => {
  const write = (files: unknown) => writeFileSync(positions, JSON.stringify({ v: 1, files }));

  it("a wrong offset for a name that is there: found by searching, and the file is corrected", () => {
    write({ [keyOf(exe)]: { "near-end": 5 * MB, early: AT["near-end"] } });
    expect(engineSupportsSwitch(exe, "near-end")).toBe(true);
    expect(engineSupportsSwitch(exe, "early")).toBe(true);
    expect(stored()[keyOf(exe)]).toEqual({ "near-end": AT["near-end"], early: AT.early });
  });

  it("an offset for a name that is not there: still false", () => {
    write({ [keyOf(exe)]: { missing: AT["near-end"], "near-en": AT["near-end"], gone: SIZE - 2 } });
    for (const name of ["missing", "near-en", "gone"]) expect(engineSupportsSwitch(exe, name), name).toBe(false);
  });

  it.each([
    ["not JSON", "{{{"],
    ["another shape", JSON.stringify([1, 2, 3])],
    ["files is a list", JSON.stringify({ v: 1, files: [] })],
    ["offsets that are no offsets", null],
  ])("%s: the same answers", (_what, text) => {
    writeFileSync(positions, text ?? JSON.stringify({ v: 1, files: { [keyOf(exe)]: { "near-end": -1, early: 1e12, "across-a-chunk": "12", "last-bytes": 1.5, "also-near-end": null } } }));
    for (const name of Object.keys(AT)) expect(engineSupportsSwitch(exe, name), name).toBe(true);
    expect(engineSupportsSwitch(exe, "missing")).toBe(false);
    expect(stored()[keyOf(exe)]).toEqual(AT);
  });

  it("a binary that changed (size or time) starts again", () => {
    engineSupportsSwitch(exe, "near-end");
    const dir = mkdtempSync(join(root, "changed-"));
    const body = Buffer.alloc(SIZE, 0x41);
    needle("near-end").copy(body, 2 * MB);
    const other = writeEngine(dir, body);
    // the old binary's positions under the new binary's key would point at the wrong place: checked, then searched
    write({ [keyOf(other)]: { "near-end": AT["near-end"] } });
    newProcess();
    expect(engineSupportsSwitch(other, "near-end")).toBe(true);
    expect(engineSupportsSwitch(other, "early")).toBe(false);
    expect(stored()[keyOf(other)]).toEqual({ "near-end": 2 * MB });
    // same path and size, a later modification time: another key
    const before = keyOf(other);
    const later = new Date(Date.now() + 10_000);
    utimesSync(other, later, later);
    expect(keyOf(other)).not.toBe(before);
    newProcess();
    expect(engineSupportsSwitch(other, "near-end")).toBe(true);
    expect(Object.keys(stored())).toContain(keyOf(other));
  });

  it("a positions file that cannot be written changes nothing", () => {
    switchPositions.file = () => join(root, "chrome", "cannot", "engine-switches.json");   // under a FILE
    for (const name of Object.keys(AT)) expect(engineSupportsSwitch(exe, name), name).toBe(true);
    expect(engineSupportsSwitch(exe, "missing")).toBe(false);
  });
});
