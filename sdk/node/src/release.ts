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
  tag: "v0.1.0-pre.19",
  version: "149.0.7827.114",
  asset: "clearcote-149.0.7827.114-windows-x64.zip",
  url: "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.19/clearcote-149.0.7827.114-windows-x64.zip",
  sha256: "da47f325053a98130baf6f4907e13ba5135d37645fb5c150e59c8081e7df48b3",
  exeSha256: "09a9f5ed46be45b54babc91872256fcdd5ef61cef6bf65cbec3928cbb38ee17a",
  size: 242656951,
  os: "win32",
  archive: "zip",
  binary: "chrome.exe",
  assetGlob: "windows-x64",
};

const LINUX: ReleaseInfo = {
  tag: "v0.1.0-pre.19",
  version: "149.0.7827.114",
  asset: "clearcote-149.0.7827.114-linux-x64.tar.xz",
  url: "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.19/clearcote-149.0.7827.114-linux-x64.tar.xz",
  sha256: "1be5a9f83f8f8217d97caf52553b5fe8e24a3360dfc83c471ba91d2d95a97ac1",
  exeSha256: "7c5ea6ce563bd6c12642f12b1c85d308c09096814e9d7fcd59dd360fdfe6bb63",
  size: 146861776,
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
