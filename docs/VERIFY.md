# Verify Clearcote — don't trust, verify

The whole point of Clearcote is that you don't have to take anyone's word for what's inside it. This page explains how to confirm, for yourself, that a Clearcote release is exactly what it claims to be — and nothing more.

## The trust model in one line

**Open source in → reproducible build → checksummed, attestable artifact out.** No step requires faith.

| Layer | How you verify it |
|---|---|
| Upstream Chromium | Pinned to an exact revision in `UPSTREAM_REVISION`; the source tarball is hash-checked on download. |
| De-Googling | Comes from upstream [ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium) — itself open and widely audited. |
| Clearcote's changes | Every change is a plain-text patch in [`patches/`](../patches). Read them; diff them; question them. |
| The binary | Published with SHA-256 checksums and (planned) build provenance/attestation. |

## 1. Verify the GPG signature — do this first

A checksum only proves a download is internally consistent; it proves nothing if an attacker who controls the release also rewrites the checksum. The real anchor is a GPG signature you check against the **project's pinned signing key**, whose fingerprint is published here (and in the [README](../README.md)) and does **not** change between releases:

```
CA96 F185 F96A 693A EDB3  AC1F CB00 D851 B7A8 6B0F
```

```bash
# 1. confirm the key you're about to trust IS the pinned one — compare this fingerprint to the value above
gpg --with-fingerprint --show-keys clearcote-signing-key.asc
gpg --import clearcote-signing-key.asc

# 2. verify the signed checksum file is authentic
gpg --verify SHA256SUMS.txt.asc SHA256SUMS.txt        # expect: Good signature
```

A fresh import prints `WARNING: This key is not certified with a trusted signature` — that's **expected** (you haven't personally certified the key). What proves integrity is that **the fingerprint matches the pinned value above** *and* the signature is **Good**. If the fingerprint differs, treat the release as tampered and **do not run it.**

## 2. Check the published checksums

With the signature verified, confirm your download matches the signed checksum. The Windows asset is
a `.zip`; the Linux asset is a `.tar.xz`:

```bash
# Windows
sha256sum -c clearcote-<version>-windows-x64.zip.sha256
# Linux
sha256sum -c clearcote-<version>-linux-x64.tar.xz.sha256
```

You can also confirm the inner binary against the signed aggregate list:

```bash
# Windows — compare to the chrome.exe line in SHA256SUMS.txt
unzip -p clearcote-<version>-windows-x64.zip chrome.exe | sha256sum
# Linux — compare to the chrome line in SHA256SUMS.txt
tar -xJOf clearcote-<version>-linux-x64.tar.xz chrome | sha256sum
```

If a hash doesn't match `SHA256SUMS.txt` (whose authenticity you verified in step 1), **do not run it.**

## 3. Read the patches

The identity/privacy behavior of Clearcote is defined entirely by the patch set in [`patches/`](../patches) plus the upstream ungoogled patches. These are unified diffs against a pinned Chromium revision. There is no compiled-in behavior that isn't visible there. If something is unclear, open an issue — clarity is a feature.

## 4. Rebuild it yourself

The deepest verification is reproduction. Follow [BUILDING.md](BUILDING.md) with the same pinned revision, the patch set in [`patches/`](../patches), and the published [`config/args.gn`](../config/args.gn), then compare your build to the published one.

Note honestly: Chromium cross-builds are **not yet bit-for-bit deterministic** (embedded build paths, timestamps, linker/PGO nondeterminism), so a byte-identical hash match is the *goal*, not a guarantee today. What you can fully audit right now is that every change is a readable patch and the build config is public — nothing compiled-in is hidden. A reproducible/attested build is tracked in the [Roadmap](../ROADMAP.md), Phase 4. If your build diverges in a way the patches and config don't explain, that's a bug we want to hear about.

## 5. (Planned) Verify build provenance

Future releases will publish build provenance/attestation so you can confirm an artifact was produced by the public CI from this exact source — not hand-assembled. The verification command will live here when it lands. See the [Roadmap](../ROADMAP.md), Phase 4.

## What "no sketchy things" means here

- **No opaque binaries in the source tree.** The repo is source and readable patches.
- **No silent network calls.** The de-Googled base removes Google's telemetry/update beacons; any network behavior is auditable in the patches and upstream.
- **No "just trust the .exe."** If we can't show you how to reproduce it, we don't ship it.

Found something that doesn't add up? Please [open an issue](https://github.com/clearcotelabs/clearcote-browser/issues). Verifiability only works if people actually check — and we want you to.
