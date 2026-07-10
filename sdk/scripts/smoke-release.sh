#!/usr/bin/env bash
# Release smoke test — install the PUBLISHED clearcote SDK from the real registries and LAUNCH it,
# so we never ship a release whose browser can't actually start. Verifies BOTH SDKs (PyPI + npm) and
# BOTH tiers (FREE always; PRO when CLEARCOTE_LICENSE_KEY is set), each by launching the browser
# headless and reading a real navigator.userAgent. Any failure exits non-zero.
#
# Usage:
#   CLEARCOTE_LICENSE_KEY=cc_lic_... sdk/scripts/smoke-release.sh <version>            # this host's OS
#   CLEARCOTE_LICENSE_KEY=cc_lic_... sdk/scripts/smoke-release.sh <version> --docker   # clean-room Linux container
#
# Run the host mode on EACH target OS (Windows + Linux). --docker adds a bare-image Linux check that
# also proves the required system libraries are documented (a bare `FROM debian` deploy is how most
# Docker users break). Requires: host mode -> python3 + node/npm on PATH; docker mode -> Docker.
# See docs/RELEASE-SMOKE-TEST.md.
set -uo pipefail

VERSION="${1:?usage: smoke-release.sh <version> [--docker]}"
MODE="${2:-host}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Chromium/Chrome headless runtime libraries for Debian bookworm (a bare image ships none of these).
DEB_DEPS="ca-certificates fonts-liberation libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
libasound2 libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libatspi2.0-0 libxshmfence1 libx11-6 \
libxcb1 libxext6 libxi6"

run_host() {
  local rc=0 tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN

  echo "== Python: pip install clearcote==$VERSION =="
  python3 -m venv "$tmp/venv"
  "$tmp/venv/bin/pip" install -q "clearcote==$VERSION"
  "$tmp/venv/bin/python" "$HERE/smoke_launch.py" || rc=1

  echo "== Node: npm i clearcote@$VERSION =="
  ( cd "$tmp" && npm init -y >/dev/null 2>&1 && npm i -s "clearcote@$VERSION" >/dev/null 2>&1 )
  cp "$HERE/smoke_launch.mjs" "$tmp/smoke_launch.mjs"
  ( cd "$tmp" && node smoke_launch.mjs ) || rc=1

  return $rc
}

run_docker() {
  # Clean-room: bare debian, install system libs + both runtimes + the published SDK, then launch.
  docker run --rm \
    -e "CLEARCOTE_LICENSE_KEY=${CLEARCOTE_LICENSE_KEY:-}" \
    -e "VERSION=$VERSION" -e "DEB_DEPS=$DEB_DEPS" \
    -v "$HERE:/smoke:ro" \
    debian:bookworm-slim bash -c '
      set -e
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq >/dev/null
      apt-get install -y -qq python3 python3-venv python3-pip nodejs npm $DEB_DEPS >/dev/null 2>&1
      echo "container: $(python3 --version) | node $(node -v)"
      python3 -m venv /venv && /venv/bin/pip install -q clearcote==$VERSION
      /venv/bin/python /smoke/smoke_launch.py
      cd /root && npm init -y >/dev/null 2>&1 && npm i -s clearcote@$VERSION >/dev/null 2>&1
      cp /smoke/smoke_launch.mjs /root/ && node /root/smoke_launch.mjs
    '
}

echo "### clearcote release smoke test — v$VERSION (mode: $MODE) ###"
[ -n "${CLEARCOTE_LICENSE_KEY:-}" ] || echo "(no CLEARCOTE_LICENSE_KEY — PRO tier will be skipped)"

case "$MODE" in
  host)   run_host ;;
  --docker|docker) run_docker ;;
  *) echo "unknown mode: $MODE (use 'host' or '--docker')"; exit 2 ;;
esac
RC=$?

echo
if [ $RC -eq 0 ]; then echo "### SMOKE PASS — v$VERSION launches ($MODE) ###";
else echo "### SMOKE FAIL — v$VERSION did NOT launch cleanly ($MODE) — DO NOT SHIP ###"; fi
exit $RC
