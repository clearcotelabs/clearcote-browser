// Pinned Clearcote release the SDK downloads and verifies.
// Bumping the SDK to a new browser build = updating these constants (and the version in
// package.json). The sha256 is the SINGLE trust anchor for the auto-download: if you trust
// this package, the hash check guarantees you run exactly the published, signed binary.
// Published checksums + GPG signatures live on the release page.

export interface ReleaseInfo {
  /** Git tag / release the binary comes from. */
  tag: string;
  /** Chromium version the build is based on. */
  version: string;
  /** Release asset (zip) file name. */
  asset: string;
  /** Direct download URL for the asset. */
  url: string;
  /** SHA-256 of the zip — verified after download; mismatch is a hard failure. */
  sha256: string;
  /** SHA-256 of chrome.exe inside the zip — verified after extraction (defense in depth). */
  exeSha256: string;
  /** Expected zip size in bytes (for progress display only). */
  size: number;
  /** Platform the binary runs on. */
  os: NodeJS.Platform;
}

export const RELEASE: ReleaseInfo = {
  tag: "v0.1.0-pre.15",
  version: "149.0.7827.114",
  asset: "clearcote-149.0.7827.114-windows-x64.zip",
  url: "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.15/clearcote-149.0.7827.114-windows-x64.zip",
  sha256: "8fc279533a928c8c8614788000afa7ef95895859df9ea5438dee5a32a1a6e58a",
  exeSha256: "5743595256c89c6874804bf3315acce592fc7f1883760c8d380c010151a73b23",
  size: 242646871,
  os: "win32",
};

/** GitHub repo (owner/name) the releases come from — used by the opt-in auto-update resolver. */
export const REPO = "clearcotelabs/clearcote-browser";

/** Clearcote's release-signing key fingerprint, pinned out-of-band. This NEVER changes between
 * releases, so it is the durable trust anchor for `autoUpdate`: when a `gpg` binary is available,
 * an auto-resolved (un-pinned) release's `SHA256SUMS.txt.asc` is verified against THIS fingerprint
 * before the binary is trusted. (Pinned mode trusts the baked-in sha256 instead.) */
export const SIGNING_KEY_FPR = "CA96F185F96A693AEDB3AC1FCB00D851B7A86B0F";
