// Canvas/WebGL noise mode per profile folder: one noise per profile for NEW profiles, per-site noise kept
// for profiles that already have a history (SDK 0.70.5).
//
// Measured 11-22 (SESSION-CONTEXT 317): with per-site noise (the engine default) a third-party frame (a
// captcha or anti-bot widget) reads a different canvas on every site that embeds it, which no real device
// does; with one noise per profile it reads one canvas everywhere, still different per profile, as
// CloakBrowser does. Switching an EXISTING profile would show every site a new canvas on its next launch,
// so the mode is decided once per folder and remembered in <userDataDir>/clearcote.json:
//   - noisePerProfile set (true/false): used, and remembered for later launches that leave it unset.
//   - unset, folder remembered: its remembered mode.
//   - unset, new folder (missing, empty, or no Chrome data yet: no "Local State", no "Default"): per
//     profile when the engine implements it (r60+), else per site; remembered either way, so the folder
//     keeps the noise it first showed.
//   - unset, folder with Chrome data but nothing remembered (made before 0.70.5): per site, remembered.
// Throwaway launches (launch()'s temp profile, serve() without a folder, ephemeralProfile: false) are
// new by definition. A pass-through launch (fingerprint: "off") is left alone.
// Python (_noisemode.py) and .NET (NoiseMode.cs) match this file.

import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { engineSupportsSwitch } from "./launchopts.js";

export const PROFILE_STATE_FILE = "clearcote.json";
export type NoiseMode = "per-profile" | "per-site";

/** The mode remembered in the folder, or null (nothing remembered, or a file that is not ours to read). */
export function readNoiseMode(userDataDir: string): NoiseMode | null {
  try {
    const s = JSON.parse(readFileSync(join(userDataDir, PROFILE_STATE_FILE), "utf8"));
    return s && (s.noise === "per-profile" || s.noise === "per-site") ? s.noise : null;
  } catch {
    return null;
  }
}

/** True when the folder already holds a Chrome profile (the browser writes these on its first run). */
export function hasChromeData(userDataDir: string): boolean {
  try {
    if (!existsSync(userDataDir)) return false;
    const names = readdirSync(userDataDir);
    return names.includes("Local State") || names.includes("Default");
  } catch {
    return false;
  }
}

/** Remember the mode, keeping any other keys in the file. Throws on an unwritable folder. */
export function writeNoiseMode(userDataDir: string, mode: NoiseMode): void {
  const file = join(userDataDir, PROFILE_STATE_FILE);
  let s: Record<string, unknown> = {};
  try {
    const cur = JSON.parse(readFileSync(file, "utf8"));
    if (cur && typeof cur === "object" && !Array.isArray(cur)) s = cur;
  } catch { /* missing or unreadable: start over */ }
  if (s.noise === mode) return;
  s.noise = mode;
  writeFileSync(file, JSON.stringify(s, null, 1) + "\n", "utf8");
}

/**
 * The noise mode for this launch, as `noisePerProfile` (true = one noise per profile). `userDataDir` null
 * = a launch with no folder (incognito), which is new. Remembers the result in the folder, fail-soft.
 */
export function resolveNoisePerProfile(
  requested: boolean | undefined,
  userDataDir: string | null,
  o: { exe?: string; passthrough?: boolean; quiet?: boolean; supports?: (exe: string | undefined) => boolean } = {},
): boolean | undefined {
  if (o.passthrough) return requested;
  const supports = o.supports ?? ((exe) => engineSupportsSwitch(exe, "fingerprint-noise-per-profile"));
  let perProfile: boolean;
  if (requested === true || requested === false) perProfile = requested;
  else {
    const remembered = userDataDir ? readNoiseMode(userDataDir) : null;
    if (remembered) perProfile = remembered === "per-profile";
    else if (userDataDir && hasChromeData(userDataDir)) perProfile = false;  // made before 0.70.5: keep its noise
    else perProfile = supports(o.exe);
  }
  if (userDataDir) {
    try {
      // A folder that does not exist yet is created here (Playwright would create it anyway): the browser
      // writes "Local State" on its first run, after which an unremembered folder would read as old.
      mkdirSync(userDataDir, { recursive: true });
      writeNoiseMode(userDataDir, perProfile ? "per-profile" : "per-site");
    } catch (e) {
      if (!o.quiet) process.stderr.write(`[clearcote] could not remember the noise mode in ${userDataDir} (continuing): ${String(e)}\n`);
    }
  }
  return perProfile;
}
