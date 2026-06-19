# Releasing the SDKs (npm + PyPI) — automated via GitHub Actions

This is the runbook for publishing the `clearcote` **SDK** packages (npm `clearcote` from
`sdk/node`, PyPI `clearcote` from `sdk/python`). It is **separate** from the browser-binary
release in [RELEASING.md](RELEASING.md): the browser ships on its own `v0.1.0-pre.N` tag line; the
SDKs ship on a dedicated **`sdk-v*`** tag line. The two never trigger each other.

Publishing uses **OIDC "trusted publishing"** — no `NPM_TOKEN`, no PyPI API token, nothing stored
in GitHub secrets. A short-lived OIDC token is minted per run and exchanged for a one-time publish
credential. npm **provenance** and PyPI **PEP 740 attestations** are generated automatically.

> Verified against official docs (2026): npm trusted publishing went GA 2025-07-31
> ([changelog](https://github.blog/changelog/2025-07-31-npm-trusted-publishing-with-oidc-is-generally-available/));
> PyPI uses [`pypa/gh-action-pypi-publish@release/v1`](https://github.com/pypa/gh-action-pypi-publish).
> `clearcote` is already published on both registries, so OIDC can be configured immediately (no
> cold-start). The repo is **public**, which is required for npm provenance.

## Workflows
- [`.github/workflows/npm.yml`](../.github/workflows/npm.yml) — build (`tsc`) + `npm publish` from `sdk/node`.
- [`.github/workflows/pypi.yml`](../.github/workflows/pypi.yml) — `python -m build` + upload from `sdk/python` (two-job split: ungated build → gated publish).

Both trigger on `push:` of a `sdk-v*` tag, with a `workflow_dispatch` manual fallback. Each runs
behind a GitHub **Environment** (`npm` / `pypi`) with a required-reviewer approval gate.

## One-time setup (do this once, before the first automated release)

### 1. GitHub — environments
Settings → Environments → create **`npm`** and **`pypi`**. For each:
- Add **Required reviewers** (your release approver).
- If you are the **sole maintainer**, leave *Prevent self-review* **OFF** (otherwise you can never
  approve your own release and it blocks forever). With a second maintainer, turn it **ON** for
  two-person control.
- Optional: add a *Deployment branches and tags* rule restricting the environment to `sdk-v*` tags.
- No secrets are added for the OIDC path.

### 2. npmjs.com — trusted publisher
`clearcote` package → **Settings → Trusted Publisher → GitHub Actions**. Enter (all
**case-sensitive**):
| Field | Value |
|---|---|
| Organization or user | `clearcotelabs` |
| Repository | `clearcote-browser` |
| Workflow filename | `npm.yml` *(filename only, with `.yml`, no path)* |
| Environment name | `npm` |

Requires npm CLI ≥ 11.5.1 / Node ≥ 22.14.0 on the runner — the workflow pins Node 24 and runs
`npm install -g npm@latest`, so this is handled.

### 3. pypi.org — trusted publisher
Project `clearcote` → **Publishing → Add a new publisher (GitHub Actions)**:
| Field | Value |
|---|---|
| Owner | `clearcotelabs` |
| Repository | `clearcote-browser` |
| Workflow name | `pypi.yml` *(basename only)* |
| Environment name | `pypi` |

*(If the project ever doesn't exist yet, add a **pending publisher** under Account → Publishing with
the same fields — it converts on first upload and does not reserve the name, so publish promptly.)*

## Per-release steps (every SDK release)
1. Bump the **same** version in **all three** hardcoded spots:
   - `sdk/node/package.json` → `"version"`
   - `sdk/python/pyproject.toml` → `[project] version`
   - `sdk/python/clearcote/__init__.py` → `__version__`
2. Commit and push to `main`.
3. Tag and push:
   ```bash
   git tag sdk-v0.6.1
   git push origin sdk-v0.6.1
   ```
4. The push starts **both** workflows. Each pauses at its environment gate — **approve** `npm` and
   `pypi` in the repo's Actions/Environments UI. On approval each publishes (OIDC, with
   provenance/attestations). A version that doesn't match the tag fails the run loudly (a guard
   step), turning a forgotten bump into a clean red ❌ instead of a mispublish.

> A single `sdk-v*` tag publishes **both** packages (they share one version today). If you ever need
> to release them at different versions, split into per-package tag namespaces.

## Auth fallback (only if OIDC is unavailable)
If you ever must publish before OIDC is configured: `npm publish --access public` from a logged-in
machine (or a temporary `NODE_AUTH_TOKEN` automation token), and `twine upload` with a PyPI API
token. Then switch back to the token-free OIDC path. Do **not** leave tokens in GitHub secrets.

## Notes / gotchas
- Owner/repo/workflow-filename/environment are **case-sensitive exact matches** between the registry
  config and the workflow — the repo is `clearcote-browser` (not `clearcote`). A mismatch fails OIDC
  silently with an "invalid publisher" error.
- npm provenance requires the repo to stay **public** and `package.json` `repository` to match it
  (it does: `git+https://github.com/clearcotelabs/clearcote-browser.git`). Making the repo private
  silently disables provenance.
- `release/v1` of the PyPI action is a moving major pointer; pin it to a full commit SHA for maximum
  supply-chain hardening (at the cost of manual updates).
