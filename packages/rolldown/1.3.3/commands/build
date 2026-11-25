#!/bin/bash
# Builds the native binding for riscv64 and stages it as its own package. Runs in
# the cross image (amd64 + riscv64 cross toolchain): the workspace is large, and
# qemu turns a three minute build into hours.
#
# The binding goes into out/binding/, not into the wrapper: upstream ships the binary
# as @rolldown/binding-<arch>-<abi>, and the wrapper's loader finds it there by
# package name. Staging it inside the wrapper instead would put a riscv64 binary in
# a package that every architecture installs.
set -euo pipefail

TARGET="${RISCV_TARGET:-riscv64gc-unknown-linux-gnu}"
# napi-rs names the binary after the crate and the platform package after the target.
BINDING_NAME="rolldown-binding.linux-riscv64-gnu.node"
BINDING_DIR="out/binding"
WRAPPER="out/rolldown"

test -d "$WRAPPER/dist" || {
  echo "ERROR: the wrapper is missing ($WRAPPER/dist); the fetch step places it there" >&2
  exit 1
}

echo "[build] cargo build --release -p rolldown_binding --target $TARGET"
# Only the binding crate is built. The workspace's own JS tooling (@oxc-node/core,
# rolldown's bundler) is not needed and could not run here anyway.
cargo build --release -p rolldown_binding --target "$TARGET"

# napi-rs names the cdylib after the crate; the platform package wants the platform name.
mkdir -p "$BINDING_DIR"
install -m 0644 "target/$TARGET/release/librolldown_binding.so" "$BINDING_DIR/$BINDING_NAME"

if ! file -b "$BINDING_DIR/$BINDING_NAME" | grep -q "RISC-V"; then
  echo "ERROR: $BINDING_DIR/$BINDING_NAME is not a RISC-V object:" >&2
  file "$BINDING_DIR/$BINDING_NAME" >&2
  exit 1
fi

# The directory becomes a package with exactly one binary in it; a stray file there
# would be published alongside it.
count=$(find "$BINDING_DIR" -mindepth 1 -maxdepth 1 | wc -l)
if [ "$count" -ne 1 ]; then
  echo "ERROR: $BINDING_DIR holds $count entries; only the binding belongs there" >&2
  find "$BINDING_DIR" -mindepth 1 -maxdepth 1 >&2
  exit 1
fi

echo "[build] binding: $BINDING_DIR/$BINDING_NAME ($(du -h "$BINDING_DIR/$BINDING_NAME" | cut -f1))"
