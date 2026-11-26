// The signed Pro release manifest (docs/PRO-RELEASE-MANIFEST.md).
//
// The answer of GET /api/v1/download/pro names an archive and its hashes. Whoever can change that answer
// (the site, its configuration, the kept copy under ~/.clearcote) could change the archive and the hashes
// together, so the answer also carries `manifest`: base64url(payload) "." base64url(signature), where the
// signature is Ed25519 over the ASCII bytes of the first part, made with a release key that is kept off every
// server. The public halves are pinned in release.ts (PRO_RELEASE_KEYS). verifyProManifest() runs the checks
// of the document, in its order, and the first failure wins. The same checks, reason codes and test vectors
// (sdk/testdata/pro_manifest_vectors.json) as the Python and .NET SDKs: change all three together.
//
// There is no option, environment variable or file that adds a key or skips the check.

import { createPublicKey, verify } from "node:crypto";
import { PRO_RELEASE_KEYS } from "./release.js";

/** Why a manifest was refused. */
export type ProManifestReason =
  | "missing" | "malformed" | "unknown-key" | "signature" | "product"
  | "platform" | "version" | "revision" | "archive" | "binary";

/** A Pro download answer whose release manifest is missing, not signed by a pinned key, or for another build. */
export class ProManifestError extends Error {
  /** The reason code of docs/PRO-RELEASE-MANIFEST.md. */
  readonly reason: ProManifestReason;
  /** What was wrong, in plain words (the middle of the message). */
  readonly detail: string;
  constructor(reason: ProManifestReason, detail: string) {
    super(`Clearcote Pro release manifest check failed (${reason}): ${detail}. Refusing to use this build.`);
    this.name = "ProManifestError";
    this.reason = reason;
    this.detail = detail;
  }
}

export type ProPlatform = "windows" | "linux" | "macos";

/** The fields of a /download/pro answer the check reads. All of it is untrusted until the check has passed. */
export interface ProManifestMeta {
  manifest?: unknown;
  version?: unknown;
  revision?: unknown;
  binary?: unknown;
  sha256?: unknown;
  exe_sha256?: unknown;
  chrome_dll_sha256?: unknown;
  size?: unknown;
}

/** The signed payload, as returned once every check has passed. Unknown extra fields are kept. */
export interface ProManifestPayload {
  v: 1;
  product: "clearcote-pro";
  version: string;
  revision: string;
  platform: ProPlatform;
  archive: { name?: string; sha256: string; size: number; [extra: string]: unknown };
  binaries: Record<string, string>;
  key: string;
  issued?: string;
  [extra: string]: unknown;
}

/** The launcher's file name when the answer names none: the name the install and the launch check then use. */
export function defaultProLauncher(platform: ProPlatform): string {
  return platform === "windows" ? "chrome.exe" : platform === "macos" ? "Chromium" : "chrome";
}

// SubjectPublicKeyInfo header of an Ed25519 key (RFC 8410); the raw 32-byte key follows it.
const ED25519_SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");

/**
 * Ed25519 (RFC 8032) verification of `signature` over `message` under a raw 32-byte public key.
 * False for a key or signature of the wrong length and for anything the key parser refuses: never throws.
 */
export function ed25519Verify(publicKey: Uint8Array, message: Uint8Array, signature: Uint8Array): boolean {
  if (publicKey.length !== 32 || signature.length !== 64) return false;
  try {
    const key = createPublicKey({ key: Buffer.concat([ED25519_SPKI_PREFIX, publicKey]), format: "der", type: "spki" });
    return verify(null, message, key, signature);
  } catch {
    return false;
  }
}

/** Strict base64url without padding (RFC 4648 section 5); null for anything else. Buffer.from alone skips bad characters. */
function base64urlDecode(text: string): Buffer | null {
  if (!/^[A-Za-z0-9_-]*$/.test(text) || text.length % 4 === 1) return null;
  return Buffer.from(text, "base64url");
}

function hexBytes(hex: unknown, bytes: number): Buffer | null {
  return typeof hex === "string" && hex.length === bytes * 2 && /^[0-9a-fA-F]+$/.test(hex) ? Buffer.from(hex, "hex") : null;
}

const isText = (v: unknown): v is string => typeof v === "string" && v !== "";
const isObject = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const own = (o: object, k: string): boolean => Object.prototype.hasOwnProperty.call(o, k);
const sameHash = (a: unknown, b: unknown): boolean => isText(a) && isText(b) && a.toLowerCase() === b.toLowerCase();

/** A value for an error message: short, quoted, on one line. */
function show(v: unknown): string {
  if (v === undefined) return "nothing";
  let s: string;
  try {
    s = typeof v === "string" ? v : JSON.stringify(v) ?? String(v);
  } catch {
    s = String(v);
  }
  s = s.replace(/[^\x20-\x7e]/g, "?");
  return `'${s.length > 80 ? `${s.slice(0, 77)}...` : s}'`;
}

/**
 * Check the release manifest of a /download/pro answer and return its payload.
 *
 * `platform` is the platform that was asked for; `wantRevision` ("r68") and `wantVersion` ("158.0.8127.36") are
 * what the caller pinned, when it pinned one. Throws {@link ProManifestError} with the reason code of the first
 * check that fails. Nothing of the payload except `key` is read before the signature has verified.
 */
export function verifyProManifest(
  meta: ProManifestMeta, platform: ProPlatform, wantRevision?: string | null, wantVersion?: string | null,
): ProManifestPayload {
  const fail = (reason: ProManifestReason, detail: string): never => {
    throw new ProManifestError(reason, detail);
  };

  // 1. present
  const manifest = isObject(meta) ? meta.manifest : undefined;
  if (manifest === undefined || manifest === null || manifest === "") {
    return fail("missing", "the download answer carries no signed release manifest");
  }

  // 2. two base64url parts, the first a JSON object
  if (typeof manifest !== "string") return fail("malformed", "the manifest is not a string");
  const parts = manifest.split(".");
  if (parts.length !== 2) return fail("malformed", `the manifest has ${parts.length} parts, not 2 (payload.signature)`);
  const [signedText, signatureText] = parts;
  const payloadBytes = base64urlDecode(signedText);
  const signature = base64urlDecode(signatureText);
  if (!payloadBytes || !signature) return fail("malformed", "the manifest is not base64url");
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(payloadBytes));
  } catch {
    return fail("malformed", "the manifest payload is not JSON");
  }
  if (!isObject(parsed)) return fail("malformed", "the manifest payload is not a JSON object");
  const payload = parsed;

  // 3. signed by a key this SDK pins. Only `key` is read here; it picks the key and proves nothing.
  const keyId = payload.key;
  const pinned = typeof keyId === "string" && own(PRO_RELEASE_KEYS, keyId) ? PRO_RELEASE_KEYS[keyId] : undefined;
  if (typeof pinned !== "string") {
    return fail("unknown-key", `it is signed with key ${show(keyId)}, which is not a release key this SDK version knows`);
  }

  // 4. the signature, over the ASCII text of the first part
  const publicKey = hexBytes(pinned, 32);
  if (signature.length !== 64 || !publicKey || !ed25519Verify(publicKey, Buffer.from(signedText, "ascii"), signature)) {
    return fail("signature", `the signature does not verify under release key ${show(keyId)}`);
  }

  // From here the payload is what the release key signed.
  // 5. format version
  if (payload.v !== 1) return fail("malformed", `manifest format ${show(payload.v)} is not one this SDK version reads (1)`);
  // 6. product
  if (payload.product !== "clearcote-pro") return fail("product", `the manifest is for product ${show(payload.product)}, not 'clearcote-pro'`);
  // 7. platform
  if (payload.platform !== platform) return fail("platform", `the manifest is for platform ${show(payload.platform)}, not '${platform}'`);
  // 8. version
  if (!isText(payload.version) || payload.version !== meta.version) {
    return fail("version", `the manifest is for version ${show(payload.version)}, the download answer says ${show(meta.version)}`);
  }
  if (wantVersion !== undefined && wantVersion !== null && payload.version !== wantVersion) {
    return fail("version", `the manifest is for version ${show(payload.version)}, not the pinned ${show(wantVersion)}`);
  }
  // 9. revision
  if (!isText(payload.revision) || payload.revision !== meta.revision) {
    return fail("revision", `the manifest is for revision ${show(payload.revision)}, the download answer says ${show(meta.revision)}`);
  }
  if (wantRevision !== undefined && wantRevision !== null && payload.revision !== wantRevision) {
    return fail("revision", `the manifest is for revision ${show(payload.revision)}, not the pinned ${show(wantRevision)}`);
  }
  // 10. the archive
  const archive = payload.archive;
  if (!isObject(archive) || !sameHash(archive.sha256, meta.sha256)) {
    return fail("archive", "the archive SHA-256 in the download answer is not the one in the manifest");
  }
  if (typeof archive.size !== "number" || archive.size !== meta.size) {
    return fail("archive", `the archive size in the download answer (${show(meta.size)}) is not the one in the manifest (${show(archive.size)})`);
  }
  // 11. the binaries the install and every launch are checked against
  const binaries = payload.binaries;
  const launcher = isText(meta.binary) ? meta.binary : defaultProLauncher(platform);
  if (!isObject(binaries) || !own(binaries, launcher) || !sameHash(binaries[launcher], meta.exe_sha256)) {
    return fail("binary", `the SHA-256 of ${show(launcher)} in the download answer is not the one in the manifest`);
  }
  if (platform === "windows" && (!own(binaries, "chrome.dll") || !sameHash(binaries["chrome.dll"], meta.chrome_dll_sha256))) {
    return fail("binary", "the SHA-256 of 'chrome.dll' in the download answer is not the one in the manifest");
  }
  return payload as ProManifestPayload;
}
