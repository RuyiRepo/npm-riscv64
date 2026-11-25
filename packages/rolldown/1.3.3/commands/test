#!/bin/bash
# Runs on riscv64 (the test image), unlike the build step which runs on amd64: the
# point of this step is that the cross-built binding loads and bundles JavaScript on
# the target CPU, in the layout a consumer installs.
#
# Upstream's own JavaScript tests are the suite: the three
# tests/behaviors/emit-debug-data/*.test.js files, the A tier agreed in
# .dsh/plan.md D30. They exercise the native binding through the public API and need
# no TypeScript toolchain. Before they run, this step checks what only the riscv64
# side can check:
#
#   1. the artifact is a RISC-V object (the build asserted it too, from the other side
#      of the cross build);
#   2. the wrapper carries no .node file - upstream ships the binary as its own
#      package, and a binding inside the wrapper would be published for every CPU;
#   3. the platform package packs exactly manifest + README + binary, the shape of
#      upstream's own platform tarballs, and the package a consumer installs resolves
#      the binding through @rolldown/binding-linux-riscv64-gnu (the loader's riscv64
#      branch) rather than through dist/.
set -euo pipefail

WRAPPER="out/rolldown"
BINDING_DIR="out/binding"
BINDING_NAME="rolldown-binding.linux-riscv64-gnu.node"
BINDING_PKG="@rolldown/binding-linux-riscv64-gnu"
ARTIFACT="$BINDING_DIR/$BINDING_NAME"

fail() { echo "ERROR: $*" >&2; exit 1; }

# The vitest config and the manifest template belong to the recipe, not to this
# repository's tooling: the step reads them from RECIPE_DIR, which the workflow
# exports (the same variable carries the skip list below).
recipe_dir="${RECIPE_DIR:-}"
[ -n "$recipe_dir" ] || fail "RECIPE_DIR is not set; this step reads its config from the recipe"
expected_version="$(basename "$recipe_dir")"

echo "== riscv64 binding"
[ -f "$ARTIFACT" ] || fail "missing $ARTIFACT (the build step places it there)"
if ! file -b "$ARTIFACT" | grep -q "RISC-V"; then
  file "$ARTIFACT" >&2
  fail "$ARTIFACT is not a RISC-V object"
fi
echo "[test] artifact: $(file -b "$ARTIFACT" | cut -d, -f1-3)"

echo "== the wrapper stays upstream's"
if find "$WRAPPER" -name '*.node' -print -quit | grep -q .; then
  fail "$WRAPPER carries a .node file; the binding ships as its own package"
fi
echo "[test] no .node file under $WRAPPER"

echo "== the platform package, packed the way the publish step packs it"
version="$(node -p 'require("./out/rolldown/package.json").version')"
[ "$version" = "$expected_version" ] || fail "the wrapper is $version, the recipe publishes $expected_version"

work="$PWD/.riscv64-tests"
rm -rf "$work"
mkdir -p "$work/app" "$work/binding"
cp "$ARTIFACT" "$work/binding/"

# One substitution table for both recipe templates, so the packed package is the one
# the publish step would produce rather than a test-only stand-in.
render() {
  sed -e "s|{{name}}|$BINDING_PKG|" \
      -e "s|{{version}}|$version|" \
      -e "s|{{registry}}|https://registry.npmjs.org/|" \
      -e "s|{{os}}|linux|" \
      -e "s|{{cpu}}|riscv64|" \
      -e "s|{{libc}}|glibc|" \
      -e "s|{{main}}|$BINDING_NAME|" \
      "$1"
}
render "$recipe_dir/templates/package.json" > "$work/binding/package.json"
render "$recipe_dir/templates/README.md" > "$work/binding/README.md"

(
  cd "$work/binding"
  npm pack --dry-run --json --ignore-scripts > "$work/binding.json"
)
node - "$work/binding.json" "$BINDING_NAME" <<'JS'
const fs = require("node:fs");
const [, , reportPath, binary] = process.argv;
const report = JSON.parse(fs.readFileSync(reportPath, "utf8"))[0];
const files = (report.files || []).map((f) => f.path).sort();
const expected = [binary, "README.md", "package.json"].sort();
if (JSON.stringify(files) !== JSON.stringify(expected)) {
  console.error(`[test] the platform package would ship ${JSON.stringify(files)}, expected ${JSON.stringify(expected)}`);
  process.exit(1);
}
console.log(`[test] the platform package ships ${files.join(", ")} (${report.unpackedSize} bytes unpacked)`);
JS

# The wrapper has to pack without a binary: upstream's "!dist/*.node" only excludes
# direct children of dist/, so a binding anywhere below dist/ would silently ship to
# every architecture - which is exactly what this recipe used to do.
(
  cd "$WRAPPER"
  npm pack --dry-run --json --ignore-scripts > "$work/wrapper.json"
)
node - "$work/wrapper.json" <<'JS'
const fs = require("node:fs");
const [, , reportPath] = process.argv;
const report = JSON.parse(fs.readFileSync(reportPath, "utf8"))[0];
const files = (report.files || []).map((f) => f.path);
const binaries = files.filter((f) => f.endsWith(".node"));
if (binaries.length) {
  console.error(`[test] npm would publish ${binaries.join(", ")} inside the wrapper`);
  process.exit(1);
}
console.log(`[test] the wrapper ships ${report.entryCount} files and no binary`);
JS

echo "== install what a consumer gets"
(cd "$WRAPPER" && npm pack --pack-destination "$work/app" >/dev/null)
(cd "$work/binding" && npm pack --pack-destination "$work/app" >/dev/null)
cp "$recipe_dir/tests/package.json" "$work/app/package.json"
(
  cd "$work/app"
  npm install --no-audit --no-fund --no-package-lock --ignore-scripts \
    vitest "rolldown-$version.tgz" "rolldown-binding-linux-riscv64-gnu-$version.tgz" >/dev/null
)

installed="$work/app/node_modules/$BINDING_PKG"
[ -f "$installed/$BINDING_NAME" ] || fail "npm did not install $BINDING_PKG"
[ -f "$work/app/node_modules/rolldown/dist/index.mjs" ] || fail "npm did not install the wrapper"
if find "$work/app/node_modules/rolldown" -name '*.node' -print -quit | grep -q .; then
  fail "the installed wrapper carries a .node file, so the binding package is not what gets loaded"
fi
echo "[test] installed $BINDING_PKG@$(node -p "require('$installed/package.json').version") next to rolldown@$version"

# Upstream's tests import "rolldown" and "vitest" from the source tree, so the
# scratch install is linked where node resolution looks for it.
ln -sfn ".riscv64-tests/app/node_modules" node_modules

echo "== upstream's own tests (vitest, tests/behaviors/emit-debug-data)"
exclude=()
skip_file="$recipe_dir/tests/riscv64-skip.txt"
if [ -f "$skip_file" ]; then
  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(printf '%s' "$line" | tr -d '[:space:]')"
    if [ -n "$line" ]; then
      exclude+=(--exclude "$line")
    fi
  done < "$skip_file"
fi
"$work/app/node_modules/.bin/vitest" run \
  --config "$recipe_dir/tests/vitest.config.mjs" \
  ${exclude[@]+"${exclude[@]}"} \
  packages/rolldown/tests/behaviors/emit-debug-data
