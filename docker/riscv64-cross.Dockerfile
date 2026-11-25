# syntax=docker/dockerfile:1
#
# amd64 build environment that cross-compiles Rust code for riscv64.
#
# Used by recipes that declare `toolchain: cross` in their .stat. The build steps
# run in this image (fast, native compiler speed) and the test step runs in the
# riscv64 image, so the produced artifact is still executed on the target CPU.
#
# Only the *build* moves off the target: nothing here is a riscv64 binary, the
# image just carries the compiler and the cross linker.
#
# The base image is the same Debian release as the riscv64 image, so glibc and
# the C library ABI line up with the runtime the test step uses.
# amd64 is also the default platform, but it is written out for the same reason as
# the riscv64 image: a mis-typed build should fail loudly instead of producing an
# image for the wrong architecture under a name that promises otherwise.
FROM --platform=linux/amd64 debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

# build-essential is the *host* compiler: proc-macro crates and build scripts are
# compiled for the build machine, and cargo needs a native linker for them. cmake
# is needed by crates that build C sources through it - mimalloc (rolldown's
# allocator) requires it, and the failure is a bare "is `cmake` not installed?".
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        file \
        git \
        g++-riscv64-linux-gnu \
        gcc-riscv64-linux-gnu \
        jq \
        nodejs \
        npm \
        pkg-config \
        python3 \
 && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH

# The default toolchain is pinned to what the first Rust recipe needs; a source
# tree that ships its own rust-toolchain.toml overrides it, and rustup then
# installs that version on first use. Both HOME directories are made writable for
# any uid because containers run as the calling user (see the action template),
# and rustup has to be able to install a toolchain into them at run time.
RUN set -eux; \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --profile minimal --no-modify-path --default-toolchain 1.100.0; \
    rustup target add riscv64gc-unknown-linux-gnu; \
    chmod -R a+rwX "$RUSTUP_HOME" "$CARGO_HOME"; \
    rustc -V; cargo -V; rustup target list --installed

# Linker and target compiler, spelled the way cargo expects them. Keeping these
# in the image means a recipe build snippet does not have to restate the cross
# wiring; it only has to pass --target.
ENV CARGO_TARGET_RISCV64GC_UNKNOWN_LINUX_GNU_LINKER=riscv64-linux-gnu-gcc \
    CC_riscv64gc_unknown_linux_gnu=riscv64-linux-gnu-gcc \
    CXX_riscv64gc_unknown_linux_gnu=riscv64-linux-gnu-g++ \
    AR_riscv64gc_unknown_linux_gnu=riscv64-linux-gnu-ar

# Cargo caches live in the workspace HOME the action creates, so they survive from
# one step to the next and never land in the image layer.
ENV CARGO_HOME=/w/.container-home/cargo

WORKDIR /w
