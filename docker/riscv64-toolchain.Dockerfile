# syntax=docker/dockerfile:1
#
# riscv64 build environment for npm native addons.
#
# Used by:
#   * local verification (L1/L2) through `docker run --platform linux/riscv64`
#   * CI, once this image is published to a registry and referenced by the
#     RISCV_IMAGE variable of .github/templates/action.template.yml
#
# The base image is the official Debian port for riscv64; everything else is
# installed through the Debian archive so the toolchain matches what a
# riscv64 distribution would ship.
# The platform is spelled out on purpose: this image is riscv64-only, and a plain
# `docker build` on an amd64 host would otherwise produce an amd64 image under a
# riscv64 name. BuildKit warns about a constant FROM --platform (it is an
# anti-pattern for multi-arch images); that warning is expected here, the inability
# to build the wrong platform silently is the point.
FROM --platform=linux/riscv64 debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        file \
        git \
        jq \
        nodejs \
        npm \
        python3 \
 && rm -rf /var/lib/apt/lists/*

# node-gyp is bundled with upstream npm but Debian splits it out; keep an
# explicit copy so build snippets can call `node-gyp` directly.
RUN apt-get update \
 && apt-get install -y --no-install-recommends node-gyp \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /w
