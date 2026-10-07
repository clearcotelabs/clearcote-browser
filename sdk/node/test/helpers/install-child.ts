// proEnsureBinary() in a Node process of its own, for the cross-process install-lock tests. Started by
// startInstallChild() below through vite-node, so it imports the TypeScript sources directly.
//
//   INSTALL_API, INSTALL_CACHE -> proEnsureBinary("test-key", ...); prints {"path": ...}.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export interface ChildResult { code: number | null; out: string; err: string; path?: string }

/** Start this file in a child Node; resolves once the child has exited. */
export function startInstallChild(apiBase: string, cacheDir: string, timeoutMs = 120_000): Promise<ChildResult> {
  const viteNode = join(dirname(createRequire(import.meta.url).resolve("vite-node/package.json")), "vite-node.mjs");
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [viteNode, fileURLToPath(import.meta.url)], {
      env: { ...process.env, INSTALL_CHILD: "1", INSTALL_API: apiBase, INSTALL_CACHE: cacheDir },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "", err = "";
    child.stdout.on("data", (c) => { out += c; });
    child.stderr.on("data", (c) => { err += c; });
    const timer = setTimeout(() => child.kill(), timeoutMs);
    child.on("exit", (code) => {
      clearTimeout(timer);
      const line = out.split("\n").reverse().find((l) => l.startsWith("{"));
      resolve({ code, out, err, path: line ? (JSON.parse(line) as { path: string }).path : undefined });
    });
  });
}

if (process.env.INSTALL_CHILD === "1") {
  const { proEnsureBinary } = await import("../../src/download.js");
  const p = await proEnsureBinary("test-key", { apiBase: process.env.INSTALL_API, cacheDir: process.env.INSTALL_CACHE, quiet: true });
  process.stdout.write(JSON.stringify({ path: p }) + "\n");
}
