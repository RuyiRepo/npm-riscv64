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

@@AUTH_FN@@
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
  #
  # The report this writes is the input of the hardware run (.dsh/plan.md D41): it
  # carries the digest the upload had, so the machine that installs the package on real
  # silicon can prove it installed the same bytes.
  # Plain directory names, not dot-prefixed: the build workflow uploads these with
  # actions/upload-artifact, which skips hidden files and directories by default - a
  # ".publish-reports" path silently uploaded nothing once already.
  local report_dir report_path served_state tarball_name
  report_dir="${PUBLISH_REPORT_DIR:-$REPO_ROOT/publish-reports}"
  mkdir -p "$report_dir"
  report_path="$report_dir/$(printf '%s' "$P_NAME" | sed -e 's/@/_/g' -e 's|/|_|g').json"
  if [ -n "$accepted" ]; then
    node "$STEPS_DIR/manifest.mjs" --assert-served "$spec_json" "$abs_dir" \
      --accepted --report "$report_path" || return 1
  else
    node "$STEPS_DIR/manifest.mjs" --assert-served "$spec_json" "$abs_dir" \
      --report "$report_path" || return 1
  fi
  served_state="$(node -e '
    const fs = require("node:fs");
    process.stdout.write(String(JSON.parse(fs.readFileSync(process.argv[1], "utf8")).served || ""));
  ' "$report_path")"

  # The one case that needs the bytes kept: the registry accepted the upload but serves
  # nothing checkable, so a hardware run cannot tell whether it installed our artifact.
  # Only then is a tarball uploaded for it - a confirmed digest already says the
  # registry serves these bytes, and a machine failing to read them is that machine's
  # problem, not a reason to verify something else.
  if [ "$served_state" = "unverified" ]; then
    mkdir -p "$REPO_ROOT/physical-tarball"
    # The name npm gives the tarball is the name the hardware run has to look for
    # (`npm pack` strips the scope from the file name), so it is read from npm rather
    # than guessed. The JSON answer is an array on npm <= 10 and a bare object on 11+.
    tarball_name="$(npm pack "$abs_dir" --pack-destination "$REPO_ROOT/physical-tarball" \
      --ignore-scripts --loglevel error --json | node -e '
        let raw = "";
        process.stdin.on("data", (chunk) => (raw += chunk));
        process.stdin.on("end", () => {
          const doc = JSON.parse(raw);
          process.stdout.write(String((Array.isArray(doc) ? doc[0] : doc).filename || ""));
        });
      ')" || {
      log "cannot pack the fallback tarball for the hardware run"
      return 1
    }
    [ -n "$tarball_name" ] || {
      log "npm pack reported no file name for the fallback tarball"
      return 1
    }
    log "the report cannot confirm what $P_REGISTRY serves; kept $tarball_name for the hardware run"
    node -e '
      const fs = require("node:fs");
      const [path, name] = process.argv.slice(1);
      const doc = JSON.parse(fs.readFileSync(path, "utf8"));
      doc.tarball = name;
      fs.writeFileSync(path, JSON.stringify(doc, null, 2) + "\n");
    ' "$report_path" "$tarball_name"
  fi
  if [ -n "$refused" ]; then
    log "already published: $P_NAME@$P_VERSION (identical artifact, nothing to do)"
  fi
  return 0
}

# Credentials first: publishing may need auth before it can answer.
@@AUTH@@

# Publish order: platform packages first, then the package that references them
# through optionalDependencies.
@@SPECS@@

log "publish finished for @@IDENT@@"
