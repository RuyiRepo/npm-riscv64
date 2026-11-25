#!/usr/bin/env python3
"""Regenerate the golden rendered action (L0 snapshot fixtures).

The golden files are the review surface for rendering changes: if a template edit
or a renderer change alters the generated action, the diff shows up in review
instead of silently shipping. Regenerate deliberately, then read the diff:

    python3 tests/regen_golden.py && git diff -- tests/golden
    python3 tests/regen_golden.py rolldown 1.3.3   # one recipe only
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = REPO_ROOT / "tests/golden"
# One rendered action per shape the renderer has to produce: a single prebuild package
# and a two-package wrapper/binding pair.
CASES = [("bufferutil", "4.2.0"), ("rolldown", "1.3.3")]
STEPS_DIR = ".rendered/action"


def render_into(pkg: str, ver: str, target: Path) -> None:
    """Render one recipe with paths that do not depend on the output directory."""
    if target.exists():
        shutil.rmtree(target)
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/render.py"),
            "--pkg",
            pkg,
            "--ver",
            ver,
            "--out-dir",
            str(target),
            "--steps-dir",
            STEPS_DIR,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"render failed:\n{result.stdout}\n{result.stderr}")


def main(argv: list[str]) -> int:
    cases = CASES
    if len(argv) == 2:
        cases = [(argv[0], argv[1])]
    elif len(argv) == 1:
        # A package alone means "its only version"; a version alone is ambiguous.
        cases = [case for case in CASES if case[0] == argv[0]]
        if not cases:
            raise SystemExit(f"unknown package {argv[0]}; known: {[c[0] for c in CASES]}")
    for pkg, ver in cases:
        golden = GOLDEN_ROOT / f"{pkg}-{ver}"
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "action"
            render_into(pkg, ver, staging)
            if golden.exists():
                shutil.rmtree(golden)
            golden.mkdir(parents=True)
            shutil.copy2(staging / "action.yml", golden / "action.yml")
            shutil.copytree(staging / "steps", golden / "steps")
        print(f"golden updated: {golden.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
