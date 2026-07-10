#!/usr/bin/env node
// Release smoke test (Node side): actually launch the browser and prove the engine starts.
//
// Launches the FREE build headless, and — when CLEARCOTE_LICENSE_KEY (or CCKEY) is set — the PRO
// build too, then reads a real navigator.userAgent (proves the process started AND runs JS). Exits
// non-zero on ANY failure so a release pipeline can gate on it. Imports whatever `clearcote` is
// installed in the current directory's node_modules, so run it after `npm i clearcote@X`.
// See docs/RELEASE-SMOKE-TEST.md.

import os from "node:os";

let launch, RELEASE;
try {
  ({ launch, RELEASE } = await import("clearcote"));
} catch (e) {
  console.log(`[NODE] import clearcote FAILED: ${e?.message ?? e}`);
  process.exit(2);
}

async function run(tier, key) {
  const kw = key ? { licenseKey: key } : {};
  try {
    const b = await launch({ headless: true, args: ["--no-sandbox"], quiet: true, ...kw });
    const p = await b.newPage();
    const ua = await p.evaluate(() => navigator.userAgent);
    await b.close();
    const ok = ua.includes("Chrome");
    console.log(`[NODE ${os.platform()}] ${tier}: ${ok ? "LAUNCH_OK" : "LAUNCH_FAIL"} | ${ua.slice(0, 58)}`);
    return ok;
  } catch (e) {
    console.log(`[NODE ${os.platform()}] ${tier}: LAUNCH_FAIL (${e?.message ?? e})`);
    return false;
  }
}

const key = process.env.CLEARCOTE_LICENSE_KEY || process.env.CCKEY;
console.log(`[NODE] clearcote ${RELEASE?.version ?? "?"} on ${os.platform()} node ${process.version}`);

const results = [await run("FREE", undefined)];
if (key) {
  results.push(await run("PRO ", key));
} else {
  console.log("[NODE] PRO : SKIPPED (set CLEARCOTE_LICENSE_KEY to test the licensed build)");
}

process.exit(results.every(Boolean) ? 0 : 1);
