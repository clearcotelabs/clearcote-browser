/**
 * LIVE: a form submitted in a launchPersistentContext() profile is remembered in Default/Web Data, as
 * Chrome does. The engine's own defaults (r54) have autofill off and save 0 rows; the SDK seeds it on
 * (./src/autofillpref.ts). Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary (and a licence is
 * available for a PRO build: CLEARCOTE_LICENSE_KEY or ~/.clearcote/license.key).
 *
 *   CLEARCOTE_LIVE_ENGINE=/path/to/chrome npx vitest run test/autofill.live.test.ts
 *
 * Mirrors sdk/python/tests/test_autofill_live.py.
 */
import { execFileSync } from "node:child_process";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { launchPersistentContext } from "../src/index.js";

const LIVE = process.env.CLEARCOTE_LIVE_ENGINE;

const FORM = `<!doctype html><form method="post" action="/done">
<input name="name" autocomplete="name"><input name="email" type="email" autocomplete="email">
<input name="city" autocomplete="address-level2"><button>Send</button></form>`;

/** Rows in the autofill table, read with Node's built-in SQLite in a child (it needs a flag on Node 22). */
function autofillRows(webData: string): number {
  const out = execFileSync(process.execPath, ["--experimental-sqlite", "--no-warnings", "-e",
    `const {DatabaseSync}=require("node:sqlite");const db=new DatabaseSync(process.argv[1],{readOnly:true});console.log(db.prepare("select count(*) n from autofill").get().n)`,
    webData], { encoding: "utf8" });
  return Number(out.trim());
}

/** Launch on `udd`, submit the form on a local page, close gracefully; the autofill rows Chrome saved. */
async function submitAndCount(udd: string): Promise<{ rows: number; autofill: unknown }> {
  const server: Server = createServer((req, res) => {
    res.writeHead(200, { "content-type": "text/html" });
    res.end(req.url === "/done" ? "<p>ok</p>" : FORM);
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
  try {
    const site = `http://127.0.0.1:${(server.address() as AddressInfo).port}/`;
    const context = await launchPersistentContext(udd, { executablePath: LIVE, headless: true, quiet: true,
      fingerprint: "autofill-live-1", geoip: false });
    try {
      const page = context.pages()[0] ?? (await context.newPage());
      await page.goto(site);
      await page.fill('[name="name"]', "Ada Lovelace");
      await page.fill('[name="email"]', "ada@example.com");
      await page.fill('[name="city"]', "London");
      await Promise.all([page.waitForURL(/\/done$/), page.click("button")]);
      await page.waitForTimeout(1500);
    } finally {
      await context.close(); // graceful: Chrome writes Web Data on the way out
    }
  } finally {
    server.close();
  }
  const dir = join(udd, "Default");
  return {
    rows: autofillRows(join(dir, "Web Data")),
    autofill: JSON.parse(readFileSync(join(dir, "Preferences"), "utf8")).autofill,
  };
}

describe.runIf(LIVE)("live: autofill is on in a persistent profile", () => {
  const dirs: string[] = [];
  const newDir = () => { const d = mkdtempSync(join(tmpdir(), "cc-autofill-live-")); dirs.push(d); return d; };
  afterAll(() => { for (const d of dirs) rmSync(d, { recursive: true, force: true }); });

  it("remembers what was typed into a submitted form", async () => {
    const { rows, autofill } = await submitAndCount(newDir());
    console.log(`autofill rows (seeded on): ${rows}`);
    expect(autofill).toMatchObject({ profile_enabled: true, credit_card_enabled: true });
    expect(rows).toBeGreaterThan(0);
  }, 240000);

  it("a profile that turned it off keeps it off (and saves nothing, as the engine's default did)", async () => {
    const udd = newDir();
    mkdirSync(join(udd, "Default"));
    writeFileSync(join(udd, "Default", "Preferences"), JSON.stringify({ autofill: { profile_enabled: false } }));
    const { rows, autofill } = await submitAndCount(udd);
    console.log(`autofill rows (explicit off): ${rows}`);
    expect(autofill).toMatchObject({ profile_enabled: false, credit_card_enabled: true });
    expect(rows).toBe(0);
  }, 240000);
});
