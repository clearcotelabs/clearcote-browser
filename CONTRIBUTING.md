# Contributing to Clearcote

Thanks for your interest! Clearcote is built in the open and contributions are welcome — from typo fixes to new engine-level controls.

## Ground rules

- **Patches, not forks.** Changes to Chromium are unified diffs under [`patches/`](patches), listed in `patches/series`, one logical change per patch, each with a header comment explaining the *why*.
- **Pin to the target revision.** Patches target the revision in [`UPSTREAM_REVISION`](UPSTREAM_REVISION). If you bump it, update and re-test the whole series.
- **Engine-level, not script injection.** Behavior that affects what the browser exposes belongs in compiled code.
- **No secrets, no blobs.** Never commit credentials, tokens, or opaque binaries. Source and text patches only.
- **Don't drop privacy patches.** If a privacy/identity patch fails to apply, fix it — never skip it to make a build pass.
- **Keep it verifiable.** Every change should preserve the "rebuild it yourself and check" property.

## Workflow

1. Open an issue describing the change (especially for anything that alters exposed signals).
2. Make your change against a prepared source tree; generate a clean patch into `patches/` and add it to `patches/series`.
3. Confirm the full series applies with no rejects on a clean tree, and that it builds.
4. Open a PR with: what changed, why, and how you verified it.

## Reviewing & verification

PRs that change runtime behavior should explain the privacy/identity reasoning and include a way to verify the effect. See [docs/VERIFY.md](docs/VERIFY.md).

## Cutting a release (maintainers)

Releases follow one standing procedure — build, bundle the runtime, checksum, GPG-sign against the pinned key, verify on the build box and again after transfer, publish as a signed pre-release, and announce. Do not improvise: follow **[docs/RELEASING.md](docs/RELEASING.md)** top to bottom, every time.

## Code of conduct

Be respectful and constructive. Assume good faith. We're here to build something trustworthy together.
