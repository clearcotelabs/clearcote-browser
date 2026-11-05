// The engine checks a run token's (and a persona token's) nbf / exp against THIS computer's clock and refuses to
// start when the token is not valid yet or already expired. A clock that runs far behind or ahead then shows up as a
// licence refusal ("no valid license lease for this run") that no licence key fixes (CR-2VFYCD: a Windows clock a
// minute slow refused a free key and a Pro key alike). So before a launch, check the token the same way the engine
// will and, when it would refuse, say that the clock is off, by how much, and how to fix it.
//
// The servers backdate nbf by 15 minutes, so only a clock off by more than that is caught. `iat` is the server's
// time when it minted the token, so `iat - now` is a lower bound on how far behind this clock runs ("at least").
//
// Mirrors the Python SDK's _clockcheck.py, the .NET SDK's ClockCheck.cs and the Profile Manager's clockcheck.ts.
import { LicenseError } from "./license.js";

/** The engine refuses the token because this computer's clock is off. `skewSec` is negative when the clock runs behind. */
export class ClockSkewError extends LicenseError {
  skewSec: number;
  constructor(message: string, skewSec: number) {
    super(message, "CLOCK_SKEW");
    this.name = "ClockSkewError";
    this.skewSec = skewSec;
  }
}

interface Times { nbf?: number; iat?: number; exp?: number }

function payload(token: string): Times | null {
  try {
    const p = JSON.parse(Buffer.from(token.split(".")[0], "base64url").toString("utf8")) as Record<string, unknown>;
    const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
    return { nbf: num(p.nbf), iat: num(p.iat), exp: num(p.exp) };
  } catch {
    return null;
  }
}

/** "3 min 12 s", "2 h 5 min", "1 day 4 h". */
export function formatDuration(sec: number): string {
  const s = Math.max(1, Math.round(Math.abs(sec)));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  if (d) return `${d} day${d === 1 ? "" : "s"}${h ? ` ${h} h` : ""}`;
  if (h) return `${h} h${m ? ` ${m} min` : ""}`;
  if (m) return `${m} min${r ? ` ${r} s` : ""}`;
  return `${r} s`;
}

const utc = (sec: number) => new Date(sec * 1000).toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");

const FIX = "Set the clock to update automatically (Windows: Settings > Time & language > Date & time > "
  + "\"Set time automatically\", then \"Sync now\"; macOS: System Settings > General > Date & Time; "
  + "Linux: `timedatectl set-ntp true`) and launch again.";

/**
 * The message for a token the engine would refuse on this clock, or null when the clock is fine for it (or the
 * token has no times to check). `what` names the token in the message.
 */
export function clockSkewProblem(token: string, nowSec = Date.now() / 1000, what = "licence token"): { message: string; skewSec: number } | null {
  const t = payload(token);
  if (!t) return null;
  if (t.nbf !== undefined && nowSec < t.nbf) {
    const behind = (t.iat ?? t.nbf) - nowSec;
    return {
      skewSec: -behind,
      message: `This computer's clock is at least ${formatDuration(behind)} behind (it reads ${utc(nowSec)}; the server `
        + `issued this ${what} at ${utc(t.iat ?? t.nbf)}). The browser checks the ${what} against this computer's clock `
        + `and refuses one that is not valid yet, whatever the licence key. ${FIX}`,
    };
  }
  if (t.exp !== undefined && nowSec >= t.exp) {
    const ahead = t.iat !== undefined ? nowSec - t.iat : nowSec - t.exp;
    return {
      skewSec: ahead,
      message: `This computer's clock looks wrong: it reads ${utc(nowSec)}, after the moment this ${what} expires `
        + `(${utc(t.exp)})${t.iat !== undefined ? `, although the server issued it at ${utc(t.iat)}` : ""}. The browser `
        + `refuses an expired ${what}, whatever the licence key. ${FIX}`,
    };
  }
  return null;
}

/** Throw ClockSkewError when the engine would refuse `token` on this computer's clock. */
export function assertClockFor(token: string | null | undefined, what = "licence token", nowSec = Date.now() / 1000): void {
  if (!token) return;
  const p = clockSkewProblem(token, nowSec, what);
  if (p) throw new ClockSkewError(p.message, p.skewSec);
}
