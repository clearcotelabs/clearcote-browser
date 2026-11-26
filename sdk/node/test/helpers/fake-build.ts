// A local stand-in for the PRO download route and the archive it points at, for the install-lock tests.
//
// The archive is a small zip holding every file a healthy install must have (no real browser). The server
// counts archive downloads, and can hold the route's answer until `metaBarrier` clients have asked, so that
// several installers reach the cache check at the same moment.
// `metaStatus` makes the route refuse with its own JSON error, and `metaChallenge` answers like the site's
// host answers a burst from one address (a 403 HTML challenge page with x-vercel-mitigated).
//
// Every answer carries a release manifest (docs/PRO-RELEASE-MANIFEST.md) signed with the TEST key of
// sdk/testdata/pro_manifest_vectors.json, for exactly what the answer says. That key is pinned nowhere in the
// SDK: a test pins it with pinTestReleaseKey(), which writes into the release.ts instance the code under test
// uses. `answer` changes the answer after it was signed and `archiveBody` serves other bytes than were hashed.
import { createHash, createPrivateKey, sign, type KeyObject } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { writeManifest } from "../../src/download.js";

export const BINARY = process.platform === "win32" ? "chrome.exe" : "chrome";
export const TAG = "pro-0.0.0-r1";
/** The /download/pro `platform` of this OS. */
export const PRO_PLATFORM = process.platform === "win32" ? "windows" : process.platform === "darwin" ? "macos" : "linux";

// ── The test release key ─────────────────────────────────────────────────────────────────────────────────
const VECTORS = JSON.parse(readFileSync(new URL("../../../testdata/pro_manifest_vectors.json", import.meta.url), "utf8")) as {
  key_id: string; public_key_hex: string; private_seed_hex: string;
};
export const TEST_KEY_ID = VECTORS.key_id;
export const TEST_PUBLIC_KEY_HEX = VECTORS.public_key_hex;

/** An Ed25519 private key from its 32-byte seed (PKCS#8 header + seed). */
export function ed25519PrivateKey(seedHex: string): KeyObject {
  return createPrivateKey({ key: Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), Buffer.from(seedHex, "hex")]), format: "der", type: "pkcs8" });
}
const TEST_PRIVATE_KEY = ed25519PrivateKey(VECTORS.private_seed_hex);

/**
 * Pin the test key in PRO_RELEASE_KEYS. Call it AFTER vi.resetModules() + import("../src/download.js"): the
 * import below then resolves to the release.ts instance that download.ts uses. Not part of the package: the
 * SDK itself has no way to add a key.
 */
export async function pinTestReleaseKey(): Promise<void> {
  const release = await import("../../src/release.js");
  release.PRO_RELEASE_KEYS[TEST_KEY_ID] = TEST_PUBLIC_KEY_HEX;
}

/** base64url(payload) "." base64url(Ed25519 signature over the ASCII of the first part). */
export function signProManifest(payload: unknown, key: KeyObject = TEST_PRIVATE_KEY): string {
  const text = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
  return `${text}.${sign(null, Buffer.from(text, "ascii"), key).toString("base64url")}`;
}

export interface ProAnswer {
  tag?: string; version?: string; revision?: string; asset?: string; binary?: string;
  sha256?: string; exe_sha256?: string; chrome_dll_sha256?: string; size?: number;
  [more: string]: unknown;
}

/** The manifest payload a release would sign for this answer. The revision is the answer's, else its tag's "-rN". */
export function manifestPayload(answer: ProAnswer, platform: string = PRO_PLATFORM): Record<string, unknown> {
  const binary = answer.binary ?? (platform === "windows" ? "chrome.exe" : platform === "macos" ? "Chromium" : "chrome");
  return {
    v: 1,
    product: "clearcote-pro",
    version: answer.version,
    revision: answer.revision ?? /-(r\d+)$/.exec(answer.tag ?? "")?.[1],
    platform,
    archive: { name: answer.asset, sha256: answer.sha256, size: answer.size },
    binaries: { [binary]: answer.exe_sha256, ...(platform === "windows" ? { "chrome.dll": answer.chrome_dll_sha256 } : {}) },
    key: TEST_KEY_ID,
    issued: "2026-11-26",
  };
}

/** The answer as a signing release serves it: with `revision`, `platform` and a manifest for exactly its values. */
export function signedAnswer<T extends ProAnswer>(answer: T, platform: string = PRO_PLATFORM): T & { revision?: string; platform: string; manifest: string } {
  const payload = manifestPayload(answer, platform);
  return { ...answer, revision: payload.revision as string | undefined, platform, manifest: signProManifest(payload) };
}
const CRITICAL = process.platform === "win32"
  ? ["chrome.exe", "chrome.dll", "chrome_elf.dll", "icudtl.dat", "snapshot_blob.bin", "resources.pak"]
  : ["icudtl.dat", "snapshot_blob.bin", "resources.pak"];
export const NAMES = [...new Set([BINARY, ...CRITICAL])].sort();

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});

function crc32(b: Buffer): number {
  let c = 0xffffffff;
  for (const byte of b) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** A zip with uncompressed ("stored") entries: no dependency needed to make one. */
export function storedZip(files: Record<string, Buffer>): Buffer {
  const locals: Buffer[] = [];
  const centrals: Buffer[] = [];
  let offset = 0;
  for (const [name, data] of Object.entries(files)) {
    const n = Buffer.from(name, "utf8");
    const crc = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x21, 12); // 1980-01-01
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(n.length, 26);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0x21, 14);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(n.length, 28);
    central.writeUInt32LE(offset, 42);
    locals.push(local, n, data);
    centrals.push(central, n);
    offset += local.length + n.length + data.length;
  }
  const cd = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(Object.keys(files).length, 8);
  end.writeUInt16LE(Object.keys(files).length, 10);
  end.writeUInt32LE(cd.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, cd, end]);
}

export const fakeArchive = (): Buffer => storedZip(Object.fromEntries(NAMES.map((n) => [n, Buffer.alloc(64, "x")])));

/** What another installer leaves behind: <base>/browser, its manifest and the .verified marker. */
export function verifiedTree(base: string): string {
  const browser = path.join(base, "browser");
  mkdirSync(browser, { recursive: true });
  for (const name of NAMES) writeFileSync(path.join(browser, name), Buffer.alloc(64, "x"));
  writeManifest(base, browser);
  writeFileSync(path.join(base, ".verified"), `${"0".repeat(64)}\n`);
  return path.join(browser, BINARY);
}

export interface FakeBuild {
  url: string;
  sha: string;
  /** Archive downloads served so far. */
  readonly archiveHits: number;
  /** Requests to the download route so far. */
  readonly metaHits: number;
  /** Set to an error status to make the route refuse with its own JSON error. */
  metaStatus: number;
  /** True: a 403 challenge page like the site's host sends a burst from one address. */
  metaChallenge: boolean;
  /** Resolves on the first request to the download route. */
  metaSeen: Promise<void>;
  close(): Promise<void>;
}

export async function startFakeBuild(
  opts: {
    metaBarrier?: number; archiveDelayMs?: number; sha?: string; exeSha?: string; onArchive?: () => void;
    /** Change the answer after it was signed (drop the manifest, swap it, edit a hash). */
    answer?: (signed: ProAnswer) => unknown;
    /** Serve these bytes as the archive; the answer and its manifest still name the real archive's hash. */
    archiveBody?: Buffer;
  } = {},
): Promise<FakeBuild> {
  const archive = fakeArchive();
  const sha = opts.sha ?? createHash("sha256").update(archive).digest("hex");
  const codeSha = createHash("sha256").update(Buffer.alloc(64, "x")).digest("hex");
  let archiveHits = 0;
  let metaHits = 0;
  const control = { metaStatus: 200, metaChallenge: false };
  let seen!: () => void;
  const metaSeen = new Promise<void>((r) => { seen = r; });
  const waiting: Array<() => void> = [];
  const releaseAll = () => { for (const go of waiting.splice(0)) go(); };
  const fallback = setTimeout(releaseAll, 30_000);
  fallback.unref();
  let url = "";
  const server = createServer((req, res) => {
    if (req.url?.startsWith("/api/v1/download/pro")) {
      seen();
      metaHits++;
      const answer = () => {
        if (control.metaChallenge) { // what the site's host answers a burst from one address
          res.writeHead(403, { "content-type": "text/html; charset=utf-8", "x-vercel-mitigated": "challenge", "retry-after": "30" });
          res.end("<!DOCTYPE html><title>Vercel Security Checkpoint</title>");
          return;
        }
        if (control.metaStatus !== 200) {
          res.writeHead(control.metaStatus, { "content-type": "application/json" });
          res.end(JSON.stringify({ error: "Licence not active." }));
          return;
        }
        res.setHeader("content-type", "application/json");
        // Signed for what it says, also when `sha` / `exeSha` name hashes the archive does not have: the
        // manifest check passes and the archive / launcher hash checks behind it are the ones that refuse.
        const signed = signedAnswer({ tag: TAG, version: "0.0.0", url: `${url}/fake.zip`, sha256: sha,
          exe_sha256: opts.exeSha ?? codeSha, chrome_dll_sha256: codeSha,
          asset: "fake.zip", archive: "zip", binary: BINARY, size: archive.length });
        res.end(JSON.stringify(opts.answer ? opts.answer(signed) : signed));
      };
      waiting.push(answer);
      if (waiting.length >= (opts.metaBarrier ?? 1)) releaseAll();
      return;
    }
    if (req.url === "/fake.zip") {
      archiveHits++;
      opts.onArchive?.(); // e.g. another installer finishes while this one downloads
      setTimeout(() => { // long enough for a second installer to arrive meanwhile
        const body = opts.archiveBody ?? archive;
        res.setHeader("content-type", "application/zip");
        res.setHeader("content-length", String(body.length));
        res.end(body);
      }, opts.archiveDelayMs ?? 0);
      return;
    }
    res.statusCode = 404;
    res.end();
  });
  // Node closes an idle keep-alive socket after 5 s; a request sent on it at that moment can be resent by fetch and
  // reach the route twice, which made "one request per machine" counts flaky on slow CI runners.
  server.keepAliveTimeout = 120_000;
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  return {
    url,
    sha,
    get archiveHits() { return archiveHits; },
    get metaHits() { return metaHits; },
    get metaStatus() { return control.metaStatus; },
    set metaStatus(v: number) { control.metaStatus = v; },
    get metaChallenge() { return control.metaChallenge; },
    set metaChallenge(v: boolean) { control.metaChallenge = v; },
    metaSeen,
    close: () => new Promise<void>((r) => { clearTimeout(fallback); server.closeAllConnections(); server.close(() => r()); }),
  };
}
