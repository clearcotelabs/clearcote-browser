// `clearcote connect`: the helper pin (and its parity with the Python SDK), the code format, and the
// download -> SHA-256 check -> unpack -> cache path against a local server with a fake helper archive.
import { describe, it, expect, beforeAll, afterAll, afterEach, vi } from "vitest";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { HELPER, ensureHelper, tarCommand, helperFiles, helperPlatform, normalizePairCode } from "../src/connect.js";
import { commandUsage, main } from "../src/cli-commands.js";

const TMP = mkdtempSync(join(tmpdir(), "cc-connect-"));
let server: Server;
let base = "";
let hits = 0;
const files = new Map<string, Buffer>();

/** A helper archive shaped like the real one: <top>/lib/clearcote-helper.js + <top>/runtime/node. */
function fakeArchive(plat: string, version: string, format: "tar.gz" | "zip"): { name: string; data: Buffer } {
  const top = `clearcote-helper-${version}-${plat}`;
  const src = join(TMP, `src-${plat}-${format}`);
  mkdirSync(join(src, top, "lib"), { recursive: true });
  mkdirSync(join(src, top, "runtime"), { recursive: true });
  writeFileSync(join(src, top, "lib", "clearcote-helper.js"), "// fake helper\n");
  writeFileSync(join(src, top, "runtime", plat.startsWith("win") ? "node.exe" : "node"), "fake node\n");
  const name = `${top}.${format}`;
  const out = join(TMP, name);
  // bsdtar (Windows, macOS) writes zip with --format zip; GNU tar never needs to here.
  execFileSync(tarCommand(), format === "zip" ? ["-a", "--format", "zip", "-cf", out, "-C", src, top] : ["-czf", out, "-C", src, top]);
  return { name, data: readFileSync(out) };
}

const sha = (b: Buffer) => createHash("sha256").update(b).digest("hex");

beforeAll(async () => {
  server = createServer((req, res) => {
    hits++;
    const f = files.get((req.url || "").replace(/^\/dl\//, ""));
    if (!f) {
      res.writeHead(404);
      return res.end();
    }
    res.writeHead(200, { "Content-Length": f.length });
    res.end(f);
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
  base = `http://127.0.0.1:${(server.address() as { port: number }).port}/dl/`;
});
afterAll(async () => {
  await new Promise((r) => server.close(r));
  rmSync(TMP, { recursive: true, force: true });
});
afterEach(() => {
  delete process.env.CLEARCOTE_HELPER_DIR;
  vi.restoreAllMocks();
});

describe("the pin", () => {
  it("names one archive per platform, for its own version and tag", () => {
    expect(HELPER.baseUrl).toBe(`https://github.com/clearcotelabs/clearcote-profile-manager/releases/download/helper-v${HELPER.version}/`);
    expect(Object.keys(HELPER.assets).sort()).toEqual(["linux-arm64", "linux-x64", "mac-arm64", "mac-x64", "win-arm64", "win-x64"]);
    for (const [plat, a] of Object.entries(HELPER.assets)) {
      expect(a.asset).toBe(`clearcote-helper-${HELPER.version}-${plat}.${plat.startsWith("win") ? "zip" : "tar.gz"}`);
      expect(a.sha256).toMatch(/^[0-9a-f]{64}$/);
      expect(a.size).toBeGreaterThan(1_000_000);
    }
  });

  it("matches the Python SDK's HELPER field for field", () => {
    const py = readFileSync(join(__dirname, "..", "..", "python", "clearcote", "_connect.py"), "utf8");
    expect(py).toContain(`"version": "${HELPER.version}"`);
    expect(py).toContain(`"base_url": "${HELPER.baseUrl}"`);
    for (const [plat, a] of Object.entries(HELPER.assets)) {
      expect(py).toContain(`"${plat}": {"asset": "${a.asset}", "sha256": "${a.sha256}", "size": ${a.size}}`);
    }
  });
});

describe("helperPlatform", () => {
  it("maps Node's names to the release's", () => {
    expect(helperPlatform("win32", "x64")).toBe("win-x64");
    expect(helperPlatform("darwin", "arm64")).toBe("mac-arm64");
    expect(helperPlatform("linux", "arm64")).toBe("linux-arm64");
    expect(helperPlatform("freebsd", "x64")).toBeNull();
    expect(helperPlatform("linux", "ia32")).toBeNull();
  });
});

describe("normalizePairCode", () => {
  it("takes the code however it was typed", () => {
    expect(normalizePairCode("4f7-k2q")).toBe("4F7-K2Q");
    expect(normalizePairCode("4F7K2Q")).toBe("4F7-K2Q");
    expect(normalizePairCode(" 4f7 k2q ")).toBe("4F7-K2Q");
  });
  it("refuses what the page never shows", () => {
    for (const bad of ["", "4F7-K2", "4F7-K2QQ", "4F0-K2Q", "1F7-K2Q", "OOO-OOO", "4F7_K2Q"]) expect(normalizePairCode(bad)).toBeNull();
  });
});

describe("ensureHelper", () => {
  it("downloads, checks, unpacks once, then uses the cache", async () => {
    const plat = "linux-x64";
    const a = fakeArchive(plat, "9.9.9", "tar.gz");
    files.set(a.name, a.data);
    const helper = { version: "9.9.9", baseUrl: base, assets: { [plat]: { asset: a.name, sha256: sha(a.data), size: a.data.length } } };
    const cacheRoot = join(TMP, "cache1");
    hits = 0;
    const f = await ensureHelper({ cacheRoot, quiet: true, helper, plat });
    expect(f.script).toBe(join(cacheRoot, "helper", `9.9.9-${plat}`, `clearcote-helper-9.9.9-${plat}`, "lib", "clearcote-helper.js"));
    expect(readFileSync(f.script, "utf8")).toBe("// fake helper\n");
    expect(existsSync(f.node)).toBe(true);
    expect(hits).toBe(1);
    const again = await ensureHelper({ cacheRoot, quiet: true, helper, plat });
    expect(again).toEqual(f);
    expect(hits).toBe(1); // no second download
  });

  it.runIf(process.platform === "win32")("unpacks the Windows zip", async () => {
    const plat = "win-x64";
    const a = fakeArchive(plat, "9.9.9", "zip");
    files.set(a.name, a.data);
    const helper = { version: "9.9.9", baseUrl: base, assets: { [plat]: { asset: a.name, sha256: sha(a.data), size: a.data.length } } };
    const f = await ensureHelper({ cacheRoot: join(TMP, "cache-zip"), quiet: true, helper, plat });
    expect(f.node.endsWith("node.exe")).toBe(true);
    expect(existsSync(f.node)).toBe(true);
  });

  it("refuses an archive that does not match the pin, and leaves nothing behind", async () => {
    const plat = "linux-x64";
    const a = fakeArchive(plat, "9.9.8", "tar.gz");
    files.set(a.name, a.data);
    const helper = { version: "9.9.8", baseUrl: base, assets: { [plat]: { asset: a.name, sha256: "0".repeat(64), size: a.data.length } } };
    const cacheRoot = join(TMP, "cache2");
    await expect(ensureHelper({ cacheRoot, quiet: true, helper, plat })).rejects.toThrow(/does not match its pinned SHA-256/);
    expect(existsSync(join(cacheRoot, "helper", `9.9.8-${plat}`))).toBe(false);
  });

  it("CLEARCOTE_HELPER_DIR uses a helper you unpacked yourself, without downloading", async () => {
    const plat = "linux-x64";
    const dir = join(TMP, "own");
    mkdirSync(join(dir, "lib"), { recursive: true });
    writeFileSync(join(dir, "lib", "clearcote-helper.js"), "");
    process.env.CLEARCOTE_HELPER_DIR = dir;
    hits = 0;
    expect(await ensureHelper({ plat })).toEqual(helperFiles(dir, plat));
    expect(hits).toBe(0);
    process.env.CLEARCOTE_HELPER_DIR = join(TMP, "nothing-here");
    await expect(ensureHelper({ plat })).rejects.toThrow(/no lib\/clearcote-helper.js/);
  });
});

describe("clearcote connect (CLI)", () => {
  it("has its usage and help", () => {
    const help = commandUsage("connect");
    expect(help).toContain("clearcote connect [<code>] [--port <n>] [--allow-origin <origin>]... [--no-open]");
    expect(help).toContain("https://app.clearcotelabs.com");
  });

  it("refuses a code the page never shows, before downloading anything", async () => {
    let err = "";
    vi.spyOn(process.stderr, "write").mockImplementation(((c: string) => { err += String(c); return true; }) as never);
    const exit = vi.spyOn(process, "exit").mockImplementation(((c: number) => { throw new Error(`exit ${c}`); }) as never);
    hits = 0;
    await expect(main(["connect", "hello"])).rejects.toThrow("exit 1");
    expect(err).toContain("is not a code the profile manager shows");
    expect(hits).toBe(0);
    await expect(main(["connect", "4F7-K2Q", "extra"])).rejects.toThrow("exit 2");
    await expect(main(["connect", "--port", "x"])).rejects.toThrow("exit 2");
    expect(exit).toHaveBeenCalled();
  });
});
