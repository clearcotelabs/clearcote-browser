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
  tag: "v0.1.0-pre.4",
  version: "149.0.7827.114",
  asset: "clearcote-149.0.7827.114-windows-x64.zip",
  url: "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.4/clearcote-149.0.7827.114-windows-x64.zip",
  sha256: "40972168909e887434a3db4188d336bb7389d319a5c75967fb66ca6114c22e4c",
  exeSha256: "5743595256c89c6874804bf3315acce592fc7f1883760c8d380c010151a73b23",
  size: 253019015,
  os: "win32",
};
