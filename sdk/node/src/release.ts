// Pinned Clearcote releases the SDK downloads and verifies — one pin per platform.
// Bumping to a new browser build = updating the platform entry here (and the version in
// package.json). The sha256 is the SINGLE trust anchor for the auto-download: if you trust this
// package, the hash check guarantees you run exactly the published, signed binary.
// Published checksums + GPG signatures live on each release page.

export interface ReleaseInfo {
  /** Git tag / release the binary comes from. */
  tag: string;
  /** Chromium version the build is based on. */
  version: string;
  /** Release asset (archive) file name. */
  asset: string;
  /** Direct download URL for the asset. */
  url: string;
  /** SHA-256 of the archive — verified after download; mismatch is a hard failure. */
  sha256: string;
  /** SHA-256 of the inner browser binary — verified after extraction (defense in depth). */
  exeSha256: string;
  /** SHA-256 of chrome.dll on Windows — verified before every managed launch when present. */
  chromeDllSha256?: string;
  /** Expected archive size in bytes (for progress display only). */
  size: number;
  /** Platform the binary runs on. */
  os: NodeJS.Platform;
  /** Archive format of the asset — how to unpack it. */
  archive: "zip" | "tar.xz";
  /** Inner browser-binary file name (chrome.exe on Windows, chrome on Linux). */
  binary: string;
  /** The `-<glob>.<ext>` marker that identifies this platform's asset (e.g. "windows-x64"). */
  assetGlob: string;
}

// Per-platform pin. Each entry is a complete, self-contained pinned release for that OS: the exact
// signed asset + its SHA-256 (the trust anchor) + the inner-binary hash (defense in depth) + how to
// unpack it. Windows and Linux ship from their own release tags.
const WINDOWS: ReleaseInfo = {
  tag: "v0.1.0-pre.25",
  version: "151.0.7922.108",
  asset: "clearcote-151.0.7922.108-windows-x64.zip",
  url: "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.25/clearcote-151.0.7922.108-windows-x64.zip",
  sha256: "a3e3455c4c2da6187c9224563d99651a0cfd1a2bac5f70c2f9a19366c4c541cd",
  exeSha256: "e9c88e826534200e62b6544144ff3801c63065edb8940ff45e533918322e8dc0",
  size: 204317302,
  os: "win32",
  archive: "zip",
  binary: "chrome.exe",
  assetGlob: "windows-x64",
};

const LINUX: ReleaseInfo = {
  tag: "v0.1.0-pre.25",
  version: "151.0.7922.108",
  asset: "clearcote-151.0.7922.108-linux-x64.tar.xz",
  url: "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.25/clearcote-151.0.7922.108-linux-x64.tar.xz",
  sha256: "6c06f95783410d09799828b93f7bb26ac8174354b87f580827c8bcf1f2226b7e",
  exeSha256: "6261bae468ced0b7b1fe0f71d73344cb6feca2d9abf4a1da285ba4f572c546cc",
  size: 122845968,
  os: "linux",
  archive: "tar.xz",
  binary: "chrome",
  assetGlob: "linux-x64",
};

/** process.platform -> pinned release. Add an entry to support another OS. */
export const PLATFORMS: Record<string, ReleaseInfo> = { win32: WINDOWS, linux: LINUX };

/** The pinned release for the given platform (default: this OS), or undefined if unsupported. */
export function platformRelease(plat: NodeJS.Platform | string = process.platform): ReleaseInfo | undefined {
  return PLATFORMS[plat];
}

/**
 * process.platform -> the PRO catalog / /download/pro `platform` key. Wider than PLATFORMS: macOS has
 * a native PRO build (Apple Silicon, a Chromium.app bundle) but no free pinned release, so it is here
 * and not there. The PRO build is resolved by the site at run time, so there is nothing to pin for it.
 */
export const PRO_PLATFORMS: Record<string, "windows" | "linux" | "macos"> = { win32: "windows", linux: "linux", darwin: "macos" };

/** CPU architectures the native macOS PRO build runs on. arm64 only: an Intel Mac (or a Node running
 * under Rosetta, which reports x64) uses the Docker image instead. */
export const MACOS_ARCHES: readonly string[] = ["arm64"];

/** Indirection so tests can fake the CPU (process.arch is read-only). */
export const hostArch = { get: (): string => process.arch };

/**
 * The PRO download platform key for an OS ("windows" / "linux" / "macos"), or null if there is no
 * PRO build for it. A macOS host that is not Apple Silicon throws: the build exists, just not for this
 * CPU, and "unsupported OS" would send the caller looking in the wrong place.
 */
export function proPlatformKey(plat: string = process.platform, arch: string = hostArch.get()): "windows" | "linux" | "macos" | null {
  const key = PRO_PLATFORMS[plat] ?? null;
  if (key === "macos" && !MACOS_ARCHES.includes(arch)) {
    throw new Error(
      `The native macOS Clearcote PRO build is for Apple Silicon (arm64) only; this Mac reports ${arch || "an unknown CPU"}. ` +
        "Use the Docker image instead (launch({ docker: true })), or run on an Apple Silicon Mac.",
    );
  }
  return key;
}

/** True on an Apple Silicon Mac, where the native PRO build runs. */
export function nativeMacosSupported(plat: string = process.platform, arch: string = hostArch.get()): boolean {
  return plat === "darwin" && MACOS_ARCHES.includes(arch);
}

// The pin for the CURRENT platform. Falls back to the Windows entry on an unsupported OS so the
// existing error messaging still has a version to quote. Most code uses this; the download/guard
// paths branch on platformRelease() to reject unsupported OSes.
export const RELEASE: ReleaseInfo = platformRelease() ?? WINDOWS;

/** GitHub repo (owner/name) the releases come from — used by the opt-in auto-update resolver. */
export const REPO = "clearcotelabs/clearcote-browser";

/** Clearcote's release-signing key fingerprint, pinned out-of-band. This NEVER changes between
 * releases, so it is the durable trust anchor for `autoUpdate`: when a `gpg` binary is available,
 * an auto-resolved (un-pinned) release's `SHA256SUMS.txt.asc` is verified against THIS fingerprint
 * before the binary is trusted. (Pinned mode trusts the baked-in sha256 instead.) */
export const SIGNING_KEY_FPR = "CA96F185F96A693AEDB3AC1FCB00D851B7A86B0F";

// ── Version catalog ──────────────────────────────────────────────────────────
// Source of truth for "which browser majors exist and what tier each is". Fetched at runtime so a NEW
// release becomes switchable (launch({version:"150"})) without an SDK bump. Each build declares a
// `tier`: FREE builds are public on GitHub and carry url+sha256; PRO builds (license-gated, not yet
// public) advertise existence ONLY — the actual download routes through the authenticated
// /api/v1/download/pro. When a PRO major is promoted to public, flip its tier to "free" + add the URL.
// platform keys are "windows"/"linux" (matching the /download/pro `platform` param).

export type Tier = "free" | "pro";

export interface CatalogPlatform {
  asset?: string;
  url?: string;
  sha256?: string;
  exeSha256?: string;
  size?: number;
  archive: "zip" | "tar.xz";
  binary: string;
}
export interface CatalogBuild {
  major: number;
  version: string;
  tier: Tier;
  tag: string;
  platforms: Partial<Record<"windows" | "linux" | "macos", CatalogPlatform>>;
}
export interface Catalog {
  schema: number;
  builds: CatalogBuild[];
}

export const CATALOG_URL = "https://www.clearcotelabs.com/api/v1/versions";

/** Offline fallback snapshot — keep in sync with published releases. Lets the SDK still VALIDATE a
 * request (and download the free pins) when the live catalog is unreachable. Only list builds that are
 * actually DOWNLOADABLE: when a new build (e.g. the 150 PRO) goes live, add it to the live catalog
 * (/api/v1/versions) — no SDK republish needed — and to this snapshot on the next SDK release. */
export const CATALOG_FALLBACK: Catalog = {
  schema: 1,
  builds: [
    {
      major: 149,
      version: "149.0.7827.114",
      tier: "free",
      tag: "v0.1.0-pre.22",
      platforms: {
        windows: { asset: "clearcote-149.0.7827.114-windows-x64.zip", url: "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.22/clearcote-149.0.7827.114-windows-x64.zip", sha256: "f40c83049aae13d8c0322e9822eeb2d7236fddd5e7915304eb6f0f5f1f79fcb6", exeSha256: "09a9f5ed46be45b54babc91872256fcdd5ef61cef6bf65cbec3928cbb38ee17a", size: 242661515, archive: "zip", binary: "chrome.exe" },
        linux: { asset: "clearcote-149.0.7827.114-linux-x64.tar.xz", url: "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.22/clearcote-149.0.7827.114-linux-x64.tar.xz", sha256: "6625971f51318acb6adc301fcc18dcf790c3de0ba2b313be7182eff180a8ec53", exeSha256: "aea54f3c1b5bfc43b4c42aff00d41df49393575950df7c644cc580f72462db4a", size: 146834232, archive: "tar.xz", binary: "chrome" },
      },
    },
    {
      major: 150,
      version: "150.0.7871.114",
      tier: "free",
      tag: "v0.1.0-pre.23",
      platforms: {
        windows: { asset: "clearcote-150.0.7871.114-windows-x64.zip", url: "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.23/clearcote-150.0.7871.114-windows-x64.zip", sha256: "93fc03c45b931d8d82f714814318892929f44dd671b0993788332071d53f3135", exeSha256: "f49b0d6bc5a08857e34f951ddc456abc643283ae45ff330ee7c2c39cd75b4869", size: 245936073, archive: "zip", binary: "chrome.exe" },
        linux: { asset: "clearcote-150.0.7871.114-linux-x64.tar.xz", url: "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.23/clearcote-150.0.7871.114-linux-x64.tar.xz", sha256: "92ebd3de6fbf2c1349d3eb3c210952fcdb8dc9ed35655a35297cd68431fc8ef2", exeSha256: "cc192460484b7dcb1de8df3b3b7bea6f78962d560b6347012d9dbedd3f66720d", size: 149463344, archive: "tar.xz", binary: "chrome" },
      },
    },
    {
      major: 151,
      version: "151.0.7922.108",
      tier: "free",
      tag: "v0.1.0-pre.25",
      platforms: {
        windows: { asset: WINDOWS.asset, url: WINDOWS.url, sha256: WINDOWS.sha256, exeSha256: WINDOWS.exeSha256, size: WINDOWS.size, archive: "zip", binary: "chrome.exe" },
        linux: { asset: LINUX.asset, url: LINUX.url, sha256: LINUX.sha256, exeSha256: LINUX.exeSha256, size: LINUX.size, archive: "tar.xz", binary: "chrome" },
      },
    },
  ],
};
