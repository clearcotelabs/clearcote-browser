// Persona schema per profile folder: schema 8 for NEW profiles, the schema a profile was made with kept for
// profiles that already have a history (SDK 0.70.6).
//
// Schema 8 is the persona service's Windows machine model re-weighted on the 2026-11 /audit corpus (screen sizes and
// scaling, RAM, CPU/RAM pairs and touchscreens closer to real Windows machines; gen-156-s8b, 2026-11-23). The service
// default stays 7, and switching an EXISTING profile would give it a new screen, RAM and touch value on its next
// launch, so the schema is decided once per folder and remembered in <userDataDir>/clearcote.json (key "schema"):
//   - personaSchema set, or --fingerprint-schema=N in args: used, and remembered for later launches that leave it unset.
//   - unset, folder remembered: its remembered schema.
//   - unset, new folder (missing, empty, or no Chrome data yet: no "Local State", no "Default"): 8 when the engine
//     takes its persona from the service (156 r51+), else 7; remembered either way.
//   - unset, folder with Chrome data but nothing remembered (made before 0.70.6): 7, remembered.
// A folder on 7 launches exactly as before (no switch is added). Throwaway launches are new by definition. A
// pass-through launch (fingerprint: "off") is left alone. Python (_schemamode.py) and .NET (SchemaMode.cs) match.

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { engineSupportsSwitch } from "./launchopts.js";
import { PROFILE_STATE_FILE, hasChromeData } from "./noisemode.js";

export const NEW_PROFILE_SCHEMA = "8";
export const EARLIER_PROFILE_SCHEMA = "7";  // the persona service's default since 2026-10-26

function norm(v: unknown): string | null {
  const s = v === undefined || v === null ? "" : String(v).trim();
  return /^[1-9]$/.test(s) ? s : null;
}

/** The schema remembered in the folder, or null. */
export function readSchema(userDataDir: string): string | null {
  try {
    const s = JSON.parse(readFileSync(join(userDataDir, PROFILE_STATE_FILE), "utf8"));
    return s && typeof s === "object" ? norm(s.schema) : null;
  } catch {
    return null;
  }
}

/** Remember the schema, keeping any other keys in the file. Throws on an unwritable folder. */
export function writeSchema(userDataDir: string, schema: string): void {
  const file = join(userDataDir, PROFILE_STATE_FILE);
  let s: Record<string, unknown> = {};
  try {
    const cur = JSON.parse(readFileSync(file, "utf8"));
    if (cur && typeof cur === "object" && !Array.isArray(cur)) s = cur;
  } catch { /* missing or unreadable: start over */ }
  if (s.schema === schema) return;
  s.schema = schema;
  writeFileSync(file, JSON.stringify(s, null, 1) + "\n", "utf8");
}

/** The last --fingerprint-schema=N in a switch list, or null. */
export function schemaInArgs(args: readonly string[] | undefined): string | null {
  let found: string | null = null;
  for (const a of args ?? []) if (typeof a === "string" && a.startsWith("--fingerprint-schema=")) found = a.slice(21);
  return found === null ? null : norm(found);
}

/**
 * The `personaSchema` for this launch: the value to set, or undefined to add nothing. `userDataDir` null = a launch
 * with no folder (incognito), which is new. Remembers the schema in the folder, fail-soft. Returns an explicit
 * `requested` value unchanged; undefined when `args` already pin a schema or the schema is 7 (the service default:
 * the launch stays byte-identical to an earlier SDK's).
 */
export function resolvePersonaSchema<T>(
  requested: T | undefined,
  userDataDir: string | null,
  o: { exe?: string; args?: readonly string[]; passthrough?: boolean; quiet?: boolean; supports?: (exe: string | undefined) => boolean } = {},
): T | 8 | undefined {
  if (o.passthrough) return requested;
  // The persona service (156 r51+) is where schema 8 lives; an older engine derives its own identity.
  const supports = o.supports ?? ((exe) => engineSupportsSwitch(exe, "cc-persona-host-facts"));
  const explicit = requested === undefined || requested === null || (requested as unknown) === "" ? null : norm(requested);
  const pinned = schemaInArgs(o.args);
  let schema: string;
  if (explicit) schema = explicit;
  else if (pinned) schema = pinned;
  else {
    const remembered = userDataDir ? readSchema(userDataDir) : null;
    if (remembered) schema = remembered;
    else if (userDataDir && hasChromeData(userDataDir)) schema = EARLIER_PROFILE_SCHEMA;  // made before 0.70.6
    else schema = supports(o.exe) ? NEW_PROFILE_SCHEMA : EARLIER_PROFILE_SCHEMA;
  }
  if (userDataDir) {
    try {
      mkdirSync(userDataDir, { recursive: true });
      writeSchema(userDataDir, schema);
    } catch (e) {
      if (!o.quiet) process.stderr.write(`[clearcote] could not remember the persona schema in ${userDataDir} (continuing): ${String(e)}\n`);
    }
  }
  if (explicit) return requested;
  if (pinned || schema === EARLIER_PROFILE_SCHEMA) return undefined;
  return Number(schema) as 8;
}
