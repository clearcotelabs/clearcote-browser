// `clearcote login --device` against a local stand-in for the site's device endpoints, every branch of
// the contract: pending, slow_down (+5 s, kept), 429 (+5 s, Retry-After honoured), expired_token,
// access_denied, invalid_request, a dropped connection and a 5xx mid-poll (retried), an unreachable or
// older server, Ctrl-C, and the expiry `clearcote info --json` then reports. Mirrors
// sdk/python/tests/test_device_login.py.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { existsSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { USAGE, deviceLogin, main } from "../src/cli-commands.js";
import { licenseKeyPath } from "../src/license.js";
import { startFakeDevice, DEVICE_CODE, LICENSE_KEY, USER_CODE, type FakeDevice, type FakeDeviceOptions } from "./helpers/fake-device.js";
import { startOrigin } from "./helpers/proxies.js";
import { tempDir } from "./helpers/temp.js";

const SDK_VERSION = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version as string;
const ENV_KEYS = ["HOME", "USERPROFILE", "CLEARCOTE_CACHE", "CLEARCOTE_BINARY", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_RELEASE_CHANNEL", "CLEARCOTE_LICENSE_API"];
const saved: Record<string, string | undefined> = {};
let home: string;
let out = "";
let errOut = "";
const cleanups: Array<() => Promise<void>> = [];

beforeEach(() => {
  home = tempDir("cc-device-home-");
  for (const k of ENV_KEYS) saved[k] = process.env[k];
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.CLEARCOTE_CACHE = join(home, "cache");
  for (const k of ["CLEARCOTE_BINARY", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_RELEASE_CHANNEL", "CLEARCOTE_LICENSE_API"]) delete process.env[k];
  out = "";
  errOut = "";
  vi.spyOn(process.stdout, "write").mockImplementation(((c: string | Uint8Array) => { out += String(c); return true; }) as never);
  vi.spyOn(process.stderr, "write").mockImplementation(((c: string | Uint8Array) => { errOut += String(c); return true; }) as never);
  vi.spyOn(process, "exit").mockImplementation(((code?: number) => { throw new Error(`exit ${code}`); }) as never);
});
afterEach(async () => {
  for (const c of cleanups.splice(0)) await c();
  for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
  vi.restoreAllMocks();
});

async function serve(script: string[], o: FakeDeviceOptions = {}): Promise<FakeDevice> {
  const fake = await startFakeDevice(script, o);
  cleanups.push(() => fake.close());
  process.env.CLEARCOTE_LICENSE_API = fake.url;
  return fake;
}

/** A sleep that records the interval instead of waiting it (and honours the abort signal). */
function recordingSleep(seen: number[]) {
  return async (s: number, signal?: AbortSignal) => {
    seen.push(s);
    if (signal?.aborted) throw Object.assign(new Error("aborted"), { name: "AbortError" });
  };
}

const keyFile = () => join(home, ".clearcote", "license.key");
const metaFile = () => join(home, ".clearcote", "license.meta.json");

async function infoJson(): Promise<Record<string, any>> {
  out = "";
  await main(["info", "--quick", "--json"]);
  return JSON.parse(out);
}

describe("clearcote login --device", () => {
  it("is documented", () => {
    expect(USAGE).toContain("clearcote login --device");
  });

  it("saves the key exactly like paste login, and never prints it", async () => {
    const fake = await serve(["pending", "pending", "ok"]);
    const sleeps: number[] = [];
    await deviceLogin({ sleep: recordingSleep(sleeps) });
    // the same file, the same bytes and the same permissions saveLicenseKey (paste login) writes
    expect(keyFile()).toBe(licenseKeyPath());
    expect(readFileSync(keyFile(), "utf8")).toBe(LICENSE_KEY + "\n");
    if (process.platform !== "win32") expect(statSync(keyFile()).mode & 0o777).toBe(0o600);
    expect(out).toContain(`saved to ${keyFile()}`);
    expect(out).toContain("plan pro, expires 2027-01-31T00:00:00.000Z");
    expect(errOut).toContain(`${fake.url}/device?code=${USER_CODE}`);
    expect(errOut).toContain(`Code: ${USER_CODE}`);
    expect(out + errOut).not.toContain(LICENSE_KEY);
    expect(fake.log[0].path).toBe("/api/v1/device/code");
    expect(fake.log[0].body).toEqual({ client_name: "clearcote-node", client_version: SDK_VERSION });
    expect(fake.log[0].headers["user-agent"]).toBe(`clearcote-sdk-node/${SDK_VERSION}`);
    expect(fake.tokenPolls().map((e) => e.body)).toEqual([{ device_code: DEVICE_CODE }, { device_code: DEVICE_CODE }, { device_code: DEVICE_CODE }]);
    expect(sleeps).toEqual([5, 5, 5]);
  });

  it("runs from the command line with the real timer", async () => {
    await serve(["pending", "ok"], { interval: 1 });
    const listeners = process.listenerCount("SIGINT");
    const t0 = Date.now();
    await main(["login", "--device"]);
    expect(Date.now() - t0).toBeGreaterThanOrEqual(1900); // two 1 s waits really happened
    expect(readFileSync(keyFile(), "utf8")).toBe(LICENSE_KEY + "\n");
    expect(process.listenerCount("SIGINT")).toBe(listeners); // its Ctrl-C handler is gone again
  });

  it("slow_down and 429 each add five seconds, for good", async () => {
    await serve(["slow", "pending", "429", "pending", "ok"]);
    const sleeps: number[] = [];
    await deviceLogin({ sleep: recordingSleep(sleeps) });
    expect(sleeps).toEqual([5, 10, 10, 15, 15]);
    expect(existsSync(keyFile())).toBe(true);
  });

  it("honours a 429's Retry-After", async () => {
    await serve(["429", "ok"], { retryAfter: 30 });
    const sleeps: number[] = [];
    await deviceLogin({ sleep: recordingSleep(sleeps) });
    expect(sleeps).toEqual([5, 30]);
  });

  for (const [step, message] of [
    ["expired", "the code expired before it was approved. Nothing was saved. Run `clearcote login --device` again."],
    ["denied", "the sign-in was denied in the browser. Nothing was saved."],
    ["invalid", "the licence server rejected the device login request (invalid_request). Nothing was saved."],
  ] as const) {
    it(`${step}: exits 1 and saves nothing`, async () => {
      const fake = await serve(["pending", step]);
      await expect(deviceLogin({ sleep: recordingSleep([]) })).rejects.toThrow("exit 1");
      expect(errOut).toContain(`clearcote: ${message}`);
      expect(existsSync(keyFile())).toBe(false);
      expect(existsSync(metaFile())).toBe(false);
      expect(fake.tokenPolls()).toHaveLength(2);
    });
  }

  it("retries a dropped connection and a 5xx mid-poll, and says so once", async () => {
    const fake = await serve(["drop", "500", "pending", "ok"]);
    const sleeps: number[] = [];
    await deviceLogin({ sleep: recordingSleep(sleeps) });
    expect(errOut.split("note: licence server unreachable").length - 1).toBe(1);
    expect(fake.tokenPolls()).toHaveLength(4);
    expect(sleeps).toEqual([5, 5, 5, 5]);
    expect(readFileSync(keyFile(), "utf8").trim()).toBe(LICENSE_KEY);
  });

  it("an unreachable server fails before any code", async () => {
    process.env.CLEARCOTE_LICENSE_API = "http://127.0.0.1:1";
    const sleeps: number[] = [];
    await expect(deviceLogin({ sleep: recordingSleep(sleeps) })).rejects.toThrow("exit 1");
    expect(errOut).toContain("could not reach the licence server at http://127.0.0.1:1");
    expect(errOut).toContain("Nothing was saved.");
    expect(sleeps).toEqual([]);
    expect(existsSync(keyFile())).toBe(false);
  });

  it("a server without device login says so", async () => {
    await serve([], { codeStatus: 404 });
    await expect(deviceLogin({ sleep: recordingSleep([]) })).rejects.toThrow("exit 1");
    expect(errOut).toContain("does not support device login (HTTP 404)");
  });

  it("Ctrl-C while waiting cancels, exits 130 and saves nothing", async () => {
    const fake = await serve(["pending", "ok"]);
    const listeners = process.listenerCount("SIGINT");
    const onSpy = vi.spyOn(process, "on");
    const calls: number[] = [];
    const sleep = async (s: number, signal?: AbortSignal) => {
      calls.push(s);
      if (calls.length === 2) {
        // the CLI's own SIGINT listener, as Ctrl-C would call it
        const handler = onSpy.mock.calls.find(([ev]) => ev === "SIGINT")?.[1] as () => void;
        handler();
      }
      if (signal?.aborted) throw Object.assign(new Error("aborted"), { name: "AbortError" });
    };
    await expect(deviceLogin({ sleep })).rejects.toThrow("exit 130");
    expect(errOut).toContain("clearcote: cancelled. Nothing was saved.");
    expect(existsSync(keyFile())).toBe(false);
    expect(fake.tokenPolls()).toHaveLength(1); // the approval after Ctrl-C was never fetched
    expect(process.listenerCount("SIGINT")).toBe(listeners);
  });

  it("stops polling once the code has expired locally", async () => {
    const fake = await serve(Array(50).fill("pending"), { expiresIn: 12 });
    let t = 0;
    await expect(deviceLogin({ sleep: async (s) => { t += s * 1000; }, clock: () => t })).rejects.toThrow("exit 1");
    expect(errOut).toContain("the code expired before it was approved");
    expect(fake.tokenPolls()).toHaveLength(3); // at 5, 10 and 15 s; none once the 12 s were up
  });

  it("a key and --device together is a usage error", async () => {
    await expect(main(["login", "--device", "cc_lic_x"])).rejects.toThrow("exit 2");
    expect(errOut).toContain("pass a key or --device, not both");
  });

  it("info --json reports the expiry device login recorded, for that key only", async () => {
    await serve(["ok"]);
    await deviceLogin({ sleep: recordingSleep([]) });
    let lic = (await infoJson()).license;
    expect(lic.source).toBe("file");
    expect(lic.expiry).toMatchObject({ expiresAt: "2027-01-31T00:00:00.000Z", source: "device-login" });
    expect(lic.expiry.recordedAt).toBeTruthy();
    expect(lic.expiry.note).toContain("no licence endpoint reports");
    out = "";
    await main(["info", "--quick"]);
    expect(out).toContain("Expires         2027-01-31T00:00:00.000Z  (as reported at device login)");
    expect(readFileSync(metaFile(), "utf8")).not.toContain(LICENSE_KEY);

    process.env.CLEARCOTE_LICENSE_KEY = "cc_lic_some_other_key_9999";
    lic = (await infoJson()).license;
    expect(lic.source).toBe("env");
    expect(lic.expiry.source).toBe("unknown");
    expect("expiresAt" in lic.expiry).toBe(false);
    delete process.env.CLEARCOTE_LICENSE_KEY;

    const api = await startOrigin(() => ({ body: '{"used":0,"limit":null,"plan":"pro"}' }));
    cleanups.push(() => api.close());
    process.env.CLEARCOTE_LICENSE_API = `http://127.0.0.1:${api.port}`;
    await main(["login", "cc_lic_pasted_key_abcdefgh"]);
    expect(existsSync(metaFile())).toBe(false);
    expect((await infoJson()).license.expiry.source).toBe("unknown");
    await main(["logout"]);
    expect((await infoJson()).license.expiry).toBeUndefined();
  });

  it("no expiry, then logout removes both files", async () => {
    await serve(["ok"], { expiresAt: null, plan: "free" });
    await deviceLogin({ sleep: recordingSleep([]) });
    expect(out).toContain("plan free, no expiry");
    const exp = (await infoJson()).license.expiry;
    expect(exp).toMatchObject({ source: "device-login", expiresAt: null });
    out = "";
    await main(["info", "--quick"]);
    expect(out).toContain("Expires         never");
    await main(["logout"]);
    expect(existsSync(metaFile())).toBe(false);
    expect(existsSync(keyFile())).toBe(false);
  });
});
