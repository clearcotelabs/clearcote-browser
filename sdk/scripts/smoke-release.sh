#!/usr/bin/env bash
# Release smoke test — install the PUBLISHED clearcote SDK from the real registries and LAUNCH it,
# so we never ship a release whose browser can't actually start. Verifies BOTH SDKs (PyPI + npm) and
# BOTH tiers (FREE always; PRO when a licence key is given), each by launching the browser headless
# and reading a real navigator.userAgent. Any failure exits non-zero.
#
# Usage:
#   CCKEY=cc_lic_... sdk/scripts/smoke-release.sh <version>            # this host's OS
#   CCKEY=cc_lic_... sdk/scripts/smoke-release.sh <version> --docker   # clean-room Linux container
# (CLEARCOTE_LICENSE_KEY works too; either way the key only ever reaches the PRO runs.)
#
# Run the host mode on EACH target OS (Windows + Linux). --docker adds a bare-image Linux check that
# also proves the required system libraries are documented (a bare `FROM debian` deploy is how most
# Docker users break). Requires: host mode -> python3 + node/npm on PATH; docker mode -> Docker.
# See docs/RELEASE-SMOKE-TEST.md.
set -uo pipefail

VERSION="${1:?usage: smoke-release.sh <version> [--docker]}"
MODE="${2:-host}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY="${CCKEY:-${CLEARCOTE_LICENSE_KEY:-}}"

# Chromium/Chrome headless runtime libraries for Debian bookworm (a bare image ships none of these).
DEB_DEPS="ca-certificates fonts-liberation libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
libasound2 libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libatspi2.0-0 libxshmfence1 libx11-6 \
libxcb1 libxext6 libxi6 xz-utils"
# xz-utils: the Node SDK extracts .tar.xz with the system tar, which needs xz (Python has lzma built in).

# A path the launched program can use as its home directory (Windows programs read USERPROFILE).
winpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

run_host() {
  local rc=0 tmp vbin
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  mkdir -p "$tmp/free-home" "$tmp/pro-home"
  # Each tier runs in its own clean environment. The SDK picks a licence key up by itself from
  # CLEARCOTE_LICENSE_KEY and from ~/.clearcote/license.key, so a FREE run gets neither — otherwise
  # a maintainer's own key silently turns the "free" launch into a licensed one. PRO gets only the
  # key given to this script.
  local FREE=(env -u CLEARCOTE_LICENSE_KEY -u CCKEY HOME="$tmp/free-home" USERPROFILE="$(winpath "$tmp/free-home")")
  local PRO=(env -u CLEARCOTE_LICENSE_KEY HOME="$tmp/pro-home" USERPROFILE="$(winpath "$tmp/pro-home")" CCKEY="$KEY")

  echo "== Python: pip install clearcote==$VERSION =="
  python3 -m venv "$tmp/venv"
  # Windows venvs put the executables under Scripts/, POSIX under bin/
  vbin="$tmp/venv/bin"; [ -d "$vbin" ] || vbin="$tmp/venv/Scripts"
  "$vbin/pip" install -q "clearcote==$VERSION" || rc=1
  "${FREE[@]}" "$vbin/python" "$HERE/smoke_launch.py" free || rc=1
  if [ -n "$KEY" ]; then "${PRO[@]}" "$vbin/python" "$HERE/smoke_launch.py" pro || rc=1
  else echo "[PY] PRO : SKIPPED (no licence key given)"; fi

  echo "== Node: npm i clearcote@$VERSION =="
  ( cd "$tmp" && npm init -y >/dev/null 2>&1 && npm i -s "clearcote@$VERSION" >/dev/null 2>&1 ) || rc=1
  cp "$HERE/smoke_launch.mjs" "$tmp/smoke_launch.mjs"
  ( cd "$tmp" && "${FREE[@]}" node smoke_launch.mjs free ) || rc=1
  if [ -n "$KEY" ]; then ( cd "$tmp" && "${PRO[@]}" node smoke_launch.mjs pro ) || rc=1
  else echo "[NODE] PRO : SKIPPED (no licence key given)"; fi

  return $rc
}

run_docker() {
  # Clean-room: bare debian, install system libs + both runtimes + the published SDK, then launch.
  # node:20 image: Debian bookworm packages Node 18, and playwright-core now requires Node 20.
  #
  # Every SDK leg runs as the image's unprivileged `node` user — the way the published
  # teamflatearth/clearcote image runs the browser. One extra leg runs the FREE build as root with
  # --cap-add=SYS_NICE, the workaround the docs give for root containers: the open build is compiled
  # with DCHECKs, and as root without CAP_SYS_NICE Chromium's setpriority() is refused and the DCHECK
  # on it aborts the browser. The key goes in as CCKEY and is handed to the PRO legs only.
  # Git Bash on Windows hands Docker a /c/... path that mounts as an empty folder: pass the Windows path
  # and keep MSYS from rewriting the container-side ones.
  MSYS_NO_PATHCONV=1 docker run --rm --cap-add=SYS_NICE \
    -e "CCKEY=$KEY" -e "VERSION=$VERSION" -e "DEB_DEPS=$DEB_DEPS" \
    -v "$(winpath "$HERE"):/smoke:ro" \
    node:20-bookworm-slim bash -c '
      set -u
      rc=0
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq >/dev/null
      apt-get install -y -qq python3 python3-venv python3-pip $DEB_DEPS >/dev/null 2>&1 || { echo "apt-get FAILED"; exit 1; }
      echo "container: $(python3 --version) | node $(node -v) | SDK legs run as: node (uid $(id -u node))"
      mkdir -p /tmp/free-home /tmp/pro-home /tmp/root-free-home && chown node:node /tmp/free-home /tmp/pro-home
      as_node() { runuser -u node -- env HOME=/home/node "$@"; }
      free_node() { runuser -u node -- env -u CCKEY -u CLEARCOTE_LICENSE_KEY HOME=/tmp/free-home "$@"; }
      pro_node() { runuser -u node -- env -u CLEARCOTE_LICENSE_KEY HOME=/tmp/pro-home CCKEY="$CCKEY" "$@"; }

      as_node python3 -m venv /home/node/venv && as_node /home/node/venv/bin/pip install -q "clearcote==$VERSION" || rc=1
      free_node /home/node/venv/bin/python /smoke/smoke_launch.py free || rc=1
      if [ -n "$CCKEY" ]; then pro_node /home/node/venv/bin/python /smoke/smoke_launch.py pro || rc=1
      else echo "[PY] PRO : SKIPPED (no licence key given)"; fi

      mkdir -p /home/node/app && chown node:node /home/node/app && cp /smoke/smoke_launch.mjs /home/node/app/
      (cd /home/node/app && as_node npm init -y >/dev/null 2>&1 && as_node npm i -s "clearcote@$VERSION" >/dev/null 2>&1) || rc=1
      (cd /home/node/app && free_node node smoke_launch.mjs free) || rc=1
      if [ -n "$CCKEY" ]; then (cd /home/node/app && pro_node node smoke_launch.mjs pro) || rc=1
      else echo "[NODE] PRO : SKIPPED (no licence key given)"; fi

      echo "-- as root, with --cap-add=SYS_NICE (the documented workaround for root containers):"
      env -u CCKEY -u CLEARCOTE_LICENSE_KEY HOME=/tmp/root-free-home /home/node/venv/bin/python /smoke/smoke_launch.py free || rc=1
      exit $rc
    '
}

echo "### clearcote release smoke test — v$VERSION (mode: $MODE) ###"
[ -n "$KEY" ] || echo "(no licence key — PRO tier will be skipped; pass CCKEY or CLEARCOTE_LICENSE_KEY)"

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
