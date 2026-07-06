# Clearcote — reproducible build environment.
#
# A pinned Ubuntu image with the base tools the build scripts need. It fetches the exact pinned
# Chromium source, applies the same patch series, installs Chromium's own build deps + toolchain,
# and compiles the browser — for Windows x64 (cross) or Linux x64 (native) — producing the same
# distributable archive as the published release, which you verify against SHA256SUMS.txt.
#
#   # build the image once (the build ENVIRONMENT, not the browser)
#   docker build -t clearcote-build .
#
#   # then build a target (multi-hour; needs ~16 GB+ RAM, ~120 GB disk). Artifacts land in ./out:
#   docker run --rm -v "$PWD/out:/clearcote-build/dist" clearcote-build linux
#   docker run --rm -v "$PWD/out:/clearcote-build/dist" clearcote-build windows
#
#   # verify (see docs/VERIFY.md):
#   sha256sum -c out/clearcote-149.0.7827.114-linux-x64.tar.xz.sha256
#
# For byte-for-byte reproducibility pin the base image to a digest (see docs/BUILDING.md).
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
# Base tools the numbered build scripts need. Chromium's own (large) build dependencies are
# installed at RUN time by scripts/02-host-toolchain.sh (install-build-deps.py), because they
# need the source tree that scripts/00 fetches first. ciopfs = case-insensitive overlay for the
# Windows SDK headers; xz-utils = unpack the Linux .tar.xz.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 python3-pip curl ca-certificates \
      ninja-build zip unzip xz-utils ciopfs patch \
      sudo lsb-release file pkg-config \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /clearcote
COPY . /clearcote

# The build scripts write everything (source tree, toolchains, dist) under $WORK.
ENV WORK=/clearcote-build

ENTRYPOINT ["/clearcote/build.sh"]
CMD ["windows"]
