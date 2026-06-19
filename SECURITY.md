# Security Policy

Clearcote is a privacy / anti-detect browser — integrity *is* the product. This
policy covers how to **report a vulnerability** and how to **verify a release**.

## Reporting a vulnerability

Please report security issues **privately** — do **not** open a public issue for
an undisclosed vulnerability.

- **Preferred:** [GitHub private vulnerability reporting](https://github.com/clearcotelabs/clearcote-browser/security/advisories/new)
  — the repo **Security** tab → **Report a vulnerability**.
- **Alternative:** contact the maintainers via <https://clearcotelabs.com>,
  ideally PGP-encrypted to the project signing key (fingerprint below).

Please include the affected **release tag** and **Chromium version**, your
platform, a clear description, and a reproduction or proof-of-concept. We aim to
**acknowledge within 72 hours** and to keep you updated through triage and fix,
and we practice **coordinated disclosure** — we will agree a timeline with you
and credit you in the advisory unless you would rather stay anonymous.

### In scope

- Clearcote's source patches in [`patches/`](patches/), the build pipeline
  (`scripts/`, [`config/args.gn`](config/args.gn)), and the release / signing /
  supply-chain flow (checksums, GPG signatures, CI).
- The SDKs: [`sdk/node`](sdk/node) and [`sdk/python`](sdk/python).

### Out of scope

- Bugs in **upstream Chromium / ungoogled-chromium** that also affect stock
  builds — report those upstream. Clearcote tracks and pulls upstream security
  fixes via the pinned [`UPSTREAM_REVISION`](UPSTREAM_REVISION).
- "Site X can still detect / fingerprint it." Clearcote **reduces** but does not
  guarantee evasion of any detection system (see [DISCLAIMER.md](DISCLAIMER.md)).
  A concrete, reproducible fingerprint **leak vs. real Chrome** is, however, a
  valid bug — please file it.

## Supported versions

Clearcote is pre-1.0; only the **latest release** receives fixes. Please upgrade
before reporting (the SDKs and the `update_clearcote` flow always pull the latest
pinned build).

| Version | Supported |
|---|---|
| Latest release | :white_check_mark: |
| Older pre-releases | :x: |

## Verifying a release — don't trust, verify

Every release is checksummed and **GPG-signed** against a single, out-of-band
**pinned** signing key whose fingerprint never changes between releases:

```
CA96 F185 F96A 693A EDB3  AC1F CB00 D851 B7A8 6B0F
```

```bash
# 1. confirm the key IS the pinned one — compare this fingerprint to the value above
gpg --with-fingerprint --show-keys clearcote-signing-key.asc
gpg --import clearcote-signing-key.asc

# 2. verify the signed checksum file            (expect: Good signature)
gpg --verify SHA256SUMS.txt.asc SHA256SUMS.txt

# 3. verify your download against the signed checksums
sha256sum -c clearcote-<version>-<platform>.zip.sha256
```

The "key is not certified with a trusted signature" warning at step 2 is
expected — the proof is the **fingerprint match** plus the **Good signature**,
not a trust marker. If the fingerprint differs or a checksum fails, treat the
release as tampered and **do not run it**.

Full verification — reading the patch set and rebuilding from source — is
documented in [docs/VERIFY.md](docs/VERIFY.md).
