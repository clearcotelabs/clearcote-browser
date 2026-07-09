#!/usr/bin/env node
// Thin launcher: run the Python `clearcote-mcp` stdio server, installing it on first use.
// The Python package pulls in `clearcote` (which downloads + SHA-256-verifies the stealth binary).
"use strict";
const { spawnSync, spawn } = require("node:child_process");

function pythons() {
  return process.platform === "win32" ? ["py", "python", "python3"] : ["python3", "python"];
}
function findPython() {
  for (const p of pythons()) {
    const r = spawnSync(p, ["-c", "import sys;print(sys.version_info[0])"], { encoding: "utf8" });
    if (r.status === 0 && (r.stdout || "").trim() === "3") return p;
  }
  return null;
}
function hasServer(py) {
  return spawnSync(py, ["-c", "import clearcote_mcp"], { stdio: "ignore" }).status === 0;
}

const py = findPython();
if (!py) {
  console.error("[clearcote-mcp] Python 3.10+ is required (not found). Install Python, then re-run.");
  process.exit(1);
}
if (!hasServer(py)) {
  console.error("[clearcote-mcp] installing the Python package `clearcote-mcp` (first run)…");
  const install = spawnSync(py, ["-m", "pip", "install", "--user", "--quiet", "clearcote-mcp"],
                            { stdio: "inherit" });
  if (install.status !== 0 || !hasServer(py)) {
    console.error("[clearcote-mcp] install failed. Run:  " + py + " -m pip install clearcote-mcp");
    process.exit(1);
  }
}
// Hand over stdio to the MCP server (stdio transport).
const child = spawn(py, ["-m", "clearcote_mcp"], { stdio: "inherit", env: process.env });
child.on("exit", (code) => process.exit(code == null ? 0 : code));
process.on("SIGINT", () => child.kill("SIGINT"));
process.on("SIGTERM", () => child.kill("SIGTERM"));
