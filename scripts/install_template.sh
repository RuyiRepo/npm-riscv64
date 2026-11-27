#!/usr/bin/env bash
# Installs a published package the way a consumer does, on hardware, from the registry
# the recipe names for reading (physical.registry - a group repository that aggregates
# the private one, .dsh/plan.md D41).
#
# What this step proves and what it refuses to assume:
#
#   * the bytes come from the registry a consumer points at, not from the build tree;
#   * those bytes are the ones the pipeline published - the publish report carries the
#     sha512 npm uploaded, and a hardware run that installs anything else verifies
#     nothing;
#   * installation goes through npm's own resolution, so optionalDependencies (how
#     rolldown gets its binding) are exercised rather than bypassed.
#
# Failure codes are verdicts (see recipe_lib.EXIT_*): "the registry cannot be reached"
# and "the artifact is wrong" are different news and must not be reported alike.
set -euo pipefail

PKG="@@NAME@@"
VER="@@VERSION@@"

WORK="${PHYSICAL_WORK:?physical.sh sets PHYSICAL_WORK}"
CONSUMER="${PHYSICAL_CONSUMER:?physical.sh sets PHYSICAL_CONSUMER}"
REGISTRY="${PHYSICAL_REGISTRY:?physical.sh sets PHYSICAL_REGISTRY}"
SOURCE="${PHYSICAL_SOURCE:-@@SOURCE@@}"
TARBALL="${PHYSICAL_TARBALL:-}"
EXPECTED="${PHYSICAL_EXPECTED_INTEGRITY:-}"
FALLBACK="${PHYSICAL_FALLBACK:-false}"

EXIT_REGISTRY=11
EXIT_DIGEST=12
EXIT_INSTALL=13

log() { echo "[install] $*"; }
fail() {
  local code="$1"
  shift
  echo "[install] ERROR: $*" >&2
  exit "$code"
}

mkdir -p "$WORK/pack"
: > "$WORK/pack.err"

# npm answers `pack --json` in two shapes - an array (npm <= 10) and a bare object
# (npm >= 11). Reading one shape only would turn "cannot read the answer" into "the
# registry said nothing", which is exactly the mistake D39 is about.
pack_field() {
  node -e '
    const fs = require("node:fs");
    const [file, field] = process.argv.slice(1);
    let raw;
    try { raw = JSON.parse(fs.readFileSync(file, "utf8")); } catch { process.exit(1); }
    // npm <= 10 prints an array, npm >= 11 an object keyed by package name; both are
    // read the same way the publish helper reads them.
    const doc = Array.isArray(raw) ? raw[0] : Object.values(raw)[0];
    const value = doc && doc[field];
    if (value === undefined || value === null) process.exit(1);
    process.stdout.write(String(value));
  ' "$WORK/pack.json" "$1"
}

# sha512 in npm's SRI form, computed locally: the digest the publish report carries is
# the digest of the tarball npm uploaded, so the tarball downloaded here has to hash to
# the same value.
tgz_integrity() {
  node -e '
    const fs = require("node:fs");
    const crypto = require("node:crypto");
    const hash = crypto.createHash("sha512").update(fs.readFileSync(process.argv[1]));
    process.stdout.write("sha512-" + hash.digest("base64"));
  ' "$1"
}

tgz=""
if [ "$SOURCE" = "registry" ]; then
  log "npm pack $PKG@$VER from $REGISTRY"
  if npm pack "$PKG@$VER" --registry "$REGISTRY" --pack-destination "$WORK/pack" \
      --json --loglevel error >"$WORK/pack.json" 2>"$WORK/pack.err"; then
    tgz="$(pack_field filename 2>/dev/null || true)"
    if [ -n "$tgz" ]; then
      [ -f "$WORK/pack/$tgz" ] || tgz=""
      physical_fact source "registry"
    fi
  else
    log "npm pack failed, stderr:"
    sed -e 's/^/  /' "$WORK/pack.err" >&2 || true
  fi
fi

if [ -z "$tgz" ]; then
  # The fallback exists for one situation: the publish report says the registry serves
  # nothing checkable (`served: unverified`), so the build kept the tarball it uploaded.
  # It is not a way around a network problem - if the report holds a digest, the
  # registry does serve these bytes and this machine failing to read them is an
  # environment failure, not a reason to verify something else.
  if [ "$SOURCE" = "tarball" ] || [ "$FALLBACK" = "true" ]; then
    [ -n "$TARBALL" ] || fail "$EXIT_REGISTRY" \
      "the fallback tarball was requested but none was given (PHYSICAL_TARBALL is empty)"
    [ -f "$TARBALL" ] || fail "$EXIT_REGISTRY" \
      "the fallback tarball is missing: $TARBALL"
    cp -f "$TARBALL" "$WORK/pack/"
    tgz="$(basename "$TARBALL")"
    physical_fact source "tarball"
    log "using the build's tarball: $TARBALL"
  else
    fail "$EXIT_REGISTRY" \
      "cannot read $PKG@$VER from $REGISTRY, and the publish report holds a digest," \
      "which means the registry does serve it: this is a network or credential problem" \
      "on this machine, not an artifact problem"
  fi
fi

tgz_path="$WORK/pack/$tgz"
log "tarball: $tgz_path"
physical_fact tarball "$(basename "$tgz_path")"

integrity="$(tgz_integrity "$tgz_path")"
physical_fact installed_integrity "$integrity"
if [ -n "$EXPECTED" ]; then
  if [ "$integrity" != "$EXPECTED" ]; then
    fail "$EXIT_DIGEST" \
      "the bytes served for $PKG@$VER are not the ones the pipeline published:" \
      "$integrity (here) != $EXPECTED (publish report)"
  fi
  physical_check digest ok "$integrity"
else
  # Not the same thing as "the registry serves something else": this run was simply
  # not handed a digest, so the bytes are exercised but not proven. A manual run
  # (no publish report) is the usual reason.
  physical_check digest unknown \
    "this run was given no digest for $PKG@$VER, so the served bytes are exercised" \
    "but not proven to be the published ones"
fi

# Install through npm, as a consumer would: a tarball install still resolves the
# package's own dependencies from the registry, which is what pulls
# @rolldown/binding-linux-riscv64-gnu in for the rolldown wrapper.
log "npm install into $CONSUMER"
if ! ( cd "$CONSUMER" && npm install "$tgz_path" --registry "$REGISTRY" \
        --no-audit --no-fund --loglevel error ); then
  fail "$EXIT_INSTALL" "npm failed to install $tgz_path"
fi

manifest="$CONSUMER/node_modules/$PKG/package.json"
[ -f "$manifest" ] || fail "$EXIT_INSTALL" "npm did not install $PKG (no $manifest)"
installed_version="$(node -e '
  process.stdout.write(String(require(process.argv[1]).version || ""));
' "$manifest")"
[ "$installed_version" = "$VER" ] || fail "$EXIT_INSTALL" \
  "installed $PKG at version '$installed_version', expected '$VER'"

physical_check installed ok "$PKG@$VER"
physical_fact installed "$PKG@$VER"
printf '%s\n' "$tgz_path" >"$WORK/tgz.path"
log "installed $PKG@$VER"
