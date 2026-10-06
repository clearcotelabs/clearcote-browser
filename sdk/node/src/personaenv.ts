// Keep the persona off the browser's own command line (engine patch 1021, "env mode").
//
// A browser's command line is readable by any local user (/proc/<pid>/cmdline on Linux, the process
// environment block on Windows), and Clearcote's carries the seed, every persona override, proxy
// credentials (--proxy-auth, --socks5-credentials) and the canvas-bridge token. An engine with patch
// 1021 already moves those switches off every CHILD process's command line: they travel in the
// CLEARCOTE_PERSONA_ARGS environment variable, which the children inherit. With --persona-from-env the
// browser process reads them from that variable too, so the launcher can keep them off the browser's
// own command line as well. That is what this module does.
//
// It acts only when the engine implements --persona-from-env (probed in the binary, like every other
// new engine switch). An older engine gets the switches on its command line exactly as before.
//
// The payload format and the switch list mirror components/ungoogled/persona_transport.cc: base64 of
// `name\0value\0name\0value\0` with switch names that engine accepts. The engine stops the launch on a
// payload it cannot decode or on a name outside its list, so the list here must never be wider than
// the engine's. Chromium keeps the LAST copy of a repeated switch on a command line, while the engine
// adopts the FIRST matching entry of the payload, so each name goes in once, with its last value.
//
// Turn it off with `personaEnv: false` (launch options) or CLEARCOTE_PERSONA_ENV=0. Mirrors
// sdk/python/clearcote/_personaenv.py.

import { engineSupportsSwitch } from "./launchopts.js";

export const ENV_VAR = "CLEARCOTE_PERSONA_ARGS";
export const FROM_ENV_SWITCH = "persona-from-env";
/** The engine's own switch back to the pre-1021 behaviour. */
export const KILL_SWITCH = "disable-persona-env-transport";
export const OPT_OUT_ENV = "CLEARCOTE_PERSONA_ENV";
/** The engine's cap for the variable it republishes for its children (Windows caps one environment
 * variable at 32,767 characters). Above it the switches simply stay on the command line. */
export const MAX_PAYLOAD = 30000;

const NAMES: ReadonlySet<string> = new Set([
  "disable-canvas-noise", "disable-fingerprint-noise", "disable-fingerprint-voices",
  "disable-gpu-fingerprint", "disable-gpu-string-spoof", "proxy-auth", "socks5-credentials",
  "webrtc-ip",
]);

type Env = Record<string, string | undefined>;
type Probe = (exe: string | undefined, name: string) => boolean;

/** Whether the engine carries switch `name` (no leading dashes) in the environment. */
export function isTransported(name: string): boolean {
  return name === "fingerprint" || name.startsWith("fingerprint-") || name.startsWith("canvas-bridge-")
    || NAMES.has(name);
}

/** `[name, value]` of a `--name[=value]` argument, else null (a URL or a non-switch). */
function switchOf(arg: unknown): [string, string] | null {
  if (typeof arg !== "string" || !arg.startsWith("--") || arg.length === 2) return null;
  const body = arg.slice(2);
  const eq = body.indexOf("=");
  const name = eq < 0 ? body : body.slice(0, eq);
  return name ? [name, eq < 0 ? "" : body.slice(eq + 1)] : null;
}

/** The CLEARCOTE_PERSONA_ARGS value for `[[name, value], ...]`. */
export function encode(entries: ReadonlyArray<readonly [string, string]>): string {
  const nul = Buffer.from([0]);
  const raw = Buffer.concat(entries.flatMap(([n, v]) => [Buffer.from(n, "utf8"), nul, Buffer.from(v, "utf8"), nul]));
  return raw.toString("base64");
}

/** The inverse of {@link encode} (tests and diagnostics). */
export function decode(payload: string): Array<[string, string]> {
  const raw = Buffer.from(payload, "base64");
  const parts: Buffer[] = [];
  for (let start = 0; ;) {
    const end = raw.indexOf(0, start);
    if (end < 0) { parts.push(raw.subarray(start)); break; }
    parts.push(raw.subarray(start, end));
    start = end + 1;
  }
  if (parts[parts.length - 1].length !== 0 || parts.length % 2 !== 1) throw new Error("malformed persona payload");
  const utf8 = new TextDecoder("utf-8", { fatal: true });
  const out: Array<[string, string]> = [];
  for (let i = 0; i < parts.length - 1; i += 2) out.push([utf8.decode(parts[i]), utf8.decode(parts[i + 1])]);
  return out;
}

/** `personaEnv` option: unset follows CLEARCOTE_PERSONA_ENV (on unless it says 0/false/off/no). */
export function wanted(enabled?: boolean | null): boolean {
  if (enabled !== undefined && enabled !== null) return !!enabled;
  return !["0", "false", "off", "no"].includes((process.env[OPT_OUT_ENV] ?? "").trim().toLowerCase());
}

/**
 * The args and env for a launch of `exe`.
 *
 * When the engine implements --persona-from-env and env mode is wanted, the persona switches leave
 * `args` and travel in `env[CLEARCOTE_PERSONA_ARGS]`, and --persona-from-env is added. Otherwise both
 * come back unchanged (`env` the very object passed in). `env` may be undefined (the launch inherits
 * process.env); a changed env is a new object based on it, never the caller's. `supports` is the
 * engine probe (tests pass a stub).
 */
export function apply(
  exe: string | undefined,
  args: readonly string[] | null | undefined,
  env?: Env,
  enabled?: boolean | null,
  supports?: Probe,
): { args: string[]; env: Env | undefined } {
  const all = [...(args ?? [])];
  const unchanged = { args: all, env };
  if (!wanted(enabled)) return unchanged;
  const names = all.map(switchOf).filter((s) => s !== null).map((s) => s[0]);
  // the caller already chose a transport, or asked the engine for the old one
  if (names.includes(FROM_ENV_SWITCH) || names.includes(KILL_SWITCH)) return unchanged;
  const last = new Map<string, number>();
  all.forEach((arg, i) => {
    const s = switchOf(arg);
    if (s && isTransported(s[0])) last.set(s[0], i);
  });
  if (last.size === 0) return unchanged;
  if (!(supports ?? engineSupportsSwitch)(exe, FROM_ENV_SWITCH)) return unchanged;
  const payload = encode([...last.values()].sort((a, b) => a - b).map((i) => switchOf(all[i])!));
  if (payload.length > MAX_PAYLOAD) return unchanged;
  const kept = all.filter((a) => { const s = switchOf(a); return !(s && isTransported(s[0])); });
  return { args: [...kept, `--${FROM_ENV_SWITCH}`], env: { ...(env ?? process.env), [ENV_VAR]: payload } };
}
