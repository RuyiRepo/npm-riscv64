#!/usr/bin/env bash
# Runs a recipe's install + verify steps on real riscv64 hardware and writes the
# report that says what was proven.
#
# This wrapper exists because a hardware run has to be self-contained on a machine
# nobody cleans up for it (.dsh/plan.md D41):
#
#   * everything happens inside one mktemp directory, with HOME and npm's cache
#     redirected there - the runner is a long-lived container shared with other jobs,
#     so a stray global install or a cache in the runner's HOME would leak into the
#     next run;
#   * the directory is removed however the step ends, including on failure;
#   * the hardware facts (uname, model, ISA string, glibc, node) are collected here,
#     once, so every recipe reports them the same way;
#   * a failure still produces a report: "the machine is not ready" and "the artifact
#     is broken" are different news, and only the second one is about the package.
#
# There is no docker here on purpose: the runner runs inside a container already and
# docker-in-docker is unproven on it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IDENT="@@IDENT@@"
PKG="@@NAME@@"
VER="@@VERSION@@"
SLUG="@@SLUG@@"

export PHYSICAL_REGISTRY="${PHYSICAL_REGISTRY:-@@REGISTRY@@}"
export PHYSICAL_SOURCE="${PHYSICAL_SOURCE:-@@SOURCE@@}"
export PHYSICAL_EXPECTED_INTEGRITY="${PHYSICAL_EXPECTED_INTEGRITY:-}"
export PHYSICAL_TARBALL="${PHYSICAL_TARBALL:-}"

MIN_FREE_MB="${PHYSICAL_MIN_FREE_MB:-5000}"
STARTED_AT="$(date +%s)"

log() { echo "[physical] $*"; }

cpuinfo_field() { # field prefix; first match, stripped of its key
  awk -v key="$1" 'index($0, key) == 1 {
    sub(/^[^:]*:[[:space:]]*/, "");
    print;
    exit
  }' /proc/cpuinfo
}

@@AUTH_FN@@

WORK="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/physical-$SLUG.XXXXXX")"
cleanup() {
  if [ "${PHYSICAL_KEEP_WORK:-false}" = "true" ]; then
    log "keeping the working directory for inspection: $WORK"
    return 0
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

# Isolation: nothing this run does may outlive it.
export PHYSICAL_WORK="$WORK"
export HOME="$WORK/home"
export npm_config_cache="$WORK/npm-cache"
export NPM_CONFIG_USERCONFIG="$WORK/.npmrc"
mkdir -p "$HOME" "$npm_config_cache" "$WORK/consumer"
: > "$NPM_CONFIG_USERCONFIG"
export PHYSICAL_CONSUMER="$WORK/consumer"
# A consumer project has to be a real one: npm resolves and records dependencies
# relative to it, and installing a tarball still pulls the package's own
# dependencies from the registry.
cat >"$PHYSICAL_CONSUMER/package.json" <<'CONSUMER_JSON'
{ "name": "physical-verify", "version": "0.0.0", "private": true }
CONSUMER_JSON
export PHYSICAL_PKG="$PKG"
export PHYSICAL_VER="$VER"
export PHYSICAL_FACTS="$WORK/facts.tsv"
export PHYSICAL_CHECKS="$WORK/checks.jsonl"
: > "$PHYSICAL_FACTS"
: > "$PHYSICAL_CHECKS"

# Report destination: a path when the workflow passes one, otherwise next to the
# workspace so a manual run leaves something behind.
if [ -n "${PHYSICAL_REPORT:-}" ]; then
  case "$PHYSICAL_REPORT" in
    */) REPORT="$PHYSICAL_REPORT/physical-report-$SLUG.json" ;;
    *)
      if [ -d "$PHYSICAL_REPORT" ]; then
        REPORT="$PHYSICAL_REPORT/physical-report-$SLUG.json"
      else
        REPORT="$PHYSICAL_REPORT"
      fi
      ;;
  esac
else
  REPORT="$PWD/physical-report-$SLUG.json"
fi
mkdir -p "$(dirname "$REPORT")"

# ---------------------------------------------------------------------------
# collectors: what the recipe snippets write into, so they never build JSON
# ---------------------------------------------------------------------------

physical_fact() { printf '%s\t%s\n' "$1" "$2" >>"$PHYSICAL_FACTS"; }

physical_check() { # name result detail
   local name="$1" result="$2"
   shift 2
   local detail="$*"
   node -e '
     const fs = require("node:fs");
     const [file, name, result, detail] = process.argv.slice(1);
     fs.appendFileSync(file, JSON.stringify({name, result, detail}) + "\n");
   ' "$PHYSICAL_CHECKS" "$name" "$result" "$detail"
}

# The artifact has to be a RISC-V object; `file` answers that, and when it is not
# installed the ELF header (e_machine 243) answers it without extra tooling. A
# degraded check is still a check - it passes, and says so in the report so the
# environment can be fixed rather than the result being silently weaker.
physical_elf() { # path
  local path="$1" out
  if [ ! -f "$path" ]; then
    physical_check elf fail "no such file: $path"
    return 1
  fi
  if command -v file >/dev/null 2>&1; then
    out="$(file -b "$path")"
    physical_fact elf "$out"
    physical_fact elf_check file
    if printf '%s' "$out" | grep -qi 'RISC-V'; then
      physical_check elf ok "$out"
      return 0
    fi
    physical_check elf fail "$out (expected a RISC-V object)"
    return 1
  fi
  out="$(node -e '
    const fs = require("node:fs");
    const b = fs.readFileSync(process.argv[1]);
    if (b.subarray(0, 4).toString("hex") !== "7f454c46") {
      console.log("not an ELF file");
      process.exit(1);
    }
    const little = b[5] === 1;
    const machine = little ? b.readUInt16LE(18) : b.readUInt16BE(18);
    const type = little ? b.readUInt16LE(16) : b.readUInt16BE(16);
    const names = {243: "RISC-V"};
    console.log(
      `ELF machine=${machine} (${names[machine] || "unknown"}) type=${type} ` +
      `class=${b[4] === 2 ? "ELF64" : "ELF32"}`
    );
    process.exit(machine === 243 ? 0 : 1);
  ' "$path" 2>&1)" || {
    physical_fact elf "$out"
    physical_fact elf_check "elf-header(degraded)"
    physical_check elf fail "$out (expected e_machine 243, RISC-V)"
    return 1
  }
  physical_fact elf "$out"
  physical_fact elf_check "elf-header(degraded)"
  physical_check elf ok "$out (file is not installed: header-only check)"
  log "note: 'file' is missing, ELF was read from the header - install file when convenient"
  return 0
}
# `log` is exported too: the helpers above are exported functions and they call it, so a
# snippet that calls physical_elf from its own shell would otherwise print
# "log: command not found" instead of the note (seen on the first hardware run).
export -f log physical_fact physical_check physical_elf

# ---------------------------------------------------------------------------
# environment gates: a broken machine must not read as a broken artifact
# ---------------------------------------------------------------------------

fail_env() {
  echo "[physical] ERROR: $*" >&2
  physical_check environment fail "$*"
  finish 10
}

finish() { # exit code (default 0)
  local code="${1:-0}"
  local verdict="ok"
  case "$code" in
    0) verdict="ok" ;;
    10) verdict="environment" ;;
    11) verdict="registry" ;;
    12) verdict="digest" ;;
    13) verdict="install" ;;
    14) verdict="execute" ;;
    *) verdict="failed:$code" ;;
  esac
  local ended_at elapsed
  ended_at="$(date +%s)"
  elapsed=$((ended_at - STARTED_AT))
  if PHYSICAL_REPORT_FILE="$REPORT" PHYSICAL_VERDICT="$verdict" \
     PHYSICAL_ELAPSED="$elapsed" PHYSICAL_IDENT="$IDENT" \
     node -e '
       const fs = require("node:fs");
       const env = process.env;
       const facts = {};
       for (const line of fs.readFileSync(env.PHYSICAL_FACTS, "utf8").split("\n")) {
         if (!line) continue;
         const [key, ...rest] = line.split("\t");
         facts[key] = rest.join("\t");
       }
       const checks = fs.readFileSync(env.PHYSICAL_CHECKS, "utf8")
         .split("\n").filter(Boolean).map((line) => JSON.parse(line));
       const report = {
         ident: env.PHYSICAL_IDENT,
         pkg: env.PHYSICAL_PKG,
         ver: env.PHYSICAL_VER,
         verdict: env.PHYSICAL_VERDICT,
         source: env.PHYSICAL_SOURCE,
         registry: env.PHYSICAL_REGISTRY,
         expected_integrity: env.PHYSICAL_EXPECTED_INTEGRITY,
         installed_integrity: facts.installed_integrity || "",
         binding: facts.binding || "",
         elf: facts.elf || "",
         elf_check: facts.elf_check || "",
         host: {
           uname_m: facts.uname_m || "",
           model: facts.model || "",
           isa: facts.isa || "",
           glibc: facts.glibc || "",
           node: facts.node || "",
           npm: facts.npm || "",
         },
         checks,
         duration_s: Number(env.PHYSICAL_ELAPSED),
       };
       fs.writeFileSync(env.PHYSICAL_REPORT_FILE, JSON.stringify(report, null, 2) + "\n");
     '; then
    log "report: $REPORT"
  else
    echo "[physical] ERROR: cannot write the report to $REPORT" >&2
    code=1
  fi
  # A human-readable line for the log and the step summary.
  if [ "$verdict" = "ok" ]; then
    log "verdict=ok $IDENT"
  else
    log "verdict=$verdict $IDENT (see $REPORT)"
  fi
  exit "$code"
}

environment_gate() {
  local arch="${PHYSICAL_ARCH:-$(uname -m)}"
  physical_fact uname_m "$arch"
  if [ "$arch" != "riscv64" ]; then
    if [ "${PHYSICAL_ALLOW_FOREIGN_ARCH:-false}" = "true" ]; then
      log "note: running on '$arch'; PHYSICAL_ALLOW_FOREIGN_ARCH=true allows this for rehearsal"
    else
      fail_env "this runner is '$arch', not riscv64: the workflow asks for the label set [self-hosted, linux, riscv64]"
    fi
  fi

  local node_version
  node_version="$({ node -v 2>/dev/null || true; })"
  [ -n "$node_version" ] || fail_env \
    "node is not installed on this runner. Install node >= 20 once, e.g.:" \
    "'sudo dnf install -y nodejs npm' or 'sudo apt-get install -y nodejs npm'"
  local major="${node_version#v}"
  major="${major%%.*}"
  case "$major" in
    '' | *[!0-9]*) fail_env "cannot read the node version from '$node_version'" ;;
  esac
  if [ "$major" -lt 20 ]; then
    fail_env "node $node_version is too old: the hardware run requires >= 20, because a green" \
      "light on an older runtime is not the green light a user gets. Upgrade node once on this host"
  fi
  local npm_version
  npm_version="$(npm -v 2>/dev/null || true)"

  physical_fact node "$node_version"
  physical_fact npm "$npm_version"

  # /proc/cpuinfo is read with awk and an explicit `exit`: `sed ... | head -1` closes
  # the pipe while sed still writes, and under `pipefail` that turns the whole step into
  # SIGPIPE (141) - which would be reported as a broken artifact rather than a broken
  # idiom. Different architectures name the fields differently, so both spellings are
  # tried; riscv64 has `isa`, x86 has `model name`.
  local model isa
  if [ -r /proc/cpuinfo ]; then
    isa="$(cpuinfo_field 'isa')"
  fi
  # The board, when the kernel exposes one: `/proc/device-tree/model` is what real RISC-V
  # boards carry ("SpacemiT K1 ...", "Sophgo ..."), and it is readable even where
  # /proc/cpuinfo is not (containers usually hide the latter, not the former). The
  # cpuinfo spellings are tried in order because kernels disagree: `model name` on x86,
  # `uarch`/`model` on several RISC-V kernels, `CPU`/`Processor` on ARM.
  if [ -r /proc/device-tree/model ]; then
    model="$(tr -d '\000' < /proc/device-tree/model)"
  fi
  if [ -z "$model" ] && [ -r /proc/cpuinfo ]; then
    for key in 'model name' uarch model CPU Processor; do
      model="$(cpuinfo_field "$key")"
      [ -n "$model" ] && break
    done
  fi
  physical_fact model "${model:-unknown}"
  physical_fact isa "${isa:-unknown}"

  local glibc=""
  glibc="$({ getconf GNU_LIBC_VERSION 2>/dev/null || true; })"
  physical_fact glibc "${glibc:-unknown}"

  local free_mb=""
  if command -v df >/dev/null 2>&1; then
    free_mb="$(df -Pk "$WORK" | awk 'NR == 2 { print int($4 / 1024) }')"
  fi
  if [ -n "$free_mb" ] && [ "$free_mb" -lt "$MIN_FREE_MB" ]; then
    fail_env "only ${free_mb} MB free under $WORK; a hardware run needs ${MIN_FREE_MB} MB" \
      "(PHYSICAL_MIN_FREE_MB overrides this floor)"
  fi
  if ! command -v flock >/dev/null 2>&1; then
    log "note: flock is not installed; the workflow's concurrency group is the only lock"
  else
    exec 9>"${PHYSICAL_LOCK:-${RUNNER_TEMP:-/tmp}/physical-verify.lock}"
    if ! flock -n 9; then
      fail_env "another hardware run holds $PHYSICAL_LOCK"
    fi
  fi
}

# The run may verify only what this build published. A workflow passes the merged
# publish report; a manual run may pass none, in which case the package is verified as
# asked.

main() {
  if [ -n "${PHYSICAL_PLAN:-}" ]; then
    if [ ! -f "$PHYSICAL_PLAN" ]; then
      fail_env "PHYSICAL_PLAN is set but missing: $PHYSICAL_PLAN"
    fi
    local planned
    planned="$(node -e '
      const fs = require("node:fs");
      const plan = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
      const wanted = process.argv[2];
      const entry = plan[wanted];
      process.stdout.write(entry ? "yes" : "no");
    ' "$PHYSICAL_PLAN" "$PKG")"
    if [ "$planned" != "yes" ]; then
      log "skipped: $PKG is not in this run's publish report"
      exit 0
    fi
    # The report is the source of both the digest and the fallback decision.
    if [ -z "$PHYSICAL_EXPECTED_INTEGRITY" ]; then
      export PHYSICAL_EXPECTED_INTEGRITY
      PHYSICAL_EXPECTED_INTEGRITY="$(node -e '
        const fs = require("node:fs");
        const plan = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
        const entry = plan[process.argv[2]] || {};
        process.stdout.write(String(entry.integrity || ""));
      ' "$PHYSICAL_PLAN" "$PKG")"
    fi
    if [ -z "${PHYSICAL_FALLBACK:-}" ]; then
      export PHYSICAL_FALLBACK
      PHYSICAL_FALLBACK="$(node -e '
        const fs = require("node:fs");
        const plan = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
        const entry = plan[process.argv[2]] || {};
        process.stdout.write(entry.served === "unverified" ? "true" : "false");
      ' "$PHYSICAL_PLAN" "$PKG")"
    fi
    # The tarball the build kept is named in the report; where it was downloaded to is
    # the workflow's business (PHYSICAL_TARBALL_DIR), so the two are joined here.
    if [ "$PHYSICAL_FALLBACK" = "true" ] && [ -z "$PHYSICAL_TARBALL" ] &&
       [ -n "${PHYSICAL_TARBALL_DIR:-}" ]; then
      local planned_tarball
      planned_tarball="$(node -e '
        const fs = require("node:fs");
        const plan = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
        const entry = plan[process.argv[2]] || {};
        process.stdout.write(String(entry.tarball || ""));
      ' "$PHYSICAL_PLAN" "$PKG")"
      if [ -n "$planned_tarball" ]; then
        PHYSICAL_TARBALL="$PHYSICAL_TARBALL_DIR/$(basename "$planned_tarball")"
        export PHYSICAL_TARBALL
        log "fallback tarball from the build: $PHYSICAL_TARBALL"
      fi
    fi
  fi

  environment_gate
  physical_fact source "$PHYSICAL_SOURCE"
  physical_fact registry "$PHYSICAL_REGISTRY"

@@AUTH@@

  local code=0
  # Snippets may be overridden by the recipe (commands/install, commands/verify); the
  # generated ones are the default. They run in separate shells on purpose: a snippet
  # that fails must not leave the wrapper's own state half-changed.
  log "install: $PKG@$VER from $PHYSICAL_REGISTRY (source=$PHYSICAL_SOURCE)"
  bash "$SCRIPT_DIR/install.sh" || code=$?
  if [ "$code" -ne 0 ]; then
    echo "[physical] install step failed with code $code" >&2
    finish "$code"
  fi

  if [ -f "$WORK/tgz.path" ]; then
    export PHYSICAL_TGZ
    PHYSICAL_TGZ="$(cat "$WORK/tgz.path")"
  fi

  log "verify: $PKG@$VER"
  code=0
  bash "$SCRIPT_DIR/verify.sh" || code=$?
  if [ "$code" -ne 0 ]; then
    echo "[physical] verify step failed with code $code" >&2
    [ "$code" = "10" ] || code=14
    finish "$code"
  fi

  finish 0
}

main "$@"
