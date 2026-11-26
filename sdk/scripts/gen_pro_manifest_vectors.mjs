// Regenerate sdk/testdata/pro_manifest_vectors.json: the shared test vectors for the signed Pro release manifest
// (docs/PRO-RELEASE-MANIFEST.md). Every SDK runs the same cases, so the three verifiers cannot drift apart.
//   node sdk/scripts/gen_pro_manifest_vectors.mjs
// The key pair here is a TEST key, derived from a fixed seed. It signs nothing real and is pinned nowhere.
import fs from "node:fs"; import path from "node:path"; import { fileURLToPath } from "node:url";
import { createHash, createPrivateKey, createPublicKey, sign } from "node:crypto";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const seed = createHash("sha256").update("clearcote pro manifest TEST key 1").digest();
const priv = createPrivateKey({ key: Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), seed]), format: "der", type: "pkcs8" });
const pubDer = createPublicKey(priv).export({ type: "spki", format: "der" }); const pub = pubDer.subarray(pubDer.length - 32);
const seed2 = createHash("sha256").update("clearcote pro manifest TEST key 2 (not pinned)").digest();
const priv2 = createPrivateKey({ key: Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), seed2]), format: "der", type: "pkcs8" });
const KEY = "test-key-1";
const b64u = (b) => Buffer.from(b).toString("base64url");
const h = (s) => createHash("sha256").update(s).digest("hex");
const token = (payload, key = priv) => { const body = b64u(JSON.stringify(payload)); return body + "." + b64u(sign(null, Buffer.from(body), key)); };

const win = { version: "158.0.8127.36", revision: "r68", tag: "pro-158.0.8127.36-r68", platform: "windows", asset: "clearcote-pro-158.0.8127.36-windows-x64-r68.zip",
  archive: "zip", binary: "chrome.exe", url: "https://example.invalid/file?t=x", sha256: h("win archive"), exe_sha256: h("win exe"), chrome_dll_sha256: h("win dll"), size: 209760398 };
const lin = { version: "158.0.8127.36", revision: "r68", tag: "pro-158.0.8127.36-r68", platform: "linux", asset: "clearcote-pro-158.0.8127.36-linux-x64-r68.tar.xz",
  archive: "tar.xz", binary: "chrome", url: "https://example.invalid/file?t=y", sha256: h("linux archive"), exe_sha256: h("linux exe"), size: 172176964 };
const payloadFor = (m, over = {}) => ({ v: 1, product: "clearcote-pro", version: m.version, revision: m.revision, platform: m.platform,
  archive: { name: m.asset, sha256: m.sha256, size: m.size },
  binaries: m.platform === "windows" ? { "chrome.exe": m.exe_sha256, "chrome.dll": m.chrome_dll_sha256 } : { [m.binary]: m.exe_sha256 }, key: KEY, issued: "2026-11-26", ...over });
const good = token(payloadFor(win)); const [gb, gs] = good.split(".");
const flip = (s) => (s[0] === "A" ? "B" : "A") + s.slice(1);
const C = (name, meta, platform, expect, extra = {}) => ({ name, platform, want_revision: null, want_version: null, ...extra, meta, expect });
const cases = [
  C("valid windows", { ...win, manifest: good }, "windows", "ok"),
  C("valid linux", { ...lin, manifest: token(payloadFor(lin)) }, "linux", "ok"),
  C("valid, pinned to the same revision", { ...win, manifest: good }, "windows", "ok", { want_revision: "r68", want_version: "158.0.8127.36" }),
  C("valid, hashes in upper case in the release info", { ...win, sha256: win.sha256.toUpperCase(), manifest: good }, "windows", "ok"),
  C("unsigned: no manifest", { ...win }, "windows", "missing"),
  C("unsigned: manifest is an empty string", { ...win, manifest: "" }, "windows", "missing"),
  C("manifest is not a string", { ...win, manifest: { v: 1 } }, "windows", "malformed"),
  C("one part only", { ...win, manifest: gb }, "windows", "malformed"),
  C("three parts", { ...win, manifest: good + ".x" }, "windows", "malformed"),
  C("payload is not JSON", { ...win, manifest: (() => { const b = b64u("not json"); return b + "." + b64u(sign(null, Buffer.from(b), priv)); })() }, "windows", "malformed"),
  C("signature of the wrong length", { ...win, manifest: gb + "." + b64u(Buffer.alloc(40, 7)) }, "windows", "signature"),
  C("signature altered", { ...win, manifest: gb + "." + flip(gs) }, "windows", "signature"),
  C("payload altered after signing (revision r68 -> r69)", { ...win, revision: "r69", manifest: b64u(JSON.stringify(payloadFor({ ...win, revision: "r69" }))) + "." + gs }, "windows", "signature"),
  C("signed by a key that is not pinned, under the pinned key id", { ...win, manifest: token(payloadFor(win), priv2) }, "windows", "signature"),
  C("unknown key id", { ...win, manifest: token(payloadFor(win, { key: "someone-else" })) }, "windows", "unknown-key"),
  C("wrong product", { ...win, manifest: token(payloadFor(win, { product: "clearcote-free" })) }, "windows", "product"),
  C("unknown manifest version", { ...win, manifest: token(payloadFor(win, { v: 2 })) }, "windows", "malformed"),
  C("manifest of another platform", { ...lin, platform: "windows", manifest: token(payloadFor(lin)) }, "windows", "platform"),
  C("wrong revision: a valid r67 manifest served for r68", { ...win, manifest: token(payloadFor({ ...win, revision: "r67" })) }, "windows", "revision"),
  C("wrong revision: release info and manifest agree on r67, the caller pinned r68", { ...win, revision: "r67", manifest: token(payloadFor({ ...win, revision: "r67" })) }, "windows", "revision", { want_revision: "r68" }),
  C("wrong version", { ...win, manifest: token(payloadFor({ ...win, version: "157.0.8083.80" })) }, "windows", "version"),
  C("wrong version: the caller pinned another one", { ...win, manifest: good }, "windows", "version", { want_version: "157.0.8083.80" }),
  C("archive hash in the release info differs from the manifest", { ...win, sha256: h("another archive"), manifest: good }, "windows", "archive"),
  C("archive size differs from the manifest", { ...win, size: win.size + 1, manifest: good }, "windows", "archive"),
  C("launcher hash differs from the manifest", { ...win, exe_sha256: h("another exe"), manifest: good }, "windows", "binary"),
  C("chrome.dll hash differs from the manifest", { ...win, chrome_dll_sha256: h("another dll"), manifest: good }, "windows", "binary"),
  C("windows manifest without chrome.dll", { ...win, manifest: token(payloadFor(win, { binaries: { "chrome.exe": win.exe_sha256 } })) }, "windows", "binary"),
  C("manifest without the launcher", { ...lin, manifest: token(payloadFor(lin, { binaries: {} })) }, "linux", "binary"),
  C("release info without the launcher hash", { ...lin, exe_sha256: undefined, manifest: token(payloadFor(lin)) }, "linux", "binary"),
];
const out = { note: "TEST vectors for the signed Pro release manifest. The key is a test key: it is pinned nowhere and signs nothing real. Regenerate with sdk/scripts/gen_pro_manifest_vectors.mjs.",
  key_id: KEY, public_key_hex: pub.toString("hex"), private_seed_hex: seed.toString("hex"),
  rfc8032: [ // RFC 8032 section 7.1, TEST 1..3: checks the Ed25519 verifier itself
    { public_key_hex: "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", message_hex: "", signature_hex: "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b" },
    { public_key_hex: "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", message_hex: "72", signature_hex: "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00" },
    { public_key_hex: "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", message_hex: "af82", signature_hex: "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a" },
  ],
  cases };
const file = path.join(HERE, "..", "testdata", "pro_manifest_vectors.json");
fs.writeFileSync(file, JSON.stringify(out, null, 1) + "\n");
console.log(`${cases.length} cases -> ${file}; test public key ${pub.toString("hex")}`);
