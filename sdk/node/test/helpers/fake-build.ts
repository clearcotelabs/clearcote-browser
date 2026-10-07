// A local stand-in for the PRO download route and the archive it points at, for the install-lock tests.
//
// The archive is a small zip holding every file a healthy install must have (no real browser). The server
// counts archive downloads, and can hold the route's answer until `metaBarrier` clients have asked, so that
// several installers reach the cache check at the same moment.
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { writeManifest } from "../../src/download.js";

export const BINARY = process.platform === "win32" ? "chrome.exe" : "chrome";
export const TAG = "pro-0.0.0-r1";
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
  for (const name of NAMES) writeFileSync(path.join(browser, name), Buffer.alloc(64, "y"));
  writeManifest(base, browser);
  writeFileSync(path.join(base, ".verified"), `${"0".repeat(64)}\n`);
  return path.join(browser, BINARY);
}

export interface FakeBuild {
  url: string;
  sha: string;
  /** Archive downloads served so far. */
  readonly archiveHits: number;
  /** Resolves on the first request to the download route. */
  metaSeen: Promise<void>;
  close(): Promise<void>;
}

export async function startFakeBuild(opts: { metaBarrier?: number; archiveDelayMs?: number; sha?: string; exeSha?: string } = {}): Promise<FakeBuild> {
  const archive = fakeArchive();
  const sha = opts.sha ?? createHash("sha256").update(archive).digest("hex");
  let archiveHits = 0;
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
      const answer = () => {
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify({ tag: TAG, version: "0.0.0", url: `${url}/fake.zip`, sha256: sha, exe_sha256: opts.exeSha, asset: "fake.zip", archive: "zip", binary: BINARY, size: archive.length }));
      };
      waiting.push(answer);
      if (waiting.length >= (opts.metaBarrier ?? 1)) releaseAll();
      return;
    }
    if (req.url === "/fake.zip") {
      archiveHits++;
      setTimeout(() => { // long enough for a second installer to arrive meanwhile
        res.setHeader("content-type", "application/zip");
        res.setHeader("content-length", String(archive.length));
        res.end(archive);
      }, opts.archiveDelayMs ?? 0);
      return;
    }
    res.statusCode = 404;
    res.end();
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  return {
    url,
    sha,
    get archiveHits() { return archiveHits; },
    metaSeen,
    close: () => new Promise<void>((r) => { clearTimeout(fallback); server.closeAllConnections(); server.close(() => r()); }),
  };
}
