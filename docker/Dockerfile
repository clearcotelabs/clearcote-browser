# clearcote — open-source stealth Chromium, as a CDP-endpoint container.
#
#   docker build -t clearcote .
#   docker run -d -p 9222:9222 clearcote        # CDP on http://localhost:9222
#   # then:  playwright.chromium.connect_over_cdp("http://localhost:9222")
#
# Published image: teamflatearth/clearcote  (docker run -d -p 9222:9222 teamflatearth/clearcote)
# Requires the clearcote SDK >= 0.13.1 (which pins the v0.1.0-pre.19 binary).
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="clearcote" \
      org.opencontainers.image.description="Open-source stealth Chromium — CDP endpoint on :9222, drop-in for Playwright/Puppeteer/browser-use" \
      org.opencontainers.image.source="https://github.com/clearcotelabs/clearcote-browser" \
      org.opencontainers.image.url="https://github.com/clearcotelabs/clearcote-browser" \
      org.opencontainers.image.licenses="BSD-3-Clause"

# Chromium runtime libraries + a base font set (fontconfig + Liberation + Noto emoji/CJK so
# canvas/text hashes stay coherent on a bare container — the #1 Linux tell). The clearcote
# release ALSO bundles the Windows metric-clone fonts, wired in by the SDK on launch. socat
# publishes Chrome's loopback-only DevTools port.
RUN apt-get update && apt-get install -y --no-install-recommends \
      xz-utils ca-certificates socat \
      libnss3 libnspr4 libgbm1 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 libxext6 libxrender1 libxcb1 \
      libpango-1.0-0 libcairo2 libx11-6 libexpat1 libdbus-1-3 libxi6 libxtst6 \
      fontconfig fonts-liberation fonts-noto-color-emoji fonts-unifont fonts-wqy-zenhei \
 && rm -rf /var/lib/apt/lists/*

# Install the SDK + bake the signed clearcote Linux binary into the image (SHA-256 verified).
# CLEARCOTE_SPEC pins the SDK version: default ">=0.13.1" for standalone builds; CI passes
# "==<version>" so the image is built from the exact SDK release that fired the workflow.
ENV XDG_CACHE_HOME=/opt/xdg-cache
ARG CLEARCOTE_SPEC=">=0.13.1"
RUN mkdir -p /opt/xdg-cache \
 && pip install --no-cache-dir "clearcote${CLEARCOTE_SPEC}" \
 && python -c "from clearcote import download; print('binary:', download())" \
 && useradd -m -u 10001 cc && chown -R cc /opt/xdg-cache

COPY serve.py /serve.py

USER cc
EXPOSE 9222
# Persona via env: CC_PLATFORM (windows|linux|macos|android), CC_FINGERPRINT, CC_BRAND (Edge|Chrome…),
# CC_ACCEPT_LANGUAGE, CC_TIMEZONE, CC_HARDWARE_CONCURRENCY, CC_GPU_VENDOR/RENDERER, CC_TLS_PROFILE.
ENTRYPOINT ["python", "/serve.py"]
