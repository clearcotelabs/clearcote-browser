"use strict";
// The launcher end to end, with stub commands on PATH: which route it takes to the Python server (an installed
// server, uvx, pipx, pip) and what it says when there is none. POSIX only: the stubs are sh scripts.
//
//   node --test mcp/npm/test/cli.test.js
//   CLEARCOTE_MCP_CLI=/path/to/other/cli.js node --test mcp/npm/test/cli.test.js   (test another launcher)
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const CLI = process.env.CLEARCOTE_MCP_CLI || path.join(__dirname, "..", "cli.js");
const VERSION = require("../package.json").version;
const SPEC = "clearcote-mcp>=" + VERSION;
const skip = process.platform === "win32" && "the stub commands are sh scripts";

// Each stub logs the calls that matter to $STUB_LOG, one line each. Probes (version checks) are not logged.
const STUBS = {
  python3: [
    "#!/bin/sh",
    'case "$*" in',
    "  *version_info*) echo 3 ;;",
    '  *base_prefix*) echo "${STUB_VENV:-0}" ;;',
    '  *"import clearcote_mcp"*)',
    '    if [ -n "$STUB_INSTALLED" ]; then echo "$STUB_INSTALLED"; exit 0; fi',
    '    if [ -f "$STUB_DIR/pip-installed" ]; then echo "$STUB_VERSION"; exit 0; fi',
    "    exit 1 ;;",
    '  "-m pip install"*)',
    '    echo "python3 $*" >> "$STUB_LOG"',
    '    if [ "$STUB_PIP" = refuse ]; then echo "error: externally-managed-environment" >&2; exit 1; fi',
    '    : > "$STUB_DIR/pip-installed" ;;',
    '  "-m clearcote_mcp") echo "python3 $*" >> "$STUB_LOG"; exit "${STUB_EXIT:-0}" ;;',
    '  *) echo "unexpected python3 call: $*" >&2; exit 2 ;;',
    "esac",
    "",
  ].join("\n"),
  uvx: [
    "#!/bin/sh",
    'if [ "$1" = "--version" ]; then echo "uv 0.0.0"; exit 0; fi',
    'echo "uvx $*" >> "$STUB_LOG"',
    'exit "${STUB_EXIT:-0}"',
    "",
  ].join("\n"),
  pipx: [
    "#!/bin/sh",
    'if [ "$1" = "--version" ]; then echo "1.0.0"; exit 0; fi',
    'echo "pipx $*" >> "$STUB_LOG"',
    'exit "${STUB_EXIT:-0}"',
    "",
  ].join("\n"),
};

// Run the launcher with only `stubs` on PATH (node itself is started by its absolute path).
function launch(t, stubs, env = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ccmcp-cli-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  for (const name of stubs) fs.writeFileSync(path.join(dir, name), STUBS[name], { mode: 0o755 });
  const log = path.join(dir, "calls.log");
  const r = spawnSync(process.execPath, [CLI], {
    encoding: "utf8", input: "", timeout: 30000,
    env: { PATH: dir, STUB_LOG: log, STUB_DIR: dir, STUB_VERSION: VERSION, ...env },
  });
  const calls = fs.existsSync(log) ? fs.readFileSync(log, "utf8").split("\n").filter(Boolean) : [];
  return { status: r.status, stdout: r.stdout, stderr: r.stderr, calls };
}

test("an installed, new-enough server runs directly, as before", { skip }, (t) => {
  const r = launch(t, ["python3", "uvx"], { STUB_INSTALLED: VERSION });
  assert.deepStrictEqual(r.calls, ["python3 -m clearcote_mcp"]);
  assert.strictEqual(r.status, 0);
  assert.strictEqual(r.stdout, "", "stdout is the MCP channel");
});

test("uvx runs the server when it is on PATH; pip is never tried", { skip }, (t) => {
  const r = launch(t, ["python3", "uvx", "pipx"], { STUB_PIP: "refuse" });
  assert.deepStrictEqual(r.calls, [`uvx --from ${SPEC} clearcote-mcp`]);
  assert.strictEqual(r.status, 0);
  assert.strictEqual(r.stdout, "");
});

test("uvx works without any Python on PATH", { skip }, (t) => {
  const r = launch(t, ["uvx"]);
  assert.deepStrictEqual(r.calls, [`uvx --from ${SPEC} clearcote-mcp`]);
  assert.strictEqual(r.status, 0);
});

test("pipx runs the server when there is no uvx", { skip }, (t) => {
  const r = launch(t, ["python3", "pipx"], { STUB_PIP: "refuse" });
  assert.deepStrictEqual(r.calls, [`pipx run --spec ${SPEC} clearcote-mcp`]);
  assert.strictEqual(r.status, 0);
  assert.strictEqual(r.stdout, "");
});

test("the server's exit code is passed on", { skip }, (t) => {
  assert.strictEqual(launch(t, ["uvx"], { STUB_EXIT: "3" }).status, 3);
});

test("pip installs for the user and runs the server when there is no uvx or pipx, as before", { skip }, (t) => {
  const r = launch(t, ["python3"]);
  assert.deepStrictEqual(r.calls, [`python3 -m pip install --user --quiet --upgrade ${SPEC}`, "python3 -m clearcote_mcp"]);
  assert.strictEqual(r.status, 0);
  assert.strictEqual(r.stdout, "");
});

test("inside a virtual environment pip installs without --user", { skip }, (t) => {
  const r = launch(t, ["python3"], { STUB_VENV: "1" });
  assert.deepStrictEqual(r.calls, [`python3 -m pip install --quiet --upgrade ${SPEC}`, "python3 -m clearcote_mcp"]);
  assert.strictEqual(r.status, 0);
});

test("when pip refuses (PEP 668) and there is no uvx or pipx, it says how to fix it", { skip }, (t) => {
  const r = launch(t, ["python3"], { STUB_PIP: "refuse" });
  assert.strictEqual(r.status, 1);
  assert.deepStrictEqual(r.calls, [`python3 -m pip install --user --quiet --upgrade ${SPEC}`]);
  for (const hint of ["uv", "pipx", "externally-managed-environment", "python3 -m venv"]) {
    assert.ok(r.stderr.includes(hint), `the message mentions ${hint}:\n${r.stderr}`);
  }
  assert.strictEqual(r.stdout, "");
});

test("with no Python, uvx or pipx at all, it names all three", { skip }, (t) => {
  const r = launch(t, []);
  assert.strictEqual(r.status, 1);
  for (const hint of ["uv", "pipx", "Python 3.10"]) {
    assert.ok(r.stderr.includes(hint), `the message mentions ${hint}:\n${r.stderr}`);
  }
});
