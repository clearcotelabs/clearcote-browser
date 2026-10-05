// serve() in a Node process of its own that then exits WITHOUT calling close(), for the tests of what
// process exit leaves behind. Started by runServeChild() below through vite-node, so it imports the
// TypeScript sources directly.
//
//   SERVE_OPTS (JSON) -> serve(); prints {"pid": <the browser's pid>}, then process.exit(0).
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** Run this file in a child Node; resolves the JSON line it printed once the child has exited. */
export function runServeChild(opts: Record<string, unknown>, timeoutMs = 60_000): Promise<{ pid: number }> {
  const viteNode = join(dirname(createRequire(import.meta.url).resolve("vite-node/package.json")), "vite-node.mjs");
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [viteNode, fileURLToPath(import.meta.url)], {
      env: { ...process.env, SERVE_CHILD: "1", SERVE_OPTS: JSON.stringify(opts) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "", err = "";
    child.stdout.on("data", (c) => { out += c; });
    child.stderr.on("data", (c) => { err += c; });
    const timer = setTimeout(() => { child.kill(); reject(new Error(`serve child timed out\n${err}`)); }, timeoutMs);
    child.on("exit", (code) => {
      clearTimeout(timer);
      const line = out.split("\n").reverse().find((l) => l.startsWith("{"));
      if (code !== 0 || !line) return reject(new Error(`serve child exited ${code}\n${out}\n${err}`));
      resolve(JSON.parse(line));
    });
  });
}

if (process.env.SERVE_CHILD === "1") {
  const { serve } = await import("../../src/index.js");
  const srv = await serve(JSON.parse(process.env.SERVE_OPTS ?? "{}"));
  process.stdout.write(JSON.stringify({ pid: srv.pid }) + "\n");
  process.exit(0);
}
