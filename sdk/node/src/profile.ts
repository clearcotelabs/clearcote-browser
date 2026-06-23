// Saved, launchable personas.
//
// A `Profile` bundles a set of launch options — the fingerprint persona (seed, GPU, brand, …)
// AND its `canvasBridge` config — under a name you can persist and re-launch as one coherent
// identity:
//
//   import { Profile, launch } from "clearcote";
//
//   await new Profile("acct-1", {
//     fingerprint: "acct-1",
//     gpuVendor: "Google Inc. (Intel)",
//     gpuRenderer: "ANGLE (Intel, Intel(R) UHD Graphics ... D3D11)",
//     canvasBridge: { url: "ws://127.0.0.1:9099", auth: "user:secret" },
//   }).save();
//
//   const browser = await Profile.load("acct-1").launch({ headless: false }); // or launch({ profile: "acct-1" })
//
// Profiles are plain JSON at `~/.clearcote/profiles/<name>.json` (override the dir with
// CLEARCOTE_PROFILE_DIR). The persona's claimed GPU, the bridge endpoint, and the bridge's
// GPU-keyed cache stay coherent because they travel together in one file. Fingerprint option
// keys are normalized on load, so a profile written by the Python SDK (snake_case) loads
// correctly here (camelCase) and vice versa.
//
// SECURITY: a profile is a plaintext file that may hold credentials (e.g. `canvasBridge.auth`)
// — it is written 0600 but is NOT encrypted; do not commit or share it. Treat profile *names*
// and profile *files* as trusted input (a loaded profile can set any launch option).

import { mkdirSync, readFileSync, writeFileSync, existsSync, readdirSync, chmodSync } from "node:fs";
import { homedir } from "node:os";
import { join, dirname, sep } from "node:path";
import { FINGERPRINT_KEYS, type FingerprintOptions } from "./fingerprint.js";
import type { Browser, BrowserContext } from "playwright-core";
import type { LaunchOptions, PersistentContextOptions } from "./index.js";

/** Directory saved profiles live in (override with `CLEARCOTE_PROFILE_DIR`). */
export const PROFILE_DIR = process.env.CLEARCOTE_PROFILE_DIR ?? join(homedir(), ".clearcote", "profiles");

/** A persisted set of launch options (a saved persona). */
export type ProfileOptions = FingerprintOptions & Record<string, unknown>;

const FINGERPRINT_KEY_SET = new Set<string>(FINGERPRINT_KEYS as string[]);
const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

function toCamel(key: string): string {
  return key.replace(/_([a-z0-9])/g, (_, c: string) => c.toUpperCase());
}

/** Map snake_case fingerprint keys (e.g. a Python-written profile) to this SDK's camelCase. */
function normalizeKeys(options: Record<string, unknown>): ProfileOptions {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(options)) {
    const camel = toCamel(k);
    out[FINGERPRINT_KEY_SET.has(camel) ? camel : k] = v;
  }
  return out as ProfileOptions;
}

function profilePath(nameOrPath: string): string {
  // a path (has a separator, or ends in .json) is used verbatim; a bare name must be a safe slug
  // (no separators, no "..") so an untrusted name cannot traverse out of PROFILE_DIR.
  if (nameOrPath.includes(sep) || nameOrPath.includes("/") || nameOrPath.endsWith(".json")) {
    return nameOrPath;
  }
  if (!SAFE_NAME.test(nameOrPath)) {
    throw new Error(`invalid profile name '${nameOrPath}' — use [A-Za-z0-9._-] (or pass an explicit path)`);
  }
  return join(PROFILE_DIR, `${nameOrPath}.json`);
}

/** A named bundle of {@link launch} options (a saved persona). */
export class Profile {
  name: string;
  options: ProfileOptions;

  constructor(name: string, options: ProfileOptions = {}) {
    this.name = name;
    this.options = { ...options };
  }

  /** Merge in more options; returns this for chaining. */
  set(options: ProfileOptions): this {
    Object.assign(this.options, options);
    return this;
  }

  /** `~/.clearcote/profiles/<name>.json` (or the explicit path the name resolves to). */
  get path(): string {
    return profilePath(this.name);
  }

  /** Persist as JSON (defaults to {@link path}). Returns the file path. The dir is created 0700
   * and the file written 0600 (it may hold secrets). */
  save(path?: string): string {
    const dest = path ?? this.path;
    mkdirSync(dirname(dest), { recursive: true });
    writeFileSync(dest, `${JSON.stringify({ name: this.name, options: this.options }, null, 2)}\n`, { mode: 0o600 });
    try {
      chmodSync(dirname(dest), 0o700);
      chmodSync(dest, 0o600);
    } catch {
      /* best-effort on platforms without POSIX modes */
    }
    return dest;
  }

  /** Load a saved profile by name (under PROFILE_DIR) or by explicit path. */
  static load(nameOrPath: string): Profile {
    const data = JSON.parse(readFileSync(profilePath(nameOrPath), "utf8")) as {
      name?: string;
      options?: Record<string, unknown>;
    };
    const base = nameOrPath.split(/[\\/]/).pop() ?? nameOrPath;
    const name = data.name ?? (base.endsWith(".json") ? base.slice(0, -5) : base);
    return new Profile(name, normalizeKeys(data.options ?? {}));
  }

  /** Launch this persona; explicit `overrides` win over the saved options. */
  async launch(overrides: ProfileOptions = {}): Promise<Browser> {
    const { launch } = await import("./index.js");
    return launch({ ...this.options, ...overrides } as LaunchOptions);
  }

  /** Launch this persona with a persistent profile directory. */
  async launchPersistentContext(userDataDir: string, overrides: ProfileOptions = {}): Promise<BrowserContext> {
    const { launchPersistentContext } = await import("./index.js");
    return launchPersistentContext(userDataDir, { ...this.options, ...overrides } as PersistentContextOptions);
  }
}

/** Names of the saved profiles under {@link PROFILE_DIR}. */
export function listProfiles(): string[] {
  if (!existsSync(PROFILE_DIR)) return [];
  return readdirSync(PROFILE_DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => f.slice(0, -5))
    .sort();
}

/** Convenience wrapper for {@link Profile.load}. */
export function loadProfile(nameOrPath: string): Profile {
  return Profile.load(nameOrPath);
}

/** Resolve the saved (camelCase-normalized) options for a `profile` option (a name, path, or Profile). */
export function resolveProfileOptions(profile: string | Profile): ProfileOptions {
  return profile instanceof Profile ? normalizeKeys({ ...profile.options }) : { ...Profile.load(profile).options };
}
