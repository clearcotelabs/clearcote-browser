// Runs the served-window fit in a Node process of its own, so a test can take Node's global WebSocket
// away for real (`--no-experimental-websocket`, i.e. what Node 20 looks like). Started by
// runFitChild() below through vite-node, so it imports the TypeScript sources directly.
//
//   FIT_MODE=fit    FIT_WS_URL, FIT_OPTS (JSON)  -> fitServedWindow() against that endpoint
//   FIT_MODE=serve  FIT_SERVE_OPTS (JSON)        -> serve(), then the first page's geometry as a
//                                                   client attaching afterwards sees it
//
// Prints one JSON line: { webSocket: typeof globalThis.WebSocket, ... }.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const READ = "[[screen.width, screen.height], [screen.availWidth, screen.availHeight], " +
  "[innerWidth, innerHeight], [outerWidth, outerHeight], [screenX, screenY]]";

async function main(): Promise<void> {
  const webSocket = typeof (globalThis as { WebSocket?: unknown }).WebSocket;
  if (process.env.FIT_MODE === "fit") {
    const { fitServedWindow } = await import("../../src/geometry.js");
    const fit = await fitServedWindow(process.env.FIT_WS_URL, JSON.parse(process.env.FIT_OPTS ?? "{}"));
    process.stdout.write(JSON.stringify({ webSocket, fit }) + "\n");
    return;
  }
  const { serve } = await import("../../src/index.js");
  const { chromium } = await import("playwright-core");
  const srv = await serve(JSON.parse(process.env.FIT_SERVE_OPTS ?? "{}"));
  try {
    const browser = await chromium.connectOverCDP(srv.cdpUrl);
    try {
      const page = browser.contexts()[0].pages()[0] ?? await browser.contexts()[0].newPage();
      await page.goto("data:text/html,<body style='margin:0'>geo</body>");
      await page.waitForTimeout(700);   // first paint: innerWidth reads 0 before it
      const m = (await page.evaluate(READ)) as number[][];
      process.stdout.write(JSON.stringify({
        webSocket, pid: srv.pid, screen: m[0], avail: m[1], inner: m[2], outer: m[3], pos: m[4],
      }) + "\n");
    } finally {
      await browser.close();
    }
  } finally {
    await srv.close();
  }
}

/**
 * Run this file in a child Node without a global WebSocket (`--no-experimental-websocket` where Node
 * has one; Node 20 has none to begin with). Resolves the JSON line it printed.
 */
export function runFitChild(env: Record<string, string>, timeoutMs = 60_000): Promise<Record<string, unknown>> {
  const viteNode = join(dirname(createRequire(import.meta.url).resolve("vite-node/package.json")), "vite-node.mjs");
  const flags = typeof (globalThis as { WebSocket?: unknown }).WebSocket === "undefined" ? [] : ["--no-experimental-websocket"];
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [...flags, viteNode, fileURLToPath(import.meta.url)], {
      env: { ...process.env, ...env, FIT_CHILD: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "", err = "";
    child.stdout.on("data", (c) => { out += c; });
    child.stderr.on("data", (c) => { err += c; });
    const timer = setTimeout(() => { child.kill(); reject(new Error(`fit child timed out\n${err}`)); }, timeoutMs);
    child.on("exit", (code) => {
      clearTimeout(timer);
      const line = out.split("\n").reverse().find((l) => l.startsWith("{"));
      if (code !== 0 || !line) return reject(new Error(`fit child exited ${code}\n${out}\n${err}`));
      resolve(JSON.parse(line));
    });
  });
}

// No process.exit() on success: the child has to wind down by itself, so a connection the fit left
// open shows up as a timeout.
if (process.env.FIT_CHILD === "1") {
  main().catch((e) => {
    process.stderr.write(String((e as Error)?.stack ?? e) + "\n");
    process.exitCode = 1;
  });
}
