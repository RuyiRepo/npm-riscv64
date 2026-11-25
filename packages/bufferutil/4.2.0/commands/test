#!/usr/bin/env bash
# Tests the prebuild the way a consumer on this platform uses it: upstream's own
# suite, plus the riscv64 cases upstream has no way to run.
#
# This step only ever runs on riscv64 (.dsh/plan.md N1), so nothing here may assume
# another architecture. A case that would need one belongs in
# tests/riscv64-skip.txt with its reason, never in an edited upstream test file.
set -euo pipefail

pkg=bufferutil
artifact="prebuilds/linux-riscv64/$pkg.node"

fail() { echo "ERROR: $*" >&2; exit 1; }

# Upstream cases to skip, read from the recipe: one case per line, "#" starts the
# reason. The list is data for this step, not part of the build.
skips=()
recipe_dir="${RECIPE_DIR:-}"
if [ -n "$recipe_dir" ] && [ -f "$recipe_dir/tests/riscv64-skip.txt" ]; then
  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [ -n "$line" ]; then
      skips+=("$line")
    fi
  done < "$recipe_dir/tests/riscv64-skip.txt"
fi

echo "== upstream suite (npm test runs mocha)"
if [ "${#skips[@]}" -gt 0 ]; then
  pattern="$(printf '%s\n' "${skips[@]}" | paste -sd'|' -)"
  echo "skipped on riscv64: $pattern"
  npm test -- --grep "^($pattern)$" --invert
else
  npm test
fi

echo "== riscv64 prebuild: $artifact"
[ -f "$artifact" ] || fail "missing artifact $artifact"
file "$artifact"
file "$artifact" | grep -Eqi 'risc-?v' || fail "artifact is not a RISC-V object"

# The overlay copies the whole prebuilds/linux-riscv64 directory into the upstream
# package, so anything else that lands here is published too: exactly one file,
# named the way node-gyp-build expects it.
shipped=("prebuilds/linux-riscv64"/*)
[ "${#shipped[@]}" -eq 1 ] || fail "prebuilds/linux-riscv64 holds ${#shipped[@]} entries, the overlay must add exactly one file"
[ -f "${shipped[0]}" ] || fail "the overlay must hold a file, got ${shipped[0]}"
[ "$(basename "${shipped[0]}")" = "$pkg.node" ] || fail "expected $pkg.node, got $(basename "${shipped[0]}")"

# A consumer installs from a registry, where build/ does not exist: node-gyp-build
# must then resolve the prebuild this recipe publishes instead of compiling.
if [ -d build ]; then
  mv build build.hidden
  trap 'mv build.hidden build' EXIT
fi

node - <<'EOF'
const assert = require('node:assert');
const resolved = require('node-gyp-build').path(__dirname);
assert.ok(
  resolved.includes('prebuilds/linux-riscv64'),
  `expected the riscv64 prebuild, got: ${resolved}`
);

const { mask, unmask } = require(resolved);
const payload = Buffer.from('ruyi-riscv64-bufferutil');
const key = Buffer.from('0123456789abcdef');
const masked = Buffer.alloc(payload.length);

mask(payload, key, masked, 0, payload.length);
assert.notDeepStrictEqual(masked, payload, 'mask must change the payload');
unmask(masked, key);
assert.deepStrictEqual(masked, payload, 'unmask must restore the payload');

console.log(`bufferutil ok: ${resolved} round-tripped ${payload.length} bytes`);
EOF
