# Release smoke test — never ship a build that can't launch

Unit tests don't prove the **browser actually starts**. A release can be green on CI and still be
broken for users: a bad binary pin, a mis-packaged archive, a missing PRO route, or (in Docker) a
missing system library. This smoke test installs the **published** SDK from the real registries and
**launches the browser** — FREE and PRO, Python and Node, on each target OS plus a clean-room
container — reading a real `navigator.userAgent` to prove the engine started and runs JS.

**Run it on every SDK release, after publish, before you announce the release.** If it fails, the
release is broken — yank/patch it; do not ship.

## What it covers

| Axis | Values |
| --- | --- |
| SDK | Python (PyPI) · Node (npm) |
| Tier | FREE (no key) · PRO (`CLEARCOTE_LICENSE_KEY` set) |
| Environment | Windows host · Linux host · Linux clean-room Docker |

FREE proves the GitHub-pinned build downloads, verifies, and launches with **no** license backend
contact. PRO proves the authenticated `/api/v1/download/pro` route + lease/run-token + gated launch
all work end to end. Python and Node share the same per-OS binary cache, so each OS downloads the
FREE and PRO binary once.

## How to run

The harness lives in [`sdk/scripts/`](../sdk/scripts/): `smoke-release.sh` (orchestrator) +
`smoke_launch.py` / `smoke_launch.mjs` (the actual launchers, which exit non-zero on any failure).

```bash
# On EACH target OS (Windows via Git-Bash/PowerShell, Linux):
CLEARCOTE_LICENSE_KEY=cc_lic_...  sdk/scripts/smoke-release.sh 0.15.2

# Clean-room Linux container (bare debian + the documented system libs — how Docker users deploy):
CLEARCOTE_LICENSE_KEY=cc_lic_...  sdk/scripts/smoke-release.sh 0.15.2 --docker
```

- Pass the version you just published. Omit `CLEARCOTE_LICENSE_KEY` to smoke only the FREE tier
  (PRO is then reported `SKIPPED`).
- **Requirements:** host mode needs `python3` + `node`/`npm` on PATH; `--docker` needs Docker.
- **Pass criterion:** every tier prints `LAUNCH_OK` and the script exits `0` with
  `### SMOKE PASS ###`. Any `LAUNCH_FAIL` → non-zero + `### SMOKE FAIL … DO NOT SHIP ###`.

The license key is a secret: pass it via the environment, never hard-code it into the script or a
committed file.

## Docker: the system libraries

A bare image (`FROM debian:bookworm-slim`, `python:*-slim`, `node:*-slim`) ships **none** of
Chromium's shared-library dependencies, so a launch there fails with `error while loading shared
libraries`. This is the single most common way a working release "breaks" in Docker. The container
must install (bookworm package names):

```
ca-certificates fonts-liberation libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2
libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0
libpangocairo-1.0-0 libcairo2 libatspi2.0-0 libxshmfence1 libx11-6 libxcb1 libxext6 libxi6
```

Run the browser with `--no-sandbox` in a container (the SDK examples above already do), or set up a
user namespace. The `--docker` mode installs exactly this list, so a green `--docker` run is also
your proof that these are the libs to document for users.

## When it catches things

- FREE fails but PRO passes (or vice-versa) → a tier-specific packaging/route bug.
- Host passes but `--docker` fails → a missing system lib (update the list above + user docs).
- Python passes but Node fails (or vice-versa) → a one-SDK regression (e.g. the pro path landed in
  only one language, or a version was published from a stale tree).
- Import fails (`exit 2`) → the package didn't install / the entry points are broken.
