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
  tag: "v0.1.0-pre.21",
  version: "149.0.7827.114",
  asset: "clearcote-149.0.7827.114-windows-x64.zip",
  url: "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.21/clearcote-149.0.7827.114-windows-x64.zip",
  sha256: "79b03d2d875b374970b2d54eae54f77070eba06b6a446dc163420854ec068c4d",
  exeSha256: "09a9f5ed46be45b54babc91872256fcdd5ef61cef6bf65cbec3928cbb38ee17a",
  size: 242655762,
  os: "win32",
  archive: "zip",
  binary: "chrome.exe",
  assetGlob: "windows-x64",
};

const LINUX: ReleaseInfo = {
  tag: "v0.1.0-pre.21",
  version: "149.0.7827.114",
  asset: "clearcote-149.0.7827.114-linux-x64.tar.xz",
  url: "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.21/clearcote-149.0.7827.114-linux-x64.tar.xz",
  sha256: "5e7241a3e90033bc84f6079821829e99a6e6f0f6479eaa291d8b6590363aa292",
  exeSha256: "dd5aef845b47f63ebf84d769cc349dae69178639fe5c703fc52779c5a0606cce",
  size: 146851212,
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
  platforms: Partial<Record<"windows" | "linux", CatalogPlatform>>;
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
      tag: "v0.1.0-pre.21",
      platforms: {
        windows: { asset: WINDOWS.asset, url: WINDOWS.url, sha256: WINDOWS.sha256, exeSha256: WINDOWS.exeSha256, size: WINDOWS.size, archive: "zip", binary: "chrome.exe" },
        linux: { asset: LINUX.asset, url: LINUX.url, sha256: LINUX.sha256, exeSha256: LINUX.exeSha256, size: LINUX.size, archive: "tar.xz", binary: "chrome" },
      },
    },
  ],
};
