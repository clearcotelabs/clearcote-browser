# clearcote — open-source stealth Chromium, as a CDP-endpoint container.
#
#   docker build -t clearcote .
#   docker run -d -p 9222:9222 --security-opt seccomp=seccomp.json clearcote   # CDP on http://localhost:9222
#   # then:  playwright.chromium.connect_over_cdp("http://localhost:9222")
#
# seccomp.json (in this directory, and in the image at /etc/clearcote/seccomp.json) is Docker's default
# seccomp profile of moby/profiles seccomp v0.2.3 plus the two namespace calls Chrome's sandbox needs; it
# replaces the daemon's own default. Without it the container still runs, with Chrome's sandbox off
# (--no-sandbox) and a log line saying so; see README.md.
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

# Chromium runtime libraries + fontconfig. socat publishes Chrome's loopback-only DevTools port. xvfb
# provides a virtual X display so the browser runs HEADFUL by default (headed Chrome avoids headless-mode
# tells; see serve.py). libegl1 (with Mesa's EGL) lets a Linux persona render WebGL through Mesa under
# CC_HEADLESS, with no display. tini runs as PID 1 (see ENTRYPOINT) so processes orphaned inside the
# container are reaped.
#
# Fonts. The clearcote release bundles the Windows metric-clone fonts, and the SDK points fontconfig at
# them with a SELF-CONTAINED config, so nothing under /usr/share/fonts reaches the browser: the emoji and
# CJK packages this image used to install were invisible, and every CJK, Indic or Thai character drew
# as an empty box (a Windows machine draws all of them). The script fonts now go into their own
# directory, which CLEARCOTE_FALLBACK_FONT_DIRS (below) lists AFTER the bundle: they are used only for
# characters the bundle cannot draw. Only script fonts go there; a Latin family would compete with the
# clones and change the Windows widths. Hard links, so nothing is stored twice; fonts-noto-core is then
# removed (~50 MB, mostly faces we do not use) and the linked files stay.
RUN apt-get update && apt-get install -y --no-install-recommends \
      xz-utils ca-certificates socat xvfb libegl1 tini \
      libnss3 libnspr4 libgbm1 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 libxext6 libxrender1 libxcb1 \
      libpango-1.0-0 libcairo2 libx11-6 libexpat1 libdbus-1-3 libxi6 libxtst6 \
      fontconfig fonts-liberation fonts-noto-color-emoji fonts-unifont fonts-wqy-zenhei \
      fonts-ipafont-gothic fonts-noto-core \
 && F=/usr/local/share/clearcote/fonts && mkdir -p $F && cd /usr/share/fonts \
 && cp -l truetype/noto/NotoColorEmoji.ttf truetype/wqy/wqy-zenhei.ttc \
          opentype/ipafont-gothic/ipag.ttf opentype/ipafont-gothic/ipagp.ttf $F/ \
 && for s in Arabic Armenian Bengali Cherokee Devanagari Ethiopic Georgian Gujarati Gurmukhi Javanese \
             Kannada Khmer Lao Malayalam Myanmar Oriya Sinhala Symbols Tamil Telugu Thaana Thai; do \
      cp -l truetype/noto/NotoSans$s-Regular.ttf truetype/noto/NotoSans$s-Bold.ttf $F/ || exit 1; done \
 && for s in Gothic Math Mongolian NKo NewTaiLue Ogham OldItalic Osmanya PhagsPa Runic Symbols2 TaiLe \
             Tifinagh Vai Yi; do \
      cp -l truetype/noto/NotoSans$s-Regular.ttf $F/ || exit 1; done \
 && cp -l truetype/noto/NotoSerifTibetan-Regular.ttf truetype/noto/NotoSerifTibetan-Bold.ttf $F/ \
 && apt-get purge -y fonts-noto-core \
 && test "$(ls $F | wc -l)" = 65 \
 && rm -rf /var/lib/apt/lists/*
ENV CLEARCOTE_FALLBACK_FONT_DIRS=/usr/local/share/clearcote/fonts

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

COPY serve.py idle_exit.py proxy_relay.py sandbox_check.py /
COPY seccomp.json /etc/clearcote/seccomp.json
# What the entrypoint does with the settings the SDKs pass (see SERVE_PROTOCOL in serve.py): 2 = takes
# CC_SECRETS_FILE and CC_IDLE_EXIT_SECONDS, logs in to a proxy that needs a password, and reports what it
# applied in a "serve-state" line; 3 = also runs Chrome's sandbox when the container allows it, so the SDKs
# pass the seccomp profile above. The SDKs read this label before they start a container; an image without
# it gets plain variables.
LABEL com.clearcotelabs.serve-protocol="3"

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
