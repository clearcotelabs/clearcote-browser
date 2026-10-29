// A lease is checked in when the process ends without draining its event loop: Ctrl+C, SIGTERM and
// process.exit() skip "beforeExit", so before this a free or capped plan's slot stayed taken for its full
// lease TTL (6 min). Real child processes against a local fake lease server; the licence module is bundled
// once with esbuild so the child runs it as plain Node.
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { spawn } from "node:child_process";
import { createServer, type Server } from "node:http";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import { tempDir } from "./helpers/temp.js";

const dir = tempDir("cc-exitrel-");
const checkins: string[] = [];
let n = 0;
let server: Server;
let base = "";

const CHILD = `
import { acquireLease } from "./license.mjs";
const mode = process.argv[2];
if (mode === "other") process.on("SIGINT", () => process.exit(130)); // an app's own handler owns the exit
await acquireLease({ quiet: true });
process.stdout.write("ready\\n");
if (mode === "exit") process.exit(0);
setInterval(() => {}, 1000); // keep running until signalled
`;

beforeAll(async () => {
  await build({
    entryPoints: [fileURLToPath(new URL("../src/license.ts", import.meta.url))],
    bundle: true, platform: "node", format: "esm", outfile: join(dir, "license.mjs"), logLevel: "silent",
  });
  writeFileSync(join(dir, "child.mjs"), CHILD);
  server = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const ep = (req.url ?? "").split("/").pop();
      const b = body ? JSON.parse(body) : {};
      res.setHeader("content-type", "application/json");
      if (ep === "checkout") {
        const id = `L${++n}`;
        res.end(JSON.stringify({ lease_id: id, token: "t.sig", exp: Math.floor(Date.now() / 1000) + 900,
          lease_ttl_sec: 360, heartbeat_interval_sec: 60, concurrency: { used: 1, limit: 1 }, lease_scope: "browser" }));
        return;
      }
      if (ep === "checkin") checkins.push(String(b.lease_id));
      res.end("{}");
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
  const addr = server.address();
  base = `http://127.0.0.1:${typeof addr === "object" && addr ? addr.port : 0}`;
}, 60_000);

afterAll(() => new Promise<void>((r) => server.close(() => r())));

/** Run the child to the point it holds a lease, then `act` on it; resolves with how it ended and the lease id. */
function run(mode: string, act: (child: ReturnType<typeof spawn>) => void): Promise<{ code: number | null; signal: string | null; lease: string }> {
  const home = tempDir("cc-exitrel-home-");
  const before = n;
  const child = spawn(process.execPath, [join(dir, "child.mjs"), mode], {
    cwd: dir,
    env: { ...process.env, HOME: home, USERPROFILE: home, CLEARCOTE_LICENSE_API: base, CLEARCOTE_LICENSE_KEY: `cc_lic_free_${mode}`,
      CLEARCOTE_INSTANCE_ID: "" },
    stdio: ["ignore", "pipe", "inherit"],
  });
  return new Promise((resolve) => {
    child.stdout!.on("data", (d) => { if (String(d).includes("ready")) act(child); });
    child.on("exit", (code, signal) => resolve({ code, signal, lease: `L${before + 1}` }));
  });
}

describe("lease check-in when the process ends early", () => {
  it("process.exit() checks the lease in (exit hook, child process)", async () => {
    const r = await run("exit", () => {});
    expect(r.code).toBe(0);
    expect(checkins).toContain(r.lease);
  }, 30_000);

  // child.kill() on Windows is TerminateProcess, not a signal the process can handle.
  it.skipIf(process.platform === "win32")("SIGINT with no other listener checks in, then dies by the signal", async () => {
    const r = await run("idle", (c) => c.kill("SIGINT"));
    expect(r.signal).toBe("SIGINT");
    expect(checkins).toContain(r.lease);
  }, 30_000);

  it.skipIf(process.platform === "win32")("SIGTERM checks in, then dies by the signal", async () => {
    const r = await run("idle", (c) => c.kill("SIGTERM"));
    expect(r.signal).toBe("SIGTERM");
    expect(checkins).toContain(r.lease);
  }, 30_000);

  it.skipIf(process.platform === "win32")("SIGINT with the app's own handler: its process.exit wins and the exit hook checks in", async () => {
    const r = await run("other", (c) => c.kill("SIGINT"));
    expect(r.code).toBe(130);
    expect(checkins).toContain(r.lease);
  }, 30_000);
});
