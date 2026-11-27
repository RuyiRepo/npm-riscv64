#!/usr/bin/env python3
"""Rehearse the hardware verification without hardware (L2).

Everything the hardware run does except running on real silicon can be rehearsed here:
publish the recipe to a registry, render the hardware action, and run its
``steps/physical.sh`` inside the riscv64 image against that registry. The wrapper is
executed exactly as the hardware runner would execute it, except that the silicon is
emulated - which is what makes a bad recipe discoverable before it costs hardware time.

Usage::

    python3 tests/rehearse_physical.py --pkg bufferutil --ver 4.2.0 \\
        --registry http://127.0.0.1:4890 --token-file .dsh/tmp/l2-real.token

The tree to publish comes from ``--tree`` (default ``.dsh/tmp/l1/<slug>/src``, where
``tests/run_action_locally.py`` leaves its build). ``--no-publish`` verifies a package
that is already in the registry; ``--keep`` leaves the sandbox in place.

What this cannot show: node >= 20 availability, `file`/`flock` presence, the real CPU
model and ISA string, and whether the box can reach the registry at all. Those are the
first things a real run reports as an `environment` verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def slug_for(pkg: str, ver: str) -> str:
    return pkg.lstrip("@").replace("/", "-") + "-" + ver


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+ " + " ".join(argv), flush=True)
    return subprocess.run(argv, **kwargs)


def render(pkg: str, ver: str, out_dir: Path, template: str, extra: list[str]) -> None:
    argv = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "render.py"),
        "--pkg", pkg,
        "--ver", ver,
        "--template", template,
        "--out-dir", str(out_dir),
        *extra,
    ]
    result = run(argv, capture_output=True, text=True)
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit("render failed: %s" % template)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--ver", required=True)
    parser.add_argument("--root", default="packages")
    parser.add_argument("--tree", help="built tree to publish (default: .dsh/tmp/l1/<slug>/src)")
    parser.add_argument("--registry", required=True, help="registry to publish to and read back")
    parser.add_argument("--token-env-file", help="file holding the registry credential")
    parser.add_argument("--image", default=os.environ.get("RISCV_IMAGE", "npm-riscv64/toolchain:riscv64"))
    parser.add_argument("--workspace", help="sandbox directory (default: .dsh/tmp/physical/<slug>)")
    parser.add_argument("--no-publish", action="store_true", help="verify a package already published")
    parser.add_argument("--keep", action="store_true", help="keep the sandbox for inspection")
    args = parser.parse_args(argv)

    slug = slug_for(args.pkg, args.ver)
    workspace = Path(args.workspace) if args.workspace else REPO_ROOT / ".dsh/tmp/physical" / slug
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    workspace = workspace.resolve()

    registry = args.registry
    overrides = ["--physical-registry", registry]
    publish_action = workspace / "publish-action"
    physical_action = workspace / "physical-action"

    if not args.no_publish:
        tree = Path(args.tree) if args.tree else REPO_ROOT / ".dsh/tmp/l1" / slug / "src"
        if not tree.is_dir():
            raise SystemExit(
                "no built tree at %s: run tests/run_action_locally.py first, or pass --tree" % tree
            )
        # The recipe's own registry is replaced, so a rehearsal never publishes to the
        # real one. Everything else (auth, manifest, optionalDependencies) stays as the
        # recipe says.
        render(
            args.pkg, args.ver, publish_action, ".github/templates/action.template.yml",
            ["--publish-registry", registry, *overrides, "--root", args.root],
        )
        shutil.copytree(tree, workspace / "src", symlinks=True)
        env = dict(os.environ)
        env["npm_config_cache"] = str(workspace / ".npm-cache")
        if args.token_env_file:
            env["NODE_AUTH_TOKEN"] = Path(args.token_env_file).read_text().strip()
        result = run(["bash", str(publish_action / "steps" / "publish.sh")], cwd=workspace, env=env, text=True)
        if result.returncode != 0:
            print("publish failed; nothing to verify", file=sys.stderr)
            return result.returncode

    render(
        args.pkg, args.ver, physical_action, ".github/templates/physical.template.yml",
        [*overrides, "--root", args.root],
    )

    reports = workspace / "publish-reports"
    plan = workspace / "plan.json"
    plan_args = []
    if reports.is_dir() and any(reports.iterdir()):
        merged = run(
            ["node", str(REPO_ROOT / "scripts" / "physical_plan.mjs"), "--merge", str(reports), "--out", str(plan)],
            text=True,
        )
        if merged.returncode != 0:
            print("cannot merge the publish reports", file=sys.stderr)
            return merged.returncode
        plan_args = ["-e", "PHYSICAL_PLAN=/w/plan.json"]
    else:
        # Without a report the wrapper must not be handed a plan at all: an empty plan
        # reads as "this package was not published in this run" and would skip the
        # verification the caller asked for.
        print("no publish report in this sandbox: the digest is unknown, the run verifies less")

    out_dir = workspace / "physical-reports"
    out_dir.mkdir()
    runner_temp = workspace / "tmp"
    runner_temp.mkdir()
    # The wrapper names the report after the recipe slug, which is not the sandbox slug
    # (that one carries the version): read whatever it wrote.
    report = out_dir / ("physical-report-%s.json" % args.pkg.lstrip("@").replace("/", "-"))
    container_argv = [
        "docker", "run", "--rm", "--network", "host", "--platform", "linux/riscv64",
        "-v", "%s:/w" % workspace,
        "-w", "/w",
        "-e", "RUNNER_TEMP=/w/tmp",
        *plan_args,
        "-e", "PHYSICAL_REPORT=/w/physical-reports",
        args.image,
        "bash", "/w/physical-action/steps/physical.sh",
    ]
    completed = run(container_argv, text=True)
    if completed.returncode != 0:
        print("\nhardware step exited %d" % completed.returncode, file=sys.stderr)

    if not report.is_file():
        written = sorted(out_dir.glob("*.json"))
        report = written[0] if written else report
    if report.is_file():
        report_doc = json.loads(report.read_text(encoding="utf-8"))
        print("\nverdict: %s" % report_doc.get("verdict"))
        for check in report_doc.get("checks") or []:
            print("  %-12s %-6s %s" % (check["name"], check["result"], check["detail"]))
        print("report: %s" % report)
    elif completed.returncode == 0:
        print("nothing was verified (no report was written)")
        return 0
    else:
        print("no report was written", file=sys.stderr)
        return completed.returncode or 1

    if not args.keep:
        print("sandbox is kept at %s (it holds the report; remove it when done)" % workspace)
    verdict = report_doc.get("verdict")
    if verdict == "ok":
        print("\nL2 (hardware) result: OK")
        return 0
    print("\nL2 (hardware) result: %s" % verdict, file=sys.stderr)
    return completed.returncode or 1


if __name__ == "__main__":
    sys.exit(main())
