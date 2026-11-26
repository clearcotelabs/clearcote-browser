// The signed Pro release manifest check (docs/PRO-RELEASE-MANIFEST.md), on its own: the shared test vectors
// (sdk/testdata/pro_manifest_vectors.json, the same file the Python and .NET SDKs run), the Ed25519 verifier
// against RFC 8032, and the corners the vectors leave out. The download path is covered in
// pro-manifest-download.test.ts.
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { generateKeyPairSync } from "node:crypto";
import { readFileSync } from "node:fs";
import { proPins } from "../src/download.js";
import { ProManifestError, defaultProLauncher, ed25519Verify, verifyProManifest, type ProManifestMeta, type ProPlatform } from "../src/promanifest.js";
import { PRO_RELEASE_KEYS } from "../src/release.js";
import { TEST_KEY_ID, TEST_PUBLIC_KEY_HEX, manifestPayload, signProManifest, signedAnswer } from "./helpers/fake-build.js";

interface Case {
  name: string; platform: ProPlatform; want_revision: string | null; want_version: string | null;
  meta: ProManifestMeta; expect: string;
}
const VECTORS = JSON.parse(readFileSync(new URL("../../testdata/pro_manifest_vectors.json", import.meta.url), "utf8")) as {
  key_id: string; public_key_hex: string;
  rfc8032: Array<{ public_key_hex: string; message_hex: string; signature_hex: string }>;
  cases: Case[];
};
const hex = (s: string) => Buffer.from(s, "hex");

/** "ok", or the reason code of the refusal. Anything but a ProManifestError is a bug and is rethrown. */
function outcome(c: Pick<Case, "meta" | "platform" | "want_revision" | "want_version">): string {
  try {
    verifyProManifest(c.meta, c.platform, c.want_revision, c.want_version);
    return "ok";
  } catch (e) {
    if (!(e instanceof ProManifestError)) throw e;
    return e.reason;
  }
}

const pinnedBefore = { ...PRO_RELEASE_KEYS };
function unpinAll(): void {
  for (const k of Object.keys(PRO_RELEASE_KEYS)) delete PRO_RELEASE_KEYS[k];
}
beforeEach(() => {
  unpinAll();
  PRO_RELEASE_KEYS[TEST_KEY_ID] = TEST_PUBLIC_KEY_HEX; // the test key, for this file only
});
afterEach(() => {
  unpinAll();
  Object.assign(PRO_RELEASE_KEYS, pinnedBefore);
});

/** A correct Windows answer, signed with the test key. */
const good = (over: Record<string, unknown> = {}) => signedAnswer({
  tag: "pro-158.0.8127.36-r68", version: "158.0.8127.36", asset: "a.zip", binary: "chrome.exe",
  sha256: "a".repeat(64), exe_sha256: "b".repeat(64), chrome_dll_sha256: "c".repeat(64), size: 1234, ...over,
}, "windows");

describe("Ed25519 (RFC 8032 section 7.1)", () => {
  it("has the three vectors", () => {
    expect(VECTORS.rfc8032).toHaveLength(3);
  });

  it.each(VECTORS.rfc8032.map((v, i) => [i + 1, v] as const))("vector %i verifies, and fails when one byte changes", (_n, v) => {
    const key = hex(v.public_key_hex), msg = hex(v.message_hex), sig = hex(v.signature_hex);
    expect(ed25519Verify(key, msg, sig)).toBe(true);
    for (const at of [0, 31, 32, 63]) { // R and S halves of the signature
      const bad = Buffer.from(sig);
      bad[at] ^= 0x01;
      expect(ed25519Verify(key, msg, bad), `signature byte ${at}`).toBe(false);
    }
    const otherKey = Buffer.from(key);
    otherKey[5] ^= 0x01;
    expect(ed25519Verify(otherKey, msg, sig), "key byte").toBe(false);
    const otherMsg = msg.length ? Buffer.from(msg) : Buffer.from([0]);
    if (msg.length) otherMsg[0] ^= 0x01;
    expect(ed25519Verify(key, otherMsg, sig), "message byte").toBe(false);
  });

  it("a key or signature of the wrong length is false, not an exception", () => {
    const v = VECTORS.rfc8032[1];
    const key = hex(v.public_key_hex), msg = hex(v.message_hex), sig = hex(v.signature_hex);
    expect(ed25519Verify(key, msg, sig.subarray(0, 63))).toBe(false);
    expect(ed25519Verify(key, msg, Buffer.concat([sig, Buffer.alloc(1)]))).toBe(false);
    expect(ed25519Verify(key, msg, Buffer.alloc(0))).toBe(false);
    expect(ed25519Verify(key.subarray(0, 31), msg, sig)).toBe(false);
    expect(ed25519Verify(Buffer.concat([key, Buffer.alloc(1)]), msg, sig)).toBe(false);
    expect(ed25519Verify(Buffer.alloc(0), msg, sig)).toBe(false);
  });

  it("the test key of the vectors file is the public half of its seed", () => {
    expect(VECTORS.key_id).toBe(TEST_KEY_ID);
    const manifest = signProManifest({ key: TEST_KEY_ID });
    const [text, sig] = manifest.split(".");
    expect(ed25519Verify(hex(VECTORS.public_key_hex), Buffer.from(text, "ascii"), Buffer.from(sig, "base64url"))).toBe(true);
  });
});

describe("release manifest test vectors", () => {
  it("covers every reason code", () => {
    expect(VECTORS.cases.length).toBeGreaterThanOrEqual(27);
    expect([...new Set(VECTORS.cases.map((c) => c.expect))].sort()).toEqual(
      ["archive", "binary", "malformed", "missing", "ok", "platform", "product", "revision", "signature", "unknown-key", "version"],
    );
  });

  it.each(VECTORS.cases.map((c) => [c.name, c.expect, c] as const))("%s -> %s", (_name, want, c) => {
    expect(outcome(c)).toBe(want);
  });

  it("a passing case returns the signed payload", () => {
    const c = VECTORS.cases.find((x) => x.name === "valid windows")!;
    const p = verifyProManifest(c.meta, c.platform);
    expect(p).toMatchObject({ v: 1, product: "clearcote-pro", version: "158.0.8127.36", revision: "r68", platform: "windows", key: TEST_KEY_ID });
    expect(p.archive.sha256).toBe(c.meta.sha256);
    expect(Object.keys(p.binaries).sort()).toEqual(["chrome.dll", "chrome.exe"]);
  });

  it("with no key pinned every case that reaches the key check is refused as unknown-key", () => {
    unpinAll();
    const early = new Set(["missing", "malformed"]);
    for (const c of VECTORS.cases) {
      const got = outcome(c);
      // "unknown manifest version" is malformed at step 5, behind the key check
      if (early.has(c.expect) && c.name !== "unknown manifest version") expect(got, c.name).toBe(c.expect);
      else expect(got, c.name).toBe("unknown-key");
    }
  });
});

describe("release manifest: order and corners", () => {
  it("a correct answer passes, with or without a matching pin", () => {
    expect(outcome({ meta: good(), platform: "windows", want_revision: null, want_version: null })).toBe("ok");
    expect(outcome({ meta: good(), platform: "windows", want_revision: "r68", want_version: "158.0.8127.36" })).toBe("ok");
    expect(verifyProManifest(good(), "windows", undefined, undefined).revision).toBe("r68");
  });

  it("the error names the reason, says what was wrong and that the build is refused", () => {
    let err: unknown;
    try {
      verifyProManifest(good({ revision: "r68" }), "windows", "r69");
    } catch (e) {
      err = e;
    }
    expect(err).toBeInstanceOf(ProManifestError);
    expect(err).toBeInstanceOf(Error);
    const e = err as ProManifestError;
    expect(e.name).toBe("ProManifestError");
    expect(e.reason).toBe("revision");
    expect(e.message).toMatch(/^Clearcote Pro release manifest check failed \(revision\): .*'r68'.*'r69'.*\. Refusing to use this build\.$/);
  });

  it("the first failing check wins", () => {
    const m = good();
    // wrong platform AND wrong pinned version AND wrong pinned revision: platform is checked first
    expect(outcome({ meta: m, platform: "linux", want_revision: "r1", want_version: "1.2.3.4" })).toBe("platform");
    expect(outcome({ meta: m, platform: "windows", want_revision: "r1", want_version: "1.2.3.4" })).toBe("version");
    expect(outcome({ meta: m, platform: "windows", want_revision: "r1", want_version: null })).toBe("revision");
    // archive before binary
    expect(outcome({ meta: { ...m, sha256: "0".repeat(64), exe_sha256: "0".repeat(64) }, platform: "windows", want_revision: null, want_version: null })).toBe("archive");
    // an unpinned key id wins over a signature that cannot verify
    const [text] = (m.manifest as string).split(".");
    const other = signProManifest({ ...manifestPayload(m, "windows"), key: "other" }).split(".")[0];
    expect(outcome({ meta: { ...m, manifest: `${other}.AAAA` }, platform: "windows", want_revision: null, want_version: null })).toBe("unknown-key");
    expect(outcome({ meta: { ...m, manifest: `${text}.AAAA` }, platform: "windows", want_revision: null, want_version: null })).toBe("signature");
  });

  it("nothing in the payload counts before the signature: a forged payload that matches the answer is refused", () => {
    const m = good();
    const forger = generateKeyPairSync("ed25519").privateKey;
    const forged = signProManifest(manifestPayload(m, "windows"), forger); // names the pinned key id, signed by another key
    expect(outcome({ meta: { ...m, manifest: forged }, platform: "windows", want_revision: null, want_version: null })).toBe("signature");
    // a wrong product under a bad signature is still "signature", never "product"
    const wrong = signProManifest({ ...manifestPayload(m, "windows"), product: "x" }, forger);
    expect(outcome({ meta: { ...m, manifest: wrong }, platform: "windows", want_revision: null, want_version: null })).toBe("signature");
  });

  it("refuses base64 that is not base64url without padding", () => {
    const m = good();
    const [text, sig] = (m.manifest as string).split(".");
    const std = Buffer.from(sig, "base64url").toString("base64"); // "+", "/" and "=" padding
    expect(std).toMatch(/=$/);
    for (const manifest of [`${text}.${std}`, `${text}=.${sig}`, `${text} .${sig}`, `${text}.${sig}\n`, `*${text.slice(1)}.${sig}`]) {
      expect(outcome({ meta: { ...m, manifest }, platform: "windows", want_revision: null, want_version: null }), manifest.slice(-12)).toBe("malformed");
    }
    // an empty signature is two parts of base64url, with a signature that is not 64 bytes
    expect(outcome({ meta: { ...m, manifest: `${text}.` }, platform: "windows", want_revision: null, want_version: null })).toBe("signature");
    expect(outcome({ meta: { ...m, manifest: `.${sig}` }, platform: "windows", want_revision: null, want_version: null })).toBe("malformed");
    expect(outcome({ meta: { ...m, manifest: "." }, platform: "windows", want_revision: null, want_version: null })).toBe("malformed");
  });

  it("a payload that is JSON but not an object is malformed", () => {
    for (const payload of [[1, 2], "text", 7, null, true]) {
      const manifest = signProManifest(payload);
      expect(outcome({ meta: { ...good(), manifest }, platform: "windows", want_revision: null, want_version: null }), JSON.stringify(payload)).toBe("malformed");
    }
  });

  it("a key id is looked up among the pinned keys only (no inherited object properties)", () => {
    const m = good();
    for (const key of ["constructor", "toString", "__proto__", "hasOwnProperty", "", 7, null, undefined, { a: 1 }]) {
      const manifest = signProManifest({ ...manifestPayload(m, "windows"), key });
      expect(outcome({ meta: { ...m, manifest }, platform: "windows", want_revision: null, want_version: null }), String(key)).toBe("unknown-key");
    }
  });

  it("a pinned key that is not 32 bytes of hex gives a signature failure, not an exception", () => {
    const m = good();
    for (const bad of ["", "zz".repeat(32), TEST_PUBLIC_KEY_HEX.slice(2), `${TEST_PUBLIC_KEY_HEX}00`]) {
      PRO_RELEASE_KEYS[TEST_KEY_ID] = bad;
      expect(outcome({ meta: m, platform: "windows", want_revision: null, want_version: null }), bad.slice(0, 8)).toBe("signature");
    }
  });

  it("a second pinned key is accepted next to the first", () => {
    const pair = generateKeyPairSync("ed25519");
    const raw = pair.publicKey.export({ format: "der", type: "spki" }).subarray(-32).toString("hex");
    PRO_RELEASE_KEYS["next-key"] = raw;
    const m = good();
    const manifest = signProManifest({ ...manifestPayload(m, "windows"), key: "next-key" }, pair.privateKey);
    expect(outcome({ meta: { ...m, manifest }, platform: "windows", want_revision: null, want_version: null })).toBe("ok");
    expect(outcome({ meta: m, platform: "windows", want_revision: null, want_version: null })).toBe("ok");
    // each signature only under its own key id
    const crossed = signProManifest({ ...manifestPayload(m, "windows"), key: TEST_KEY_ID }, pair.privateKey);
    expect(outcome({ meta: { ...m, manifest: crossed }, platform: "windows", want_revision: null, want_version: null })).toBe("signature");
  });

  it("values of the wrong type never pass for equal", () => {
    const m = good();
    const sign = (over: Record<string, unknown>) => signProManifest({ ...manifestPayload(m, "windows"), ...over });
    const run = (meta: ProManifestMeta) => outcome({ meta, platform: "windows", want_revision: null, want_version: null });
    // both sides missing is not a match
    expect(run({ ...m, version: undefined, manifest: sign({ version: undefined }) })).toBe("version");
    expect(run({ ...m, revision: undefined, manifest: sign({ revision: undefined }) })).toBe("revision");
    expect(run({ ...m, sha256: undefined, manifest: sign({ archive: { size: 1234 } }) })).toBe("archive");
    expect(run({ ...m, size: undefined, manifest: sign({ archive: { sha256: m.sha256 } }) })).toBe("archive");
    expect(run({ ...m, size: "1234", manifest: sign({ archive: { sha256: m.sha256, size: "1234" } }) })).toBe("archive");
    expect(run({ ...m, exe_sha256: undefined, manifest: sign({ binaries: { "chrome.dll": m.chrome_dll_sha256 } }) })).toBe("binary");
    expect(run({ ...m, chrome_dll_sha256: undefined, manifest: sign({ binaries: { "chrome.exe": m.exe_sha256 } }) })).toBe("binary");
    expect(run({ ...m, manifest: sign({ binaries: [m.exe_sha256] }) })).toBe("binary");
    expect(run({ ...m, manifest: sign({ archive: null }) })).toBe("archive");
    expect(run({ ...m, manifest: sign({ v: "1" }) })).toBe("malformed");
    // a launcher named like an inherited property is not "listed"
    expect(run({ ...m, binary: "constructor" })).toBe("binary");
  });

  it("an answer that names no launcher is held to the platform's default name, the one the install uses", () => {
    expect([defaultProLauncher("windows"), defaultProLauncher("linux"), defaultProLauncher("macos")]).toEqual(["chrome.exe", "chrome", "Chromium"]);
    const mac = signedAnswer({ tag: "pro-158.0.8127.36-r68", version: "158.0.8127.36", asset: "a.zip", sha256: "a".repeat(64), exe_sha256: "b".repeat(64), size: 5 }, "macos");
    expect(mac.binary).toBeUndefined();
    expect(outcome({ meta: mac, platform: "macos", want_revision: null, want_version: null })).toBe("ok");
    // signed for another launcher name: refused
    const other = signProManifest({ ...manifestPayload(mac, "macos"), binaries: { chrome: "b".repeat(64) } });
    expect(outcome({ meta: { ...mac, manifest: other }, platform: "macos", want_revision: null, want_version: null })).toBe("binary");
  });

  it("chrome.dll is required for Windows only", () => {
    const linux = signedAnswer({ tag: "pro-158.0.8127.36-r68", version: "158.0.8127.36", asset: "a.tar.xz", binary: "chrome", sha256: "a".repeat(64), exe_sha256: "b".repeat(64), size: 5 }, "linux");
    expect(outcome({ meta: linux, platform: "linux", want_revision: null, want_version: null })).toBe("ok");
  });
});

describe("what a version selector pins", () => {
  it("a revision and / or an exact version, never a major", () => {
    expect(proPins(undefined)).toEqual({ revision: undefined, version: undefined });
    expect(proPins("")).toEqual({ revision: undefined, version: undefined });
    expect(proPins("latest")).toEqual({ revision: undefined, version: undefined });
    expect(proPins("158")).toEqual({ revision: undefined, version: undefined });
    expect(proPins("158.0.8127.36")).toEqual({ revision: undefined, version: "158.0.8127.36" });
    expect(proPins("r68")).toEqual({ revision: "r68", version: undefined });
    expect(proPins(" R68 ")).toEqual({ revision: "r68", version: undefined });
    expect(proPins("158.0.8127.36-r68")).toEqual({ revision: "r68", version: "158.0.8127.36" });
    expect(proPins("158.0.8127.36-R68")).toEqual({ revision: "r68", version: "158.0.8127.36" });
    expect(proPins("pro-158.0.8127.36-r68")).toEqual({ revision: "r68", version: "158.0.8127.36" });
  });
});

describe("the pinned release keys", () => {
  it("are 32-byte keys in hex, and never the test key", () => {
    expect(pinnedBefore).not.toHaveProperty(TEST_KEY_ID);
    expect(Object.values(pinnedBefore)).not.toContain(TEST_PUBLIC_KEY_HEX);
    for (const [id, key] of Object.entries(pinnedBefore)) {
      expect(id).not.toBe("");
      expect(key, id).toMatch(/^[0-9a-f]{64}$/);
    }
  });

  it("cannot be added to or skipped through the environment, an option or the package's exports", async () => {
    // the modules that hold and use the keys read no environment variable and no file
    for (const file of ["../src/promanifest.ts", "../src/release.ts"]) {
      const src = readFileSync(new URL(file, import.meta.url), "utf8");
      expect(src, file).not.toMatch(/process\.env|readFileSync|node:fs/);
    }
    const pkg = await import("../src/index.js") as Record<string, unknown>;
    expect(pkg.ProManifestError).toBe(ProManifestError);
    for (const name of ["PRO_RELEASE_KEYS", "verifyProManifest", "ed25519Verify", "proPins"]) expect(pkg, name).not.toHaveProperty(name);
  }, 120_000); // loading the whole package takes a while on a busy machine
});
