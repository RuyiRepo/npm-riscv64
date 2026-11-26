set -euo pipefail

# Publishing runs on the host: moving files and talking to a registry does not need
# riscv64 emulation, and it keeps token handling straightforward.
REPO_ROOT="$(pwd)"
SRC_DIR="${SRC_DIR:-src}"
# The manifest helper travels next to this script, so no path comes from the workflow.
STEPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export NPM_CONFIG_USERCONFIG="$REPO_ROOT/.npmrc.publish"
: > "$NPM_CONFIG_USERCONFIG"

log() { echo "[publish] $*"; }

# Temporary package trees go away however the step ends.
WORK_DIRS=()
cleanup() {
  local dir
  for dir in "${WORK_DIRS[@]}"; do
    if [ -n "$dir" ]; then
      rm -rf "$dir"
    fi
  done
}
trap cleanup EXIT

setup_auth() {
  local registry="$1" token_env="$2" auth_kind="$3"
  [ -n "$token_env" ] || return 0
  if [ -z "${!token_env:-}" ]; then
    log "no credential in $token_env; talking to $registry unauthenticated"
    return 0
  fi
  # npm looks credentials up by the registry URL it talks to, path included:
  # "//host/repository/name/" is the key for a registry at .../repository/name/.
  local dart="${registry#*://}"
  dart="${dart%%\?*}"
  dart="//$dart"
  [ "${dart: -1}" = "/" ] || dart="$dart/"
  if [ "$auth_kind" = "basic" ]; then
    # Basic credentials: base64 of user:password, as the registry asks for it
    # (WWW-Authenticate: Basic).
    printf '%s:_auth=${%s}\n' "$dart" "$token_env" >> "$NPM_CONFIG_USERCONFIG"
  else
    printf '%s:_authToken=${%s}\n' "$dart" "$token_env" >> "$NPM_CONFIG_USERCONFIG"
  fi
  log "auth configured for $dart via $token_env ($auth_kind)"
}

publish_pkg() {
  # Reads one publish spec (JSON) from stdin, prepares the package directory and
  # publishes it. See generate_publish in scripts/render.py for the two kinds:
  #
  #   source-tree       the directory the build produced, inside $SRC_DIR
  #   upstream-tarball  the upstream npm package, verified against base.integrity and
  #                     extended with the recipe's overlay, so the published package
  #                     keeps upstream's structure and gains only our riscv64 files
  local spec_json assignments
  spec_json="$(cat)"
  # A failing helper must stop the step: eval of an empty string would succeed.
  assignments="$(node "$STEPS_DIR/manifest.mjs" --shell "$spec_json")" || return 1
  eval "$assignments"
  log "package $P_NAME@$P_VERSION (kind=$P_KIND, manifest=$P_MANIFEST)"

  local work_dir publish_dir
  work_dir="$(mktemp -d "$REPO_ROOT/.publish-work.XXXXXX")"
  WORK_DIRS+=("$work_dir")

  if [ "$P_KIND" = "upstream-tarball" ]; then
    [ -n "$P_BASE_URL" ] && [ -n "$P_BASE_INTEGRITY" ] || {
      log "upstream-tarball needs base.url and base.integrity"
      return 1
    }
    curl -sSfL --retry 3 -o "$work_dir/base.tgz" "$P_BASE_URL" || {
      log "cannot fetch $P_BASE_URL"
      return 1
    }
    node "$STEPS_DIR/manifest.mjs" --verify-integrity "$spec_json" "$work_dir/base.tgz" ||
      return 1
    mkdir -p "$work_dir/pkg"
    tar xzf "$work_dir/base.tgz" -C "$work_dir/pkg" --strip-components=1 || {
      log "cannot unpack $P_BASE_URL"
      return 1
    }
    local rel src_rel dst_rel
    for rel in $P_OVERLAY; do
      src_rel="${rel%%:*}"
      dst_rel="${rel#*:}"
      [ "$dst_rel" = "$rel" ] && dst_rel="$src_rel"
      [ -e "$SRC_DIR/$src_rel" ] || {
        log "overlay source missing: $SRC_DIR/$src_rel"
        return 1
      }
      if [ -e "$work_dir/pkg/$dst_rel" ] && [ "$P_ALLOW_OVERWRITE" != "true" ]; then
        log "refusing to overwrite an upstream file with the overlay: $dst_rel"
        log "the recipe can allow this with allow_overwrite: true"
        return 1
      fi
      mkdir -p "$(dirname "$work_dir/pkg/$dst_rel")"
      cp -a "$SRC_DIR/$src_rel" "$work_dir/pkg/$dst_rel"
      log "overlay $src_rel -> $dst_rel"
    done
    publish_dir="$work_dir/pkg"
  else
    publish_dir="$SRC_DIR/$P_DIR"
    [ -d "$publish_dir" ] || {
      log "missing package directory: $publish_dir"
      return 1
    }
  fi

  node "$STEPS_DIR/manifest.mjs" --prepare "$spec_json" "$publish_dir" || return 1

  # Always publish an absolute path: npm reads a two-segment relative path such as
  # "src/." as a github shorthand spec, not as a directory.
  local abs_dir
  abs_dir="$(cd "$publish_dir" && pwd)" || {
    log "cannot enter $publish_dir"
    return 1
  }

  # How long the upload may take is part of the publish, not an afterthought. npm's
  # `fetch-timeout` covers the whole request - upload included - in every version from
  # 9 to 12, so its 300 s default silently caps a package by how fast the route is: a
  # binding-sized tarball (9.3 MB, 12.2 MB of base64 body) needs minutes, and on a
  # slower route npm aborts mid-body, retries, and the registry never sees a complete
  # request (witnessed as `PUT ... attempt 1 failed with FETCH_ERROR` and "network
  # timeout at ..." while the registry logged nothing). The budget is therefore
  # derived from the packed size (§6.4), and both numbers are logged.
  local plan assignments2 packed_bytes fetch_timeout fetch_retries
  assignments2="$(node "$STEPS_DIR/manifest.mjs" --plan "$spec_json" "$publish_dir")" || return 1
  eval "$assignments2"
  packed_bytes="$P_PACKED_BYTES"
  fetch_timeout="$P_FETCH_TIMEOUT"
  fetch_retries="$P_FETCH_RETRIES"
  log "upload $packed_bytes bytes, fetch timeout ${fetch_timeout}ms"

  # Publish, then read the registry answer instead of trusting a lookup: a registry
  # that proxies or groups another one also serves upstream versions of the same
  # name, so "npm view <name>@<version>" would call our own package published and
  # silently skip the point. Only the target registry knows about its own content.
  #
  # The output is streamed rather than captured: a slow upload otherwise looks frozen
  # in the log until npm exits, which is exactly how a stuck publish reads.
  # --force is not about ignoring errors: npm >= 11 refuses, client-side and before
  # sending anything, to publish a version the registry already knows ("You cannot
  # publish over the previously published versions"). A mirror carrying upstream's
  # release always answers that way, and the refuse-to-replace rule is the wrong rule
  # here: the tarball this recipe publishes is upstream's release plus the riscv64
  # increment, so replacing it is the whole point. Letting the registry decide costs
  # one round trip and tells the truth either way (see the assertion below).
  local out_file status
  out_file="$(mktemp)"
  set +e
  if [ -n "$fetch_retries" ]; then
    npm publish "$abs_dir" --registry "$P_REGISTRY" --access public --force \
      --ignore-scripts --no-audit --no-fund --loglevel http \
      --fetch-timeout "$fetch_timeout" --fetch-retries "$fetch_retries" 2>&1 |
      tee "$out_file"
  else
    npm publish "$abs_dir" --registry "$P_REGISTRY" --access public --force \
      --ignore-scripts --no-audit --no-fund --loglevel http \
      --fetch-timeout "$fetch_timeout" 2>&1 |
      tee "$out_file"
  fi
  status=${PIPESTATUS[0]}
  set -e
  local refused="" accepted=""
  if [ $status -eq 0 ]; then
    accepted="yes"
    log "published $P_NAME@$P_VERSION"
  elif grep -qE "EPUBLISHCONFLICT|409 Conflict|already exists|previously published|cannot publish over" "$out_file"; then
    refused="yes"
    log "the registry refused to replace $P_NAME@$P_VERSION; checking what it serves"
  else
    log "publish failed: $P_NAME@$P_VERSION"
    printf '%s\n' "$out_file" >&2
    log "registry answer is in $out_file"
    return 1
  fi
  rm -f "$out_file"

  # The upload being accepted is not the same claim as the registry serving our bytes,
  # and only the second one matters to a consumer. A refused publish whose version
  # already holds someone else's artifact stops the step here instead of passing as
  # "already published" - that is how a green build once left the mirror serving
  # upstream's package without the riscv64 increment.
  #
  # `--accepted` matters when the registry publishes no digest at all: after a 201 the
  # absence of evidence is a warning (the artifact is there, just not checkable), while
  # after a refusal it is a failure (nothing at all shows our bytes are what a
  # consumer gets).
  if [ -n "$accepted" ]; then
    node "$STEPS_DIR/manifest.mjs" --assert-served "$spec_json" "$abs_dir" --accepted || return 1
  else
    node "$STEPS_DIR/manifest.mjs" --assert-served "$spec_json" "$abs_dir" || return 1
  fi
  if [ -n "$refused" ]; then
    log "already published: $P_NAME@$P_VERSION (identical artifact, nothing to do)"
  fi
  return 0
}

# Credentials first: publishing may need auth before it can answer.
setup_auth "https://forgestash.artifact.yuantu.ac.cn/repository/npm-riscv-local/" "NODE_AUTH_TOKEN" "basic"

# Publish order: platform packages first, then the package that references them
# through optionalDependencies.
publish_pkg <<'PUBLISH_SPEC'
{
  "cpu": "riscv64",
  "dir": "out/binding",
  "engines": {
    "node": "^20.19.0 || >=22.12.0"
  },
  "kind": "source-tree",
  "libc": "glibc",
  "main": "rolldown-binding.linux-riscv64-gnu.node",
  "manifest": "template",
  "manifest_json": {
    "bugs": {
      "url": "https://github.com/rolldown/rolldown/issues"
    },
    "cpu": [
      "riscv64"
    ],
    "description": "Fast JavaScript/TypeScript bundler in Rust with Rollup-compatible API.",
    "engines": {
      "node": "^20.19.0 || >=22.12.0"
    },
    "files": [
      "rolldown-binding.linux-riscv64-gnu.node"
    ],
    "homepage": "https://rolldown.rs/",
    "keywords": [
      "bundler",
      "esbuild",
      "parcel",
      "rolldown",
      "rollup",
      "webpack"
    ],
    "libc": [
      "glibc"
    ],
    "license": "MIT",
    "main": "rolldown-binding.linux-riscv64-gnu.node",
    "name": "@rolldown/binding-linux-riscv64-gnu",
    "os": [
      "linux"
    ],
    "publishConfig": {
      "access": "public",
      "registry": "https://forgestash.artifact.yuantu.ac.cn/repository/npm-riscv-local/"
    },
    "repository": {
      "directory": "packages/rolldown",
      "type": "git",
      "url": "git+https://github.com/rolldown/rolldown.git"
    },
    "version": "1.3.3"
  },
  "name": "@rolldown/binding-linux-riscv64-gnu",
  "os": "linux",
  "readme_text": "# `@rolldown/binding-linux-riscv64-gnu`\n\nThis is the **riscv64gc-unknown-linux-gnu** binary for `@rolldown/binding`\n",
  "registry": "https://forgestash.artifact.yuantu.ac.cn/repository/npm-riscv-local/",
  "version": "1.3.3"
}
PUBLISH_SPEC

publish_pkg <<'PUBLISH_SPEC'
{
  "dir": "out/rolldown",
  "kind": "source-tree",
  "manifest": "upstream",
  "name": "rolldown",
  "optional": {
    "@rolldown/binding-linux-riscv64-gnu": "1.3.3"
  },
  "registry": "https://forgestash.artifact.yuantu.ac.cn/repository/npm-riscv-local/",
  "version": "1.3.3"
}
PUBLISH_SPEC

log "publish finished for rolldown@1.3.3"
