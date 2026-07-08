// Linux font wiring.
//
// The Linux release bundles metric-compatible font clones (Segoe UI->Selawik,
// Arial->Arimo, Times New Roman->Tinos, …) under `<binDir>/fonts/`, together with a
// self-contained `fonts.conf.template`. On a bare server/container the Windows families
// (and even the standard fontconfig metric-alias rules) are absent, so a page asking for
// "Segoe UI" collapses to a single default — a detectable render + an absent-font tell.
//
// At launch we materialize the template (substituting the real fonts dir + a writable
// cache dir) and point FONTCONFIG_FILE at it, so the clones resolve without depending on
// the host's /etc/fonts. No-op on non-Linux and on older binaries that ship no `fonts/`.

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";

/** Returns `{ FONTCONFIG_FILE }` on Linux when the font bundle is present, else `{}`. */
export function linuxFontEnv(exePath: string): Record<string, string> {
  if (process.platform !== "linux") return {};
  const fontsDir = join(dirname(exePath), "fonts");
  const template = join(fontsDir, "fonts.conf.template");
  if (!existsSync(template)) return {};
  try {
    const cacheDir = join(tmpdir(), "cc-fc-cache");
    mkdirSync(cacheDir, { recursive: true });
    const conf = readFileSync(template, "utf8")
      .split("@FONTS_DIR@").join(fontsDir)
      .split("@CACHE_DIR@").join(cacheDir);
    const confPath = join(fontsDir, "fonts.generated.conf");
    writeFileSync(confPath, conf);
    return { FONTCONFIG_FILE: confPath };
  } catch {
    return {}; // never block a launch on font wiring
  }
}

type EnvMap = { [key: string]: string | undefined };

/**
 * Build the `env` to pass to Playwright's launch so the bundled fonts resolve.
 * Merges process.env (Playwright replaces the env when `env` is set, so we must include it),
 * the bundled-font FONTCONFIG_FILE, then any caller-supplied `env` (caller wins).
 * Returns `undefined` when there is nothing to add (preserve Playwright's default env).
 */
export function fontLaunchEnv(exePath: string, userEnv?: EnvMap): EnvMap | undefined {
  const fontEnv = linuxFontEnv(exePath);
  if (Object.keys(fontEnv).length === 0 && !userEnv) return undefined;
  return { ...process.env, ...fontEnv, ...(userEnv ?? {}) };
}
