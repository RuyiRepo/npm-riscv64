#!/usr/bin/env python3
"""Checks the upstream increments a recipe carries.

Two kinds of patch live in a recipe and they must never be confused
(.dsh/plan.md D34):

* ``patches/`` changes the source so it builds and tests on riscv64. The composite
  action applies it, and it is part of the build key.
* ``upstream/`` is a patch for an upstream pull request. It is never applied here;
  it exists so the riscv64 increment can be submitted as it is, and so reviewers can
  see exactly what upstream would have to accept.

This module holds ``upstream/`` to that promise: a real unified diff against
upstream's own files, carrying the PR description, mentioning nothing that belongs
to this repository, and (when a network is available) applying cleanly to the
source tree the recipe pins.

Usage::

    check_patches.py --root packages/            # offline checks only
    check_patches.py --root packages/ --network  # also git apply --check upstream
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import recipe_lib as rl  # noqa: E402

#: The PR description a patch has to carry; a patch without it cannot be submitted.
HEADER_FIELDS = ("Upstream-PR", "Upstream-Repo", "Upstream-Base", "Upstream-Files")

#: Strings that only exist in this repository. A patch naming any of them has picked
#: up something from our side and is not an upstream increment any more.
LOCAL_MARKERS = (
    "forgestash",
    "oepkgs",
    "npm-rv-test",
    "dsh-",
    ".dsh/",
    "scripts/check_patches.py",
    "scripts/render.py",
    "recipes",
    "riscv64-cross.Dockerfile",
    "riscv64-toolchain.Dockerfile",
)

DIFF_HEADER = re.compile(r"^diff --git a/(?P<old>[^\s]+) b/(?P<new>[^\s]+)$")
PLUS_FILE = re.compile(r"^\+\+\+ b/(?P<path>[^\s]+)$")
MINUS_FILE = re.compile(r"^--- a/(?P<path>[^\s]+)$")


def parse_patch(text: str) -> tuple[dict[str, str], list[str], list[str]]:
    """Split a patch into its header fields, the diff body and the files it touches."""
    header: dict[str, str] = {}
    body: list[str] = []
    started = False
    for line in text.splitlines():
        match = re.match(r"^(Upstream-[A-Za-z-]+):\s*(.*)$", line)
        if match and not started:
            header[match.group(1)] = match.group(2)
            continue
        if line.startswith("diff --git"):
            started = True
        if started:
            body.append(line)
    files = [match.group("path") for line in body if (match := PLUS_FILE.match(line))]
    return header, body, files


def check_header(where: str, header: dict[str, str]) -> list[str]:
    problems = []
    for field in HEADER_FIELDS:
        if not header.get(field):
            problems.append(f"{where}: missing the '{field}:' line of the PR description")
    return problems


def check_shape(where: str, body: list[str], files: list[str]) -> list[str]:
    problems: list[str] = []
    if not body:
        problems.append(f"{where}: no unified diff found")
        return problems
    if not files:
        problems.append(f"{where}: no '+++ b/<path>' line; the diff has no target file")
    if any(line.startswith("--- /dev/null") for line in body):
        problems.append(
            f"{where}: the increment creates a file; adding upstream files is not what "
            "a riscv64 increment does"
        )
    if any(line.startswith("+++ /dev/null") for line in body):
        problems.append(f"{where}: the increment deletes an upstream file")
    if any(re.match(r"^[-+]{3} (?!/dev/null|[ab]/)", line) for line in body):
        problems.append(f"{where}: a path lacks the a/ or b/ prefix (is it a real git diff?)")
    for line in body:
        if DIFF_HEADER.match(line):
            continue
        if line.startswith("diff --git") and not DIFF_HEADER.match(line):
            problems.append(f"{where}: malformed diff header: {line}")
    return problems


def check_paths(where: str, files: list[str], touches: list[str]) -> list[str]:
    problems: list[str] = []
    declared = {str(item) for item in touches}
    for path in files:
        if path not in declared:
            problems.append(
                f"{where}: the diff touches '{path}', which is not listed in "
                f"upstream_increments.touches ({', '.join(sorted(declared)) or 'empty'})"
            )
    for path in sorted(declared - set(files)):
        problems.append(
            f"{where}: touches names '{path}', but the diff does not change it"
        )
    return problems


def check_no_local_leak(where: str, body: list[str], registry_hosts: set[str]) -> list[str]:
    problems: list[str] = []
    corpus = "\n".join(line for line in body if line.startswith(("+", "-")))
    for marker in LOCAL_MARKERS:
        if marker in corpus:
            problems.append(
                f"{where}: the added lines mention '{marker}', which belongs to this "
                "repository, not to an upstream patch"
            )
    for host in sorted(registry_hosts):
        if host and host in corpus:
            problems.append(f"{where}: the added lines mention the registry host '{host}'")
    return problems


def check_applies(where: str, text: str, repo: str, ref: str) -> list[str]:
    """Fetch the pinned ref and assert the patch applies to it (needs a network)."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "upstream"
            result = subprocess.run(
                ["git", "clone", "--quiet", "--depth", "1", "--branch", ref, repo, str(clone)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return [f"{where}: cannot clone {repo} at {ref}: {result.stderr.strip()}"]
            patch = Path(tmp) / "increment.patch"
            patch.write_text(text, encoding="utf-8")
            result = subprocess.run(
                ["git", "apply", "--check", str(patch)],
                cwd=clone,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return [
                    f"{where}: the patch does not apply to {repo} at {ref}: "
                    f"{result.stderr.strip()}"
                ]
    except OSError as exc:  # git missing, or a sandbox without a network
        return [f"{where}: cannot run git: {exc}"]
    return []


def check_naming_equivalence(target: rl.Target, stat: dict, increments_text: str) -> list[str]:
    """A riscv64 binding package must match the napi target the patch adds.

    napi-rs names a binding package after its target triple:
    ``riscv64gc-unknown-linux-gnu`` -> ``@<scope>/binding-linux-riscv64-gnu``. If a
    recipe publishes such a package and the increment adds this target, both sides
    have to agree, otherwise the published dependency would never be found.
    """
    problems: list[str] = []
    # napi names the package after the target triple (``...-linux-riscv64-gnu``),
    # while npm's libc field says "glibc": one is the toolchain ABI name, the other
    # the C library family, and they have to be mapped or this check lies.
    libc_suffix = {"glibc": "gnu", "musl": "musl"}
    bindings = [
        entry
        for entry in stat.get("npm_publish") or []
        if isinstance(entry, dict) and entry.get("role") == "binding"
    ]
    if not bindings:
        return problems
    for entry in bindings:
        name = str(entry.get("name") or "")
        libc = str(entry.get("libc") or "")
        cpu = str(entry.get("cpu") or "")
        if not cpu or not libc:
            problems.append(
                f"{target.ident}: binding '{name}' needs cpu and libc so its name can be "
                "checked against the napi target"
            )
            continue
        suffix = libc_suffix.get(libc, libc)
        expected = f"-{cpu}-{suffix}"
        if not name.endswith(expected):
            problems.append(
                f"{target.ident}: binding '{name}' does not end with '{expected}'; napi "
                "names platform packages after the target triple"
            )
        if "riscv64gc-unknown-linux-" not in increments_text:
            problems.append(
                f"{target.ident}: the increment does not add a riscv64gc-unknown-linux "
                "target, so upstream would never build the package this recipe publishes"
            )
    return problems


def check_target(target: rl.Target, stat: dict, *, network: bool = False) -> list[str]:
    problems: list[str] = []
    increments = stat.get("upstream_increments") or []
    if not increments:
        return problems

    upstream = stat.get("upstream") or {}
    registry_hosts = set()
    for entry in stat.get("npm_publish") or []:
        if isinstance(entry, dict):
            registry = str(entry.get("registry") or "")
            if registry and "registry.npmjs.org" not in registry:
                host = re.sub(r"^[a-z]+://", "", registry).split("/")[0]
                if host:
                    registry_hosts.add(host)

    all_text = ""
    for index, item in enumerate(increments):
        if not isinstance(item, dict):
            continue
        where = f"{target.ident} upstream_increments[{index}] ({item.get('patch')})"
        path = target.path / str(item.get("patch") or "")
        if not path.is_file():
            problems.append(f"{where}: missing patch file")
            continue
        text = path.read_text(encoding="utf-8")
        all_text += text
        header, body, files = parse_patch(text)
        problems.extend(check_header(where, header))
        problems.extend(check_shape(where, body, files))
        problems.extend(check_paths(where, files, item.get("touches") or []))
        problems.extend(check_no_local_leak(where, body, registry_hosts))
        if network:
            repo = str(upstream.get("repo") or "")
            ref = str(upstream.get("ref") or "")
            if repo and ref:
                problems.extend(check_applies(where, text, repo, ref))

    problems.extend(check_naming_equivalence(target, stat, all_text))
    return problems


def check_all(root: Path, *, network: bool = False) -> tuple[int, list[str]]:
    problems: list[str] = []
    targets = rl.enumerate_targets(root)
    checked = 0
    for target in targets:
        stat = rl.load_stat(target)
        if stat.get("upstream_increments"):
            checked += 1
        problems.extend(check_target(target, stat, network=network))
    return checked, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a recipe's upstream increments")
    parser.add_argument("--root", default="packages", help="recipes root (default: packages)")
    parser.add_argument(
        "--network",
        action="store_true",
        help="also clone the pinned upstream ref and run git apply --check",
    )
    args = parser.parse_args(argv)

    checked, problems = check_all(Path(args.root), network=args.network)
    for problem in problems:
        print(f"[FAIL] {problem}")
    print(f"checked {checked} recipe(s) with upstream increments, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
