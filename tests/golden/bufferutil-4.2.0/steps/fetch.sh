set -euo pipefail

SRC_DIR="${SRC_DIR:-src}"
TMP_DIR="${TMP_DIR:-.fetch-tmp}"
mkdir -p "$TMP_DIR"
echo "[fetch] resolved source routes: upstream"

fetch_upstream() {
  echo "  [fetch] route upstream: https://github.com/websockets/bufferutil@v4.2.0"
  rm -rf "$SRC_DIR"
  if ! git clone --quiet --depth 1 --branch 'v4.2.0' 'https://github.com/websockets/bufferutil' "$SRC_DIR" 2>/dev/null; then
    echo "  [fetch] shallow clone of 'v4.2.0' failed, retrying with a full clone" >&2
    rm -rf "$SRC_DIR"
    git clone --quiet 'https://github.com/websockets/bufferutil' "$SRC_DIR" || return 1
    git -C "$SRC_DIR" checkout --quiet 'v4.2.0' || return 1
  fi
  # Keep .git: the patch step can then use `git apply --3way` for context drift.
}

fetch_source() {
  echo "  [fetch] route source: not configured" >&2
  return 1
}

fetch_npm() {
  echo "  [fetch] route npm: not configured" >&2
  return 1
}

# Highest priority wins: upstream > source > npm. A route only falls through
# when it actually fails, so a broken mirror never blocks a good upstream.
if fetch_upstream; then
  :
elif fetch_source; then
  :
elif fetch_npm; then
  :
else
  echo "ERROR: every source route failed (upstream)" >&2
  exit 1
fi

echo "[fetch] source tree ready at $SRC_DIR ($(ls -A "$SRC_DIR" | wc -l) entries)"
