// The signed Pro release manifest in the download path (docs/PRO-RELEASE-MANIFEST.md): a fresh answer of the
// download route is checked before any archive byte is requested, and a kept answer (~/.clearcote/pro-meta-*.json,
// a file the user can write) every time before it launches an installed build, the stale fall-back included.
// The archive hash check, the launcher hash check after extraction and the launch-time hash checks behind it
// are the ones from before. A local server stands in for the route and the archive and signs with the TEST key
// (helpers/fake-build.ts); HOME and the browser cache are temp dirs. The check itself: pro-manifest.test.ts.
import { afterEach, describe, expect, it, vi } from "vitest";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import {
  BINARY, TAG, TEST_KEY_ID, fakeArchive, pinTestReleaseKey, signedAnswer, startFakeBuild,
  type FakeBuild, type ProAnswer,
} from "./helpers/fake-build.js";
import { tempDir } from "./helpers/temp.js";

const proPlatform = process.platform === "win32" || process.platform === "linux"; // the PRO route serves these
const ENV_KEYS = ["HOME", "USERPROFILE", "LOCALAPPDATA", "CLEARCOTE_LICENSE_API", "CLEARCOTE_RELEASE_CHANNEL"] as const;
const savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
const fakes: FakeBuild[] = [];
const KEY = "cc_lic_manifest";
const GOOD = "x".repeat(64); // every file of the fake build
const sha = (b: Buffer | string) => createHash("sha256").update(b).digest("hex");

afterEach(async () => {
  vi.restoreAllMocks();
  for (const f of fakes.splice(0)) await f.close();
  for (const [k, v] of Object.entries(savedEnv)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

/** A new process, as far as download.ts's memory goes. `pin: false` leaves PRO_RELEASE_KEYS as the SDK ships it. */
async function newProcess(pin = true) {
  vi.resetModules();
  const dl = await import("../src/download.js");
  if (pin) await pinTestReleaseKey();
  return dl;
}

async function setup(opts: Parameters<typeof startFakeBuild>[0] & { pin?: boolean } = {}) {
  const home = tempDir("cc-manifest-");
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.LOCALAPPDATA = home;
  delete process.env.CLEARCOTE_LICENSE_API;
  delete process.env.CLEARCOTE_RELEASE_CHANNEL;
  const srv = await startFakeBuild(opts);
  fakes.push(srv);
  const cache = tempDir("cc-manifest-cache-");
  const stderr: string[] = [];
  vi.spyOn(process.stderr, "write").mockImplementation((s: string | Uint8Array) => {
    stderr.push(String(s));
    return true;
  });
  const dl = await newProcess(opts.pin !== false);
  type Dl = typeof dl;
  const ensure = (d: Dl = dl, version?: string) => d.proEnsureBinary(KEY, { apiBase: srv.url, cacheDir: cache, quiet: true, version });
  return { home, cache, srv, dl, ensure, stderr, base: path.join(cache, TAG) };
}

/** The error a launch was refused with. A module loaded after vi.resetModules() has its own error classes, so
 * the class is told by its name. */
async function refusal(p: Promise<unknown>): Promise<Error & { reason?: string }> {
  let got: unknown;
  try {
    got = await p;
  } catch (e) {
    return e as Error & { reason?: string };
  }
  throw new Error(`expected the launch to be refused, got ${String(got)}`);
}

async function manifestRefusal(p: Promise<unknown>, reason: string): Promise<Error> {
  const e = await refusal(p);
  expect(e.message).toMatch(/^Clearcote Pro release manifest check failed \(/);
  expect(e.name).toBe("ProManifestError");
  expect(e.reason).toBe(reason);
  expect(e.message).toContain("Refusing to use this build.");
  return e;
}

const keptFiles = (home: string): string[] => {
  const dir = path.join(home, ".clearcote");
  return existsSync(dir) ? readdirSync(dir).filter((n) => /^pro-meta-[0-9a-f]{16}\.json$/.test(n)).map((n) => path.join(dir, n)) : [];
};
const nothingInstalled = (base: string) => !existsSync(path.join(base, "browser")) && !existsSync(path.join(base, ".verified"));
const without = (a: ProAnswer, field: string): ProAnswer => Object.fromEntries(Object.entries(a).filter(([k]) => k !== field));

describe.runIf(proPlatform)("a fresh answer of the PRO download route", () => {
  it("with a correct signed manifest is installed, kept, and launched again from the kept answer", async () => {
    const { home, srv, ensure, base } = await setup();
    const exe = await ensure();
    expect(exe).toBe(path.join(base, "browser", BINARY));
    expect(srv.archiveHits).toBe(1);
    const kept = JSON.parse(readFileSync(keptFiles(home)[0], "utf8")).meta;
    expect(kept.revision).toBe("r1");
    expect(typeof kept.manifest).toBe("string");
    expect(await ensure(await newProcess())).toBe(exe); // another process: the kept answer, verified again
    expect(srv.metaHits).toBe(1);
  }, 30_000);

  it("(a) without a manifest is refused, and the archive is never requested", async () => {
    for (const strip of [
      (a: ProAnswer) => without(a, "manifest"),
      (a: ProAnswer) => ({ ...a, manifest: "" }),
      (a: ProAnswer) => ({ ...a, manifest: null }),
    ]) {
      const { home, srv, ensure, base } = await setup({ answer: strip });
      await manifestRefusal(ensure(), "missing");
      expect(srv.metaHits).toBe(1);
      expect(srv.archiveHits).toBe(0);
      expect(nothingInstalled(base)).toBe(true);
      expect(keptFiles(home)).toEqual([]); // an answer that fails is not kept
      // and again: still refused, still no download
      await manifestRefusal(ensure(), "missing");
      expect(srv.archiveHits).toBe(0);
    }
  }, 30_000);

  it("with a manifest whose signature does not verify is refused before the download", async () => {
    const flip = (a: ProAnswer) => {
      const [text, sig] = String(a.manifest).split(".");
      const bytes = Buffer.from(sig, "base64url");
      bytes[10] ^= 0x01;
      return { ...a, manifest: `${text}.${bytes.toString("base64url")}` };
    };
    const { srv, ensure, base } = await setup({ answer: flip });
    await manifestRefusal(ensure(), "signature");
    expect(srv.archiveHits).toBe(0);
    expect(nothingInstalled(base)).toBe(true);
  });

  it("(b) with a correctly signed manifest of another revision is refused", async () => {
    // the answer says r1; its manifest is a genuine one, for r2 of the same version
    const other = (a: ProAnswer) => ({ ...a, manifest: signedAnswer({ ...a, revision: "r2" }).manifest });
    const { home, srv, ensure, base } = await setup({ answer: other });
    const e = await manifestRefusal(ensure(), "revision");
    expect(e.message).toContain("'r2'");
    expect(srv.archiveHits).toBe(0);
    expect(nothingInstalled(base)).toBe(true);
    expect(keptFiles(home)).toEqual([]);
  });

  it("(b) that is signed and consistent, but not the revision or version the caller pinned, is refused", async () => {
    const { srv, ensure, base } = await setup(); // the route answers r1 of 0.0.0 whatever is asked
    await manifestRefusal(ensure(undefined, "r2"), "revision");
    await manifestRefusal(ensure(undefined, "9.9.9.9-r1"), "version");
    await manifestRefusal(ensure(undefined, "9.9.9.9"), "version");
    expect(srv.archiveHits).toBe(0);
    expect(nothingInstalled(base)).toBe(true);
    expect(await ensure(undefined, "r1")).toBe(path.join(base, "browser", BINARY)); // the pinned revision itself passes
    expect(srv.archiveHits).toBe(1);
  }, 30_000);

  it("whose hashes were changed after signing is refused before the download", async () => {
    for (const [field, reason] of [["sha256", "archive"], ["size", "archive"], ["exe_sha256", "binary"],
      ...(process.platform === "win32" ? [["chrome_dll_sha256", "binary"]] : [])] as Array<[string, string]>) {
      const edit = (a: ProAnswer) => ({ ...a, [field]: field === "size" ? Number(a.size) + 1 : "0".repeat(64) });
      const { srv, ensure, base } = await setup({ answer: edit });
      await manifestRefusal(ensure(), reason);
      expect(srv.archiveHits, field).toBe(0);
      expect(nothingInstalled(base)).toBe(true);
    }
  }, 30_000);

  it("(f) is refused as unknown-key while no release key is pinned (the SDK as it ships today)", async () => {
    const { home, srv, ensure, base } = await setup({ pin: false });
    const release = await import("../src/release.js"); // the instance download.ts uses
    expect(release.PRO_RELEASE_KEYS).not.toHaveProperty(TEST_KEY_ID);
    const e = await manifestRefusal(ensure(), "unknown-key");
    expect(e.message).toContain(`'${TEST_KEY_ID}'`);
    expect(srv.metaHits).toBe(1);
    expect(srv.archiveHits).toBe(0);
    expect(nothingInstalled(base)).toBe(true);
    expect(keptFiles(home)).toEqual([]);
  });

  it("(f) no environment variable pins a key or skips the check", async () => {
    const names = ["CLEARCOTE_PRO_RELEASE_KEYS", "CLEARCOTE_PRO_RELEASE_KEY", "CLEARCOTE_RELEASE_KEYS", "CLEARCOTE_SKIP_MANIFEST",
      "CLEARCOTE_NO_VERIFY", "CLEARCOTE_INSECURE", "CLEARCOTE_ALLOW_UNSIGNED"];
    const before = Object.fromEntries(names.map((n) => [n, process.env[n]]));
    try {
      for (const n of names) process.env[n] = n.includes("KEY") ? `${TEST_KEY_ID}=${"a5".repeat(32)}` : "1";
      const { srv, ensure } = await setup({ pin: false });
      await manifestRefusal(ensure(), "unknown-key");
      expect(srv.archiveHits).toBe(0);
    } finally {
      for (const [n, v] of Object.entries(before)) {
        if (v === undefined) delete process.env[n];
        else process.env[n] = v;
      }
    }
  });
});

describe.runIf(proPlatform)("behind the manifest: the archive and the installed files", () => {
  it("(c) an archive whose bytes were altered is refused by the archive hash check", async () => {
    const altered = Buffer.from(fakeArchive());
    altered[altered.length - 30] ^= 0x01; // same length, signed manifest and answer untouched
    const { srv, ensure, base } = await setup({ archiveBody: altered });
    const e = await refusal(ensure());
    expect(e.message).toMatch(/archive SHA-256 mismatch/);
    expect(srv.archiveHits).toBe(1);
    expect(nothingInstalled(base)).toBe(true);
  }, 30_000);

  const protectedFiles = process.platform === "win32" ? [BINARY, "chrome.dll"] : [BINARY];

  it.each(protectedFiles)("(d) an installed build whose %s was altered is not launched: the launch-time hash check refuses it", async (name) => {
    const { srv, ensure, base, home } = await setup();
    const exe = await ensure();
    const file = path.join(base, "browser", name);
    writeFileSync(file, Buffer.alloc(64, "y")); // same size, other bytes
    srv.metaChallenge = true; // the site cannot be asked: nothing can repair it
    // the kept answer still passes the manifest check; the files no longer hash to its values
    const e = await refusal(ensure(await newProcess()));
    expect(e.name).not.toBe("ProManifestError");
    expect(e.message).toMatch(/no earlier answer names an\s+installed build/);
    expect(readFileSync(file, "utf8")).toBe("y".repeat(64)); // left as found, and not launched
    expect(keptFiles(home)).toHaveLength(1);
    expect(exe).toBe(path.join(base, "browser", BINARY));
  }, 30_000);

  it.each(protectedFiles)("(d) and with the site reachable, the build with the altered %s is replaced by a verified download", async (name) => {
    const { srv, ensure, base } = await setup();
    const exe = await ensure();
    const file = path.join(base, "browser", name);
    writeFileSync(file, Buffer.alloc(64, "y"));
    expect(await ensure(await newProcess())).toBe(exe);
    expect(srv.metaHits).toBe(2); // the kept answer did not launch it
    expect(srv.archiveHits).toBe(2);
    expect(readFileSync(file, "utf8")).toBe(GOOD);
  }, 30_000);
});

describe.runIf(proPlatform)("a kept answer (pro-meta-*.json)", () => {
  /** Install, then rewrite the kept answer as someone with write access to ~/.clearcote would. */
  async function installedThenEdit(edit: (meta: Record<string, unknown>) => Record<string, unknown>) {
    const s = await setup();
    const exe = await s.ensure();
    const [file] = keptFiles(s.home);
    const kept = JSON.parse(readFileSync(file, "utf8"));
    const original = kept.meta as Record<string, unknown>;
    writeFileSync(file, JSON.stringify({ at: kept.at, meta: edit({ ...original }) }));
    return { ...s, exe, file, original };
  }
  /** The launcher replaced, and the kept answer's launcher hash edited to match it. */
  const swapLauncher = (meta: Record<string, unknown>) => ({ ...meta, exe_sha256: sha(Buffer.alloc(64, "y")) });

  it("(e) with edited hashes (manifest left as it was) is not used: the site is asked and the build re-verified", async () => {
    const { srv, ensure, exe, file, original } = await installedThenEdit(swapLauncher);
    writeFileSync(exe, Buffer.alloc(64, "y")); // now the launcher hashes to the edited value
    expect(await ensure(await newProcess())).toBe(exe);
    expect(srv.metaHits).toBe(2); // the kept answer was not trusted
    expect(srv.archiveHits).toBe(2); // and the swapped launcher failed the hash the manifest covers
    expect(readFileSync(exe, "utf8")).toBe(GOOD);
    expect(JSON.parse(readFileSync(file, "utf8")).meta.exe_sha256).toBe(original.exe_sha256); // replaced by the verified answer
  }, 30_000);

  it("(e) with edited hashes is not launched when the site cannot be reached: the stale fall-back checks it too", async () => {
    const { srv, ensure, exe, stderr } = await installedThenEdit(swapLauncher);
    writeFileSync(exe, Buffer.alloc(64, "y"));
    srv.metaChallenge = true;
    const e = await manifestRefusal(ensure(await newProcess()), "binary");
    expect(e.message).toContain("kept from an earlier launch");
    expect(e.message).toContain("challenge page");
    expect(srv.archiveHits).toBe(1);
    expect(stderr.join("")).not.toContain("launching the installed build");
  }, 30_000);

  it.each([
    ["sha256", "archive"],
    ["size", "archive"],
    ["version", "version"],
    ["revision", "revision"],
    ...(process.platform === "win32" ? [["chrome_dll_sha256", "binary"]] : []),
  ] as Array<[string, string]>)("(e) with an edited %s is refused (%s) by the stale fall-back", async (field, reason) => {
    const { srv, ensure } = await installedThenEdit((m) => ({ ...m, [field]: field === "size" ? Number(m.size) + 1 : field === "version" ? "0.0.1" : field === "revision" ? "r2" : "0".repeat(64) }));
    srv.metaChallenge = true;
    await manifestRefusal(ensure(await newProcess()), reason);
  }, 30_000);

  it("(e) with the manifest removed is not used: the site is asked again", async () => {
    const { srv, ensure, exe, file } = await installedThenEdit((m) => without(m, "manifest"));
    expect(await ensure(await newProcess())).toBe(exe); // the untouched build, on the fresh, verified answer
    expect(srv.metaHits).toBe(2);
    expect(srv.archiveHits).toBe(1);
    expect(typeof JSON.parse(readFileSync(file, "utf8")).meta.manifest).toBe("string");
  }, 30_000);

  it("(e) with the manifest removed is not launched when the site cannot be reached", async () => {
    const { srv, ensure, stderr } = await installedThenEdit((m) => without(m, "manifest"));
    srv.metaChallenge = true;
    await manifestRefusal(ensure(await newProcess()), "missing");
    expect(stderr.join("")).not.toContain("launching the installed build");
  }, 30_000);

  it("(e) with another build's genuine manifest pasted in is refused", async () => {
    const { srv, ensure } = await installedThenEdit((m) => ({ ...m, manifest: signedAnswer({ ...m, revision: "r2" }).manifest }));
    srv.metaChallenge = true;
    await manifestRefusal(ensure(await newProcess()), "revision");
  }, 30_000);

  it("written while no release key is pinned to this process is not used either", async () => {
    // an answer kept by a process that trusted the test key; this process (the SDK as shipped) does not
    const { srv, ensure } = await installedThenEdit((m) => m);
    srv.metaChallenge = true;
    await manifestRefusal(ensure(await newProcess(false)), "unknown-key");
  }, 30_000);

  it("untouched still launches the installed build when the site cannot be reached", async () => {
    const { srv, ensure, exe, stderr, home } = await installedThenEdit((m) => m);
    srv.metaChallenge = true;
    for (const f of keptFiles(home)) { // older than the 10-15 min window
      const e = JSON.parse(readFileSync(f, "utf8"));
      writeFileSync(f, JSON.stringify({ ...e, at: e.at - 3600 }));
    }
    expect(await ensure(await newProcess())).toBe(exe);
    expect(srv.metaHits).toBe(2);
    expect(stderr.join("")).toContain("launching the installed build the last answer named");
  }, 30_000);
});
