#!/usr/bin/env python3
"""Run a rendered composite action on this machine (L1 verification).

The point is to execute exactly what the renderer produced - not a hand written
equivalent - so indentation bugs, quoting bugs and generated snippets are all
exercised. Steps run with ``GITHUB_WORKSPACE`` pointing at a sandbox directory
and ``/w`` bind-mounted from it, mirroring the CI layout.

Usage::

    python3 tests/run_action_locally.py --pkg bufferutil --ver 4.2.0
    python3 tests/run_action_locally.py --pkg ... --ver ... --workspace .dsh/tmp/l1

The publish step is skipped unless ``--publish`` is given, mirroring the
``inputs.publish`` gate used in CI.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
STEPS_DIR = ".rendered/action"


def evaluate_if(condition: str | None, **inputs: bool) -> bool:
    """Evaluate the small subset of `if:` expressions the template uses.

    Supported: a single ``inputs.<name> (==|!=) 'true'`` clause, or an ``&&`` chain of
    them - which is what the template is allowed to contain, so a more elaborate
    expression is a template bug worth failing on rather than guessing at.
    """
    if not condition:
        return True
    text = condition.strip()
    for clause in (part.strip() for part in text.split("&&")):
        if not clause:
            continue
        matched = False
        for name, value in inputs.items():
            for quote in ("'", '"'):
                if clause == f"inputs.{name} == {quote}true{quote}":
                    matched = True
                    if not value:
                        return False
                elif clause == f"inputs.{name} != {quote}true{quote}":
                    matched = True
                    if value:
                        return False
        if not matched:
            raise SystemExit(f"unsupported if condition in the action template: {clause}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--ver", required=True)
    parser.add_argument("--root", default="packages")
    parser.add_argument(
        "--template", default=".github/templates/action.template.yml"
    )
    parser.add_argument("--workspace", help="sandbox directory (default: .dsh/tmp/l1/<slug>)")
    parser.add_argument("--only", help="comma separated step names to run (substring match)")
    parser.add_argument("--skip", help="comma separated step names to skip (substring match)")
    parser.add_argument("--publish", action="store_true", help="also run the publish step")
    parser.add_argument(
        "--publish-only",
        action="store_true",
        help="behave like a cached re-delivery: skip fetch/patch/build/test, publish only",
    )
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="pretend the tree came from the build cache (skip fetch, patch and build)",
    )
    parser.add_argument(
        "--image",
        default=os.environ.get("RISCV_IMAGE", "npm-riscv64/toolchain:riscv64"),
        help="container image the build steps run in (the cross image for a cross recipe)",
    )
    parser.add_argument(
        "--test-image",
        default=os.environ.get("RISCV_TEST_IMAGE", ""),
        help="container image the test step runs in (defaults to the build image)",
    )
    parser.add_argument(
        "--auth-kind",
        help="override npm_publish[].auth for every publish entry (token | basic); "
        "a local registry usually wants a bearer token even when the real target "
        "takes basic credentials",
    )
    parser.add_argument("--clean", action="store_true", help="wipe the sandbox first")
    args = parser.parse_args(argv)

    slug = args.pkg.lstrip("@").replace("/", "-") + "-" + args.ver
    workspace = Path(args.workspace) if args.workspace else REPO_ROOT / ".dsh/tmp/l1" / slug
    if args.clean and workspace.exists():
        shutil.rmtree(workspace)
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    registry = os.environ.get("NPM_REGISTRY")
    render_cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts/render.py"),
        "--pkg",
        args.pkg,
        "--ver",
        args.ver,
        "--root",
        str((REPO_ROOT / args.root).resolve()),
        "--template",
        str((REPO_ROOT / args.template).resolve()),
        "--out-dir",
        STEPS_DIR,
    ]
    if registry or args.auth_kind:
        # --override takes one field per flag, so every publish entry is patched.
        recipe_stat = (REPO_ROOT / args.root).joinpath(*args.pkg.split("/"), args.ver, ".stat")
        stat = yaml.safe_load(recipe_stat.read_text(encoding="utf-8")) or {}
        for index in range(len(stat.get("npm_publish") or [])):
            if registry:
                render_cmd.append(f"--override=npm_publish.{index}.registry={registry}")
            if args.auth_kind:
                render_cmd.append(f"--override=npm_publish.{index}.auth={args.auth_kind}")

    # Render from inside the sandbox so `steps.dir` stays workspace-relative and
    # the action can resolve /w/.rendered/action inside the container.
    result = subprocess.run(render_cmd, cwd=workspace, capture_output=True, text=True)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return result.returncode

    # The action exports RECIPE_DIR as /w/<recipe dir> and the test step reads its
    # skip list, vitest config and manifest templates from there (they travel with the
    # recipe). In CI /w is the checkout, so the recipe exists at that path; the sandbox
    # only holds the build tree, so the recipe is copied in.
    recipe_src = (REPO_ROOT / args.root).joinpath(*args.pkg.split("/"), args.ver)
    recipe_dst = workspace / "packages" / pathlib.Path(*args.pkg.split("/")) / args.ver
    if recipe_dst.exists():
        shutil.rmtree(recipe_dst)
    recipe_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(recipe_src, recipe_dst)

    action_file = workspace / STEPS_DIR / "action.yml"
    action = yaml.safe_load(action_file.read_text(encoding="utf-8"))
    steps = action["runs"]["steps"]

    only = [s.strip() for s in (args.only or "").split(",") if s.strip()]
    skip = [s.strip() for s in (args.skip or "").split(",") if s.strip()]

    env = dict(os.environ)
    env.update(
        GITHUB_WORKSPACE=str(workspace),
        RISCV_IMAGE=args.image,
        RISCV_TEST_IMAGE=args.test_image or args.image,
        PYTHONUNBUFFERED="1",
        # Keep npm caches inside the sandbox: the host home may be read-only.
        npm_config_cache=str(workspace / ".npm-cache"),
    )

    print(f"workspace: {workspace}")
    print(f"image    : {args.image}")
    print(f"test img : {env['RISCV_TEST_IMAGE']}")
    failures = 0
    for step in steps:
        name = str(step.get("name", "?"))
        if "run" not in step:
            print(f"\n=== skipping non-run step: {name}")
            continue
        if not evaluate_if(
            step.get("if"),
            publish=args.publish,
            reuse=args.reuse,
            publish_only=args.publish_only,
        ):
            print(f"\n=== skipped by if-condition: {name}")
            continue
        if only and not any(token in name for token in only):
            continue
        if skip and any(token in name for token in skip):
            print(f"\n=== skipped by request: {name}")
            continue

        print(f"\n=== step: {name}")
        completed = subprocess.run(
            ["bash", "-c", str(step["run"])], cwd=workspace, env=env, text=True
        )
        if completed.returncode != 0:
            failures += 1
            print(f"!!! step failed: {name} (exit {completed.returncode})", file=sys.stderr)
            break

    if failures:
        print(f"\nL1 result: FAILED ({failures} step(s))", file=sys.stderr)
        return 1
    print("\nL1 result: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
