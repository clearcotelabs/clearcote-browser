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
| Tier | FREE (no key) · PRO (licence key given) |
| Environment | Windows host · Linux host · Linux clean-room Docker (as an unprivileged user, plus one root leg with `--cap-add=SYS_NICE`) |

FREE proves the GitHub-pinned build downloads, verifies, and launches with **no** license backend
contact. PRO proves the authenticated `/api/v1/download/pro` route + lease/run-token + gated launch
all work end to end. Each tier gets its own throwaway home directory, shared by Python and Node, so
each OS downloads the FREE and the PRO binary once per run.

## How to run

The harness lives in [`sdk/scripts/`](../sdk/scripts/): `smoke-release.sh` (orchestrator) +
`smoke_launch.py` / `smoke_launch.mjs` (the actual launchers, which exit non-zero on any failure;
each takes `free`, `pro`, or nothing for both).

```bash
# On EACH target OS (Windows via Git-Bash, Linux):
CCKEY=cc_lic_...  sdk/scripts/smoke-release.sh 0.32.0

# Clean-room Linux container (bare debian + the documented system libs — how Docker users deploy):
CCKEY=cc_lic_...  sdk/scripts/smoke-release.sh 0.32.0 --docker
```

- Pass the version you just published. Omit the key to smoke only the FREE tier (PRO is then
  reported `SKIPPED`). `CLEARCOTE_LICENSE_KEY=` works in place of `CCKEY=`.
- **The FREE runs are really keyless.** The SDK picks a key up on its own from
  `CLEARCOTE_LICENSE_KEY` and from `~/.clearcote/license.key`, so a maintainer's own key used to turn
  the "free" launch into a licensed one without anyone noticing. The script now starts every FREE run
  with neither, and the launchers add a guard: a FREE run whose Chrome major is not the SDK's pinned
  open build fails (`… a licence key reached the free run?`). Only the PRO runs receive the key.
- **Requirements:** host mode needs `python3` + `node`/`npm` on PATH; `--docker` needs Docker.
- **Pass criterion:** every run prints `LAUNCH_OK` and the script exits `0` with
  `### SMOKE PASS ###`. Any `LAUNCH_FAIL` → non-zero + `### SMOKE FAIL … DO NOT SHIP ###`.
  Each line shows the Chrome major that ran, e.g. `FREE: LAUNCH_OK | Chrome/151` and
  `PRO : LAUNCH_OK | Chrome/153` — FREE is the open build the SDK pins, PRO the current licensed build.

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

## Docker: root containers

The `--docker` mode runs every SDK leg as the image's unprivileged `node` user — the way the
published `teamflatearth/clearcote` image runs the browser (as its own `cc` user).

Up to the Chromium 150 open build (`v0.1.0-pre.23`), running as **root** in a container stopped the
open build at start: it was compiled with debug assertions (DCHECKs), and Chromium's `setpriority()`
call is refused when the container lacks `CAP_SYS_NICE` —
`FATAL:base/process/process_linux.cc … DCHECK failed: result == 0. : Permission denied`, surfaced by
Playwright as `Target page, context or browser has been closed`. Since Chromium 151 the open build is
an official build with DCHECKs compiled out, like the licensed build, and is not affected either. The
`--docker` mode still runs one extra FREE leg as root with `--cap-add=SYS_NICE`, which covers users
still pinned to 150. (Until this harness ran its legs unprivileged, its own `--docker`
mode ran as root and failed the FREE tier for exactly this reason — for 0.31.1 as much as for 0.32.0.)

## When it catches things

- FREE fails but PRO passes (or vice-versa) → a tier-specific packaging/route bug.
- FREE reports the licensed Chrome major → a key reached the free run; the harness's isolation broke.
- Host passes but `--docker` fails → a missing system lib (update the list above + user docs).
- The unprivileged legs pass but the root leg fails → `--cap-add=SYS_NICE` no longer covers root
  containers; update `docker/README.md` and the Linux notes in the SDK READMEs.
- Python passes but Node fails (or vice-versa) → a one-SDK regression (e.g. the pro path landed in
  only one language, or a version was published from a stale tree).
- Import fails (`exit 2`) → the package didn't install / the entry points are broken.

> **Runtime requirements the bare image must meet (learned on the 0.28.0 release):** Node **20 or
> newer** — `playwright-core ^1.49` resolves to a version that refuses Node 18, which is what Debian
> bookworm packages, so the container uses the `node:20-bookworm-slim` image; and **`xz-utils`** —
> the Node SDK extracts the `.tar.xz` engine archive with the system `tar`, which needs `xz`
> (the Python SDK has `lzma` built in and does not). Both are in the script's dependency list now.
