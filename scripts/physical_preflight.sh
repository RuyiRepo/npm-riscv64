#!/usr/bin/env bash
# Reports whether this machine can run the hardware verification, and says exactly what
# to install when it cannot (.dsh/plan.md D41).
#
# The hardware run installs and verifies a *published* package on the machine a user
# would use, so the machine has to look like one: the runtime a consumer has (node >=
# 20), npm to resolve with, and enough of coreutils that a failure cannot be blamed on
# the machine. The tool set is deliberately small, and every tool in it is one package
# to install - a bigger set would be one more thing to get wrong on the runner.
#
# Two rules shape the output:
#
#   * it never fails the job. A missing tool is reported as an environment fact and the
#     verification still runs: a run that stopped here would produce no report at all,
#     and "this machine is not ready" would read as "nothing was verified".
#   * the install commands are printed for the distribution actually in front of you,
#     with the no-sudo alternative spelled out, because the runner is a long-lived
#     container somebody else also uses.
#
# Usage: bash scripts/physical_preflight.sh [--summary <file>]
set -euo pipefail

SUMMARY=""
if [ "${1:-}" = "--summary" ]; then
  SUMMARY="${2:?--summary needs a path}"
fi

# ---------------------------------------------------------------------------
# which distribution is this, and which package manager names its packages
# ---------------------------------------------------------------------------

# `uname` may be absent on a machine this broken; reporting "unknown" is the point of the
# script, so nothing here may die on the way to the report.
distro="$(uname -srm 2>/dev/null || echo unknown)"
id=""
id_like=""
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  distro="${PRETTY_NAME:-${NAME:-$distro}}"
  id="${ID:-}"
  id_like="${ID_LIKE:-}"
fi

pkg_mgr=""
case "$id $id_like" in
  *debian* | *ubuntu*) pkg_mgr="apt" ;;
  *rhel* | *fedora* | *centos* | *amzn*) pkg_mgr="dnf" ;;
  *alpine*) pkg_mgr="apk" ;;
  *suse* | *sles*) pkg_mgr="zypper" ;;
  *arch*) pkg_mgr="pacman" ;;
esac
if [ -z "$pkg_mgr" ]; then
  # Fall back to whatever exists rather than guessing from a name.
  for candidate in apt-get dnf apk zypper pacman; do
    if command -v "$candidate" >/dev/null 2>&1; then
      case "$candidate" in
        apt-get) pkg_mgr="apt" ;;
        dnf) pkg_mgr="dnf" ;;
        *) pkg_mgr="$candidate" ;;
      esac
      break
    fi
  done
fi

machine_arch="$(uname -m 2>/dev/null || echo unknown)"
echo "[preflight] machine: $distro ($machine_arch)"
echo "[preflight] package manager: ${pkg_mgr:-none detected}"

# ---------------------------------------------------------------------------
# the tool set
# ---------------------------------------------------------------------------
#
# required     the run cannot work without it
# recommended  the run works without it and says so in the report
#
# `file` is recommended, not required: without it the ELF check reads the header
# (e_machine 243) instead of the full description, which still answers the question the
# check exists for, and the report marks it elf-header(degraded) so a weaker answer is
# never silently green.
# `flock` is recommended: the workflow's concurrency group is the real lock; flock adds
# a second one for runs started outside it.
# `git` is only the workflow's own checkout, not the verification.

missing_required=()
missing_recommended=()
required_ok=true
arch_ok=true
node_major=0

fail_required() { missing_required+=("$1"); required_ok=false; }

check_path() { # name kind
  local name="$1" kind="$2" path
  if path="$(command -v "$name" 2>/dev/null)" && [ -n "$path" ]; then
    echo "[preflight] $kind $name: $path"
    return 0
  fi
  echo "[preflight] $kind $name: MISSING"
  if [ "$kind" = "required" ]; then
    fail_required "$name"
  else
    missing_recommended+=("$name")
  fi
  return 1
}

check_version() { # name kind version-flag
  local name="$1" kind="$2" flag="$3" path version
  if ! path="$(command -v "$name" 2>/dev/null)" || [ -z "$path" ]; then
    echo "[preflight] $kind $name: MISSING"
    if [ "$kind" = "required" ]; then
      fail_required "$name"
    else
      missing_recommended+=("$name")
    fi
    return 1
  fi
  version="$({ "$name" "$flag" 2>&1 || true; } | head -1 || true)"
  echo "[preflight] $kind $name: $path ($version)"
  return 0
}

arch="$machine_arch"
if [ "$arch" = "riscv64" ]; then
  echo "[preflight] required uname: $arch"
else
  echo "[preflight] required uname: $arch - NOT riscv64"
  arch_ok=false
  required_ok=false
fi

# bash >= 4: the wrapper uses arrays and exported functions, and mktemp's template form
# needs a real bash; a mismatch here is worth naming rather than guessing at later.
if [ "${BASH_VERSINFO[0]:-0}" -ge 4 ]; then
  echo "[preflight] required bash: ${BASH_VERSION:-unknown} ($(command -v bash))"
else
  echo "[preflight] required bash: ${BASH_VERSION:-unknown} is too old (need >= 4.0)"
  fail_required bash
fi

# Each probe returns non-zero when the tool is absent, which is a fact to report, not an
# error: `|| true` keeps `set -e` from ending the report at the first gap.
check_version node required -v || true
check_version npm required -v || true
check_path awk required || true
for tool in mktemp df getconf date grep sed tar; do
  check_path "$tool" required >/dev/null || true
done
echo "[preflight] required coreutils: $(for t in mktemp df getconf date grep sed tar; do
  if command -v "$t" >/dev/null 2>&1; then printf '%s ' "$t"; else printf '%s(MISSING) ' "$t"; fi
done)"

# node >= 20 is a hard gate: a green light on an older runtime is not the green light a
# user gets, so an old node is reported as required-and-wrong, not as a warning.
node_version="$(node -v 2>/dev/null || true)"
if [ -n "$node_version" ]; then
  node_major="${node_version#v}"
  node_major="${node_major%%.*}"
  if [ "${node_major:-0}" -lt 20 ] 2>/dev/null; then
    echo "[preflight] required node: $node_version is too old (need >= 20)"
    fail_required "node>=20"
  fi
fi

check_version file recommended --version || true
check_version flock recommended --version || true
check_version git recommended --version || true

# ---------------------------------------------------------------------------
# what to install
# ---------------------------------------------------------------------------

pkg_for() { # tool -> package name in this distribution's vocabulary
  case "$1" in
    "node>=20" | npm | node) echo "nodejs" ;;
    mktemp | df | date | coreutils) echo "coreutils" ;;
    getconf) echo "libc-bin" ;;
    awk) echo "gawk" ;;
    flock) echo "util-linux" ;;
    *) echo "$1" ;;
  esac
}

install_line() { # package
  case "$pkg_mgr" in
    apt) echo "sudo apt-get install -y $1" ;;
    dnf) echo "sudo dnf install -y $1" ;;
    apk) echo "sudo apk add --update $1" ;;
    zypper) echo "sudo zypper install -y $1" ;;
    pacman) echo "sudo pacman -S --noconfirm $1" ;;
    *) echo "# install $1 with the package manager of this machine" ;;
  esac
}

if [ "${#missing_recommended[@]}" -gt 0 ]; then
  echo ""
  echo "[preflight] recommended, missing: ${missing_recommended[*]}"
  for tool in "${missing_recommended[@]}"; do
    case "$tool" in
      file) echo "[preflight]   $(install_line file)            # full ELF description in the report" ;;
      flock) echo "[preflight]   $(install_line util-linux)      # a second lock beside the concurrency group" ;;
      git) echo "[preflight]   $(install_line git)             # only the workflow's checkout needs it" ;;
      *) echo "[preflight]   $(install_line "$(pkg_for "$tool")")" ;;
    esac
  done
  echo "[preflight]   no sudo? ask the container owner to install those once - nothing at"
  echo "[preflight]   run time needs root - or unpack a static build into a directory already"
  echo "[preflight]   on PATH (file(1) is available from ftp://ftp.astron.com/pub/file/)."
fi

if [ "$required_ok" = false ]; then
  echo ""
  if [ "$arch_ok" = false ]; then
    echo "[preflight] readiness: WRONG MACHINE - '$arch' is not riscv64. The label set"
    echo "[preflight]            [self-hosted, linux, riscv64] is honest only where uname"
    echo "[preflight]            reports riscv64; this run would report environment."
  else
    echo "[preflight] readiness: MISSING - absent: ${missing_required[*]}"
    echo "[preflight]            the run would report an environment verdict."
  fi
  if [ "${node_major:-0}" -lt 20 ] 2>/dev/null || ! command -v node >/dev/null 2>&1; then
    echo "[preflight] install node >= 20 once (a consumer's runtime, not the distro default):"
    case "$pkg_mgr" in
      apt)
        echo "[preflight]   curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
        echo "[preflight]   sudo apt-get install -y nodejs"
        ;;
      dnf)
        echo "[preflight]   curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo -E bash -"
        echo "[preflight]   sudo dnf install -y nodejs"
        ;;
      apk)
        echo "[preflight]   sudo apk add --update nodejs npm"
        ;;
      *)
        echo "[preflight]   install nodesource's 20.x package, or unpack an official tarball"
        echo "[preflight]   from https://nodejs.org/dist/ into a directory already on PATH."
        ;;
    esac
  fi
  # One line per package, whatever number of tools it brings in, and nothing for node
  # or npm - the block above already installed both.
  printed=""
  for tool in "${missing_required[@]}"; do
    case "$tool" in
      "node>=20" | node | npm | bash | uname) continue ;;
    esac
    line="$(install_line "$(pkg_for "$tool")")"
    case " $printed " in
      *" $line "*) continue ;;
    esac
    printed="$printed $line"
    echo "[preflight]   $line"
  done
  readiness="missing"
elif [ "${#missing_recommended[@]}" -gt 0 ]; then
  echo ""
  echo "[preflight] readiness: degradable - the run works, and each weaker check is named"
  echo "[preflight]            in the report; installing the packages above makes it full."
  readiness="degraded"
else
  echo ""
  echo "[preflight] readiness: ok"
  readiness="ok"
fi

if [ -n "$SUMMARY" ]; then
  {
    echo ""
    echo "| runner check | result |"
    echo "| --- | --- |"
    echo "| machine | \`$distro\` |"
    echo "| node | \`${node_version:-missing}\` |"
    echo "| npm | \`$(npm -v 2>/dev/null || echo missing)\` |"
    echo "| bash | \`${BASH_VERSION:-unknown}\` |"
    if [ "${#missing_recommended[@]}" -gt 0 ]; then
      echo "| recommended missing | \`${missing_recommended[*]}\` |"
    else
      echo "| recommended missing | none |"
    fi
    echo "| readiness | \`$readiness\` |"
  } >>"$SUMMARY"
fi

# Always zero: see the two rules at the top. Whether the machine was ready is a fact in
# the report, not a reason to stop producing one.
exit 0
