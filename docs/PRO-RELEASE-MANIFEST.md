# Signed Pro release manifest

Every Pro build the SDKs download is covered by a **release manifest**: one signed document per platform archive
that binds the product, version, revision, platform, the archive's name, SHA-256 and size, and the SHA-256 of the
binaries inside it. The SDKs verify it with a release key pinned in their source before they accept a download, and
again every time they launch a build from a kept answer.

## Why

Before this, the expected hashes of a Pro download came in the same server answer as the download link. Whoever could
change that answer (the site, its configuration, a cache file under `~/.clearcote`) could change the archive and its
hashes together. The release key is kept off every server, so the signed manifest is the one thing those places
cannot produce.

The launch-time checks stay as they were: the launcher (and `chrome.dll` on Windows) is hashed on every managed
launch. Their expected values now have to match the manifest.

## Format

The download answer (`GET /api/v1/download/pro`) carries a `manifest` string:

```
manifest = base64url(payload) "." base64url(signature)
```

- `payload` is UTF-8 JSON (below). base64url is RFC 4648 section 5 without padding.
- `signature` is the 64-byte Ed25519 signature (RFC 8032) over the ASCII bytes of the first part, that is over the
  base64url text, not over the decoded JSON. This is the same shape as the licence tokens.

```json
{
  "v": 1,
  "product": "clearcote-pro",
  "version": "158.0.8127.36",
  "revision": "r68",
  "platform": "windows",
  "archive": { "name": "clearcote-pro-158.0.8127.36-windows-x64-r68.zip", "sha256": "<64 hex>", "size": 209760398 },
  "binaries": { "chrome.exe": "<64 hex>", "chrome.dll": "<64 hex>" },
  "key": "<key id>",
  "issued": "2026-11-26"
}
```

- `platform` is `windows`, `linux` or `macos`.
- `binaries` maps a file name inside the archive to its SHA-256. The launcher is listed under the answer's `binary`
  name (`chrome.exe`, `chrome`, `Chromium`). A Windows manifest also lists `chrome.dll`.
- `key` names the pinned key that signed it. Hashes are lower-case hex.
- Unknown extra fields are ignored. `v` other than 1 is refused.

## What an SDK checks, in this order

`verify(meta, platform, want_revision, want_version)`; the first failure wins and the download or launch is refused.

| # | Check | Reason code |
|---|---|---|
| 1 | `meta.manifest` is present and not empty | `missing` |
| 2 | It is a string with exactly two base64url parts; the payload is a JSON object | `malformed` |
| 3 | `payload.key` is a pinned key id | `unknown-key` |
| 4 | The signature is 64 bytes and verifies under that key | `signature` |
| 5 | `payload.v` is 1 | `malformed` |
| 6 | `payload.product` is `clearcote-pro` | `product` |
| 7 | `payload.platform` is the platform asked for | `platform` |
| 8 | `payload.version` equals `meta.version`, and `want_version` when the caller pinned one | `version` |
| 9 | `payload.revision` equals `meta.revision`, and `want_revision` when the caller pinned one | `revision` |
| 10 | `payload.archive.sha256` equals `meta.sha256` (case-insensitive) and `payload.archive.size` equals `meta.size` | `archive` |
| 11 | `payload.binaries[meta.binary]` equals `meta.exe_sha256`; on Windows `payload.binaries["chrome.dll"]` equals `meta.chrome_dll_sha256`; all of them present | `binary` |

Step 2 runs before step 4 only as far as it must to read `key`; nothing else from the payload is used before the
signature has verified.

The check runs on:

- a fresh answer, before anything is downloaded;
- a kept answer (`~/.clearcote/pro-meta-*.json`), before it is used to launch an installed build, including the
  stale-answer fallback when the site cannot be reached. The file is writable by the user, so it is never trusted
  without the signature.

After it passes, the archive hash check after download, the launcher hash check after extraction and the launch-time
hash checks run exactly as before, against values that now equal the manifest's.

Free builds are not covered: their hashes are pinned in the SDK source or come from the signed `SHA256SUMS.txt`.

## Keys

Pinned keys are raw 32-byte Ed25519 public keys, written as hex, in a map from key id to key (`PRO_RELEASE_KEYS` in
`release.py`, `release.ts`, `Release.cs`). More than one key can be pinned, so a new key can ship in the SDKs before
it signs anything. There is no environment variable or option that adds a key or turns the check off.

## Test vectors

`sdk/testdata/pro_manifest_vectors.json` (made by `sdk/scripts/gen_pro_manifest_vectors.mjs`) holds a test key, three
RFC 8032 signatures for the Ed25519 verifier itself, and cases with the expected reason code. All three SDKs run it.
