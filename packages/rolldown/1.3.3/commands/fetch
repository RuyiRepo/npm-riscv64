#!/bin/bash
# Two sources are needed, which is why this recipe hand-writes fetch instead of
# using the generated one:
#
#   1. the Rust workspace at an immutable tag - this is what gets rebuilt;
#   2. the published npm wrapper - its JavaScript glue is architecture
#      independent, and it cannot be rebuilt on riscv64: the bundle is produced by
#      @oxc-node/core (no riscv64 build) running rolldown itself, so bootstrapping
#      it would need a working binding before the glue exists.
#
# The wrapper tarball is pinned by digest and republished unchanged except for the
# manifest name, so the JavaScript that ships here is byte-for-byte what upstream
# published.
set -euo pipefail

SRC_DIR="${SRC_DIR:-src}"
TMP_DIR="${TMP_DIR:-.fetch-tmp}"
UPSTREAM_REPO="https://github.com/rolldown/rolldown"
UPSTREAM_REF="v1.3.3"
GLUE_URL="https://registry.npmjs.org/rolldown/-/rolldown-1.3.3.tgz"
GLUE_SHA512="92c07583be1eee1691cb4d8e626e25390025afab0aea1069018e51f980b7d0692fe0bd423b017a33dba60406003f15a91a8c3a56bf885edc838c7f5fabb03e66"

echo "[fetch] route upstream: $UPSTREAM_REPO@$UPSTREAM_REF"
rm -rf "$SRC_DIR"
if ! git clone --quiet --depth 1 --branch "$UPSTREAM_REF" "$UPSTREAM_REPO" "$SRC_DIR" 2>/dev/null; then
  echo "  [fetch] shallow clone of '$UPSTREAM_REF' failed, retrying with a full clone" >&2
  rm -rf "$SRC_DIR"
  git clone --quiet "$UPSTREAM_REPO" "$SRC_DIR" || exit 1
  git -C "$SRC_DIR" checkout --quiet "$UPSTREAM_REF" || exit 1
fi

echo "[fetch] route npm: $GLUE_URL"
mkdir -p "$TMP_DIR"
curl -sSfL -o "$TMP_DIR/rolldown-1.3.3.tgz" "$GLUE_URL"
echo "${GLUE_SHA512}  ${TMP_DIR}/rolldown-1.3.3.tgz" | sha512sum -c - >/dev/null

# The wrapper goes inside the source tree so the build, artifact paths and the
# publish directory all stay relative to one root.
rm -rf "$SRC_DIR/out/rolldown"
mkdir -p "$SRC_DIR/out/rolldown"
tar xzf "$TMP_DIR/rolldown-1.3.3.tgz" -C "$SRC_DIR/out/rolldown" --strip-components=1
test -f "$SRC_DIR/out/rolldown/dist/index.mjs" || {
  echo "ERROR: the npm tarball has no dist/index.mjs" >&2
  exit 1
}
test -f "$SRC_DIR/out/rolldown/package.json" || {
  echo "ERROR: the npm tarball has no package.json" >&2
  exit 1
}

echo "[fetch] rust sources at $SRC_DIR ($(ls -A "$SRC_DIR" | wc -l) entries), js wrapper at $SRC_DIR/out/rolldown"
