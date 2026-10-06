# clearcote — open-source stealth Chromium, as a CDP-endpoint container.
#
#   docker build -t clearcote .
#   docker run -d -p 9222:9222 clearcote        # CDP on http://localhost:9222
#   # then:  playwright.chromium.connect_over_cdp("http://localhost:9222")
#
# Published image: teamflatearth/clearcote  (docker run -d -p 9222:9222 teamflatearth/clearcote)
# Builds against the LATEST clearcote SDK (override with --build-arg CLEARCOTE_SPEC='==0.26.0').
# Free by default. Pass a licence key to run the PRO engine, and mount the cache so it downloads once:
#   docker run -d -p 9222:9222 -v clearcote-cache:/opt/xdg-cache \
#     -e CLEARCOTE_LICENSE_KEY=cc_lic_... teamflatearth/clearcote
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="clearcote" \
      org.opencontainers.image.description="Open-source stealth Chromium — CDP endpoint on :9222, drop-in for Playwright/Puppeteer/browser-use" \
      org.opencontainers.image.source="https://github.com/clearcotelabs/clearcote-browser" \
      org.opencontainers.image.url="https://github.com/clearcotelabs/clearcote-browser" \
      org.opencontainers.image.licenses="BSD-3-Clause"

# Chromium runtime libraries + a base font set (fontconfig + Liberation + Noto emoji/CJK so
# canvas/text hashes stay coherent on a bare container — the #1 Linux tell). The clearcote
# release ALSO bundles the Windows metric-clone fonts, wired in by the SDK on launch. socat
# publishes Chrome's loopback-only DevTools port. xvfb provides a virtual X display so the
# browser runs HEADFUL by default (headed Chrome avoids headless-mode tells; see serve.py). libegl1
# (with Mesa's EGL) lets a Linux persona render WebGL through Mesa under CC_HEADLESS, with no display.
# tini runs as PID 1 (see ENTRYPOINT) so processes orphaned inside the container are reaped.
RUN apt-get update && apt-get install -y --no-install-recommends \
      xz-utils ca-certificates socat xvfb libegl1 tini \
      libnss3 libnspr4 libgbm1 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 libxext6 libxrender1 libxcb1 \
      libpango-1.0-0 libcairo2 libx11-6 libexpat1 libdbus-1-3 libxi6 libxtst6 \
      fontconfig fonts-liberation fonts-noto-color-emoji fonts-unifont fonts-wqy-zenhei \
 && rm -rf /var/lib/apt/lists/*

# Install the SDK + bake the signed clearcote Linux binary into the image (SHA-256 verified).
# CLEARCOTE_SPEC pins the SDK version: empty (the default) installs the LATEST release, so a
# rebuilt image always carries current licensed-build selection; CI passes "==<version>" so the
# image is built from the exact SDK release that fired the workflow.
ENV XDG_CACHE_HOME=/opt/xdg-cache
ARG CLEARCOTE_SPEC=""
RUN mkdir -p /opt/xdg-cache \
 && pip install --no-cache-dir "clearcote${CLEARCOTE_SPEC}" \
 && python -c "from clearcote import download; print('binary:', download())" \
 && useradd -m -u 10001 cc && chown -R cc /opt/xdg-cache

# The engine cache. A licensed container fetches the PRO engine here on first start; declaring
# it a VOLUME keeps it out of the container layer and lets a named volume persist it, so the
# ~180 MB download happens once rather than per container.
VOLUME ["/opt/xdg-cache"]

COPY serve.py idle_exit.py proxy_relay.py /
# What the entrypoint does with the settings the SDKs pass (see SERVE_PROTOCOL in serve.py): 2 = takes
# CC_SECRETS_FILE and CC_IDLE_EXIT_SECONDS, logs in to a proxy that needs a password, and reports what it
# applied in a "serve-state" line. The SDKs read this label before they start a container; an image
# without it gets plain variables.
LABEL com.clearcotelabs.serve-protocol="2"

USER cc
EXPOSE 9222
# Persona via env: CC_PLATFORM (windows|linux|macos|android), CC_FINGERPRINT, CC_BRAND (Edge|Chrome…),
# CC_ACCEPT_LANGUAGE, CC_TIMEZONE, CC_HARDWARE_CONCURRENCY, CC_GPU_VENDOR/RENDERER, CC_TLS_PROFILE.
#
# tini is PID 1, with serve.py as its child. Chromium double-forks helpers (its crash handler among
# them), so they are re-parented to PID 1, and only PID 1 can reap them once they exit. A Python PID 1
# never does. Measured: 25 browsers launched and closed inside the container (docker exec, or any
# multi-browser server run in it) left 100 <defunct> chrome/chrome_crashpad entries under PID 1 for
# the life of the container; with tini, none. (CDP clients of serve.py's own browser leave none
# either way: its helpers only exit with the container.) tini also passes SIGTERM/SIGINT on to
# serve.py, which forwards them to chrome and releases the licence seat exactly as before. Arguments
# after the image name still reach serve.py unchanged.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "/serve.py"]
