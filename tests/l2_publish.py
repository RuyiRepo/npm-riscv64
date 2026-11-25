#!/usr/bin/env python3
"""Publish-semantics verification (L2) against a local registry.

What this checks, using the *rendered* publish script (never a hand written
equivalent):

1. A/B ordering - the native package is published before the JS package that
   references it, even when the recipe lists them in the other order.
2. Idempotency - a second run skips both packages without error.
3. Manifest merge - the version is written from ``.stat`` while unrelated fields
   in the upstream package.json survive byte-for-byte.
4. Identity enforcement - with ``generate_manifest: false`` a renamed package is
   rejected instead of silently published under another name.
5. Partial-failure re-entry - if B fails, a rerun publishes only B.

The registry is verdaccio, started on 127.0.0.1:4873 (``DSH_L2_PORT`` picks another
port, which matters when a leftover container still holds the default) with uplinks
disabled so the test is hermetic.

Usage::

    python3 tests/l2_publish.py            # start registry, run all cases
    python3 tests/l2_publish.py --keep     # leave the sandbox for inspection
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SANDBOX = REPO_ROOT / ".dsh/tmp/l2"
PORT = int(os.environ.get("DSH_L2_PORT", "4873"))
REGISTRY = f"http://127.0.0.1:{PORT}"
USER = "dsh"
PASSWORD = "dsh-test-password"
EMAIL = "dsh@example.com"

VERDACCIO_CONFIG = """\
storage: /verdaccio/storage
auth:
  htpasswd:
    file: /verdaccio/conf/htpasswd
    max_users: 100
uplinks: {}
packages:
  '**':
    access: $all
    publish: $authenticated
    unpublish: $authenticated
log: { type: stdout, format: pretty, level: warn }
"""

CONTAINER = "dsh-verdaccio-l2"

STAT = """\
upstream:
  repo: https://github.com/example/demo
  ref: v{ver}

artifacts:
  path: prebuilds/linux-riscv64/*.node

npm_publish:
  - role: main
    name: "@demo/{name}"
    version: {ver}
    dir: .
    requires: ["@demo/{name}-linux-riscv64-gnu"]
    # The platform reference is declared rather than inferred from `requires`:
    # requires is build ordering, optional_dependencies is what an install pulls.
    optional_dependencies: ["@demo/{name}-linux-riscv64-gnu"]
    token_env: NODE_AUTH_TOKEN
    generate_manifest: {main_manifest}
  - role: binary
    name: "@demo/{name}-linux-riscv64-gnu"
    version: {ver}
    dir: build/pkg
    os: linux
    cpu: riscv64
    token_env: NODE_AUTH_TOKEN
    generate_manifest: true
"""

UPSTREAM_MANIFEST = {
    "name": "@demo/widget",
    "version": "0.0.0",
    "description": "upstream description that must survive",
    "main": "index.js",
    "files": ["index.js", "prebuilds"],
    "dependencies": {"node-gyp-build": "^4.8.0"},
    "scripts": {"test": "mocha"},
    "license": "MIT",
}


class Failures:
    def __init__(self) -> None:
        self.items: list[str] = []

    def check(self, ok: bool, message: str, detail: str = "") -> None:
        status = "ok  " if ok else "FAIL"
        print(f"  [{status}] {message}")
        if not ok:
            if detail:
                print(f"         {detail}")
            self.items.append(message)


def wipe(path: Path) -> None:
    """Remove a sandbox directory, including files the registry container owns."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        return
    except PermissionError:
        pass
    # Files written by the container belong to another uid; let a container
    # remove them instead of fighting file ownership from the host.
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{path}:/w",
            "debian:trixie-slim",
            "sh",
            "-c",
            "rm -rf /w/* /w/.[!.]* 2>/dev/null || true",
        ],
        check=False,
        capture_output=True,
    )
    for entry in sorted(path.iterdir()):
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def wait_for_registry(timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{REGISTRY}/-/ping", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - registry not up yet
            time.sleep(0.5)
    return False


def start_registry(work: Path) -> None:
    """Run verdaccio in docker: no host npm install, no uplinks, throwaway storage."""
    base = work / "verdaccio"
    conf_dir = base / "conf"
    storage = base / "storage"
    conf_dir.mkdir(parents=True, exist_ok=True)
    storage.mkdir(parents=True, exist_ok=True)
    (conf_dir / "config.yaml").write_text(VERDACCIO_CONFIG, encoding="utf-8")
    # The image runs as an unprivileged user; keep the mount points writable.
    for path in (base, conf_dir, storage):
        path.chmod(0o777)

    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER,
            "-p",
            f"127.0.0.1:{PORT}:4873",
            "-v",
            f"{conf_dir}:/verdaccio/conf",
            "-v",
            f"{storage}:/verdaccio/storage",
            "verdaccio/verdaccio:5",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"docker run failed: {result.stderr}")


def stop_registry() -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


def login() -> str:
    payload = json.dumps(
        {"name": USER, "password": PASSWORD, "email": EMAIL}
    ).encode()
    request = urllib.request.Request(
        f"{REGISTRY}/-/user/org.couchdb.user:{USER}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode())
    token = body.get("token")
    if not token:
        raise SystemExit(f"could not obtain a token from verdaccio: {body}")
    return token


def make_recipe(root: Path, ver: str, *, main_manifest: str = "true") -> Path:
    recipe = root / "packages/@demo/widget" / ver
    (recipe / "commands").mkdir(parents=True, exist_ok=True)
    # Mirror the CI layout: the recipe lives in packages/, the built source tree
    # (and the manifest that gets published) lives in src/. The binary package's
    # directory is normally created by the build step, which this harness does not
    # run, so it is created here with something to publish.
    src = root / "src"
    (src / "prebuilds/linux-riscv64").mkdir(parents=True, exist_ok=True)
    (src / "prebuilds/linux-riscv64/widget.node").write_bytes(b"\x7fELF not really")
    (src / "build/pkg").mkdir(parents=True, exist_ok=True)
    (src / "build/pkg/widget.node").write_bytes(b"\x7fELF not really")
    (recipe / ".stat").write_text(
        STAT.format(ver=ver, name="widget", main_manifest=main_manifest),
        encoding="utf-8",
    )
    (recipe / "commands/build").write_text("echo build\n", encoding="utf-8")
    (recipe / "commands/test").write_text("echo test\n", encoding="utf-8")
    return recipe


def render_publish(root: Path, ver: str, work: Path) -> Path:
    out_dir = work / f"rendered-{ver}"
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts/render.py"),
        "--root",
        str(root / "packages"),
        "--pkg",
        "@demo/widget",
        "--ver",
        ver,
        "--out-dir",
        str(out_dir),
    ]
    for index in range(2):
        cmd.append(f"--override=npm_publish.{index}.registry={REGISTRY}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"render failed:\n{result.stdout}\n{result.stderr}")
    return out_dir / "steps/publish.sh"


def run_publish(script: Path, workspace: Path, token: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        NODE_AUTH_TOKEN=token,
        HOME=str(workspace / "home"),
        npm_config_cache=str(workspace / "npm-cache"),
    )
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", str(script)],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )


def npm_view(name: str, version: str, token: str, workspace: Path) -> dict | None:
    env = dict(os.environ)
    env.update(HOME=str(workspace / "home"), npm_config_cache=str(workspace / "npm-cache"))
    npmrc = workspace / ".view-npmrc"
    npmrc.write_text(
        f"//127.0.0.1:{PORT}/:_authToken={token}\n", encoding="utf-8"
    )
    env["NPM_CONFIG_USERCONFIG"] = str(npmrc)
    result = subprocess.run(
        ["npm", "view", f"{name}@{version}", "--json", "--registry", REGISTRY],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep the sandbox directory")
    args = parser.parse_args(argv)

    work = SANDBOX
    if work.exists() and not args.keep:
        stop_registry()
        wipe(work)
    work.mkdir(parents=True, exist_ok=True)
    workspace = work / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    print("starting verdaccio ...")
    start_registry(work)
    failures = Failures()
    try:
        if not wait_for_registry():
            log = subprocess.run(
                ["docker", "logs", CONTAINER], capture_output=True, text=True
            ).stdout
            print("registry did not start:\n" + log[-4000:], file=sys.stderr)
            return 1
        token = login()
        print(f"registry ready at {REGISTRY}")

        # ---- case 1 + 2 + 3: publish, ordering, merge, idempotency ----------
        print("\ncase 1: publish A then B with merge into the upstream manifest")
        recipe_root = workspace
        ver = "1.0.0"
        recipe = make_recipe(recipe_root, ver)
        (workspace / "src/package.json").write_text(
            json.dumps(UPSTREAM_MANIFEST, indent=2) + "\n", encoding="utf-8"
        )
        script = render_publish(recipe_root, ver, work)
        result = run_publish(script, recipe_root, token)
        print(result.stdout.strip())
        if result.returncode != 0:
            print(result.stderr.strip(), file=sys.stderr)
        failures.check(result.returncode == 0, "publish script exits 0")

        text = result.stdout
        binary_at = text.find("@demo/widget-linux-riscv64-gnu@")
        main_at = text.find("@demo/widget@1.0.0")
        failures.check(
            0 <= binary_at < main_at,
            "native package is published before the JS package",
            f"binary@{binary_at} main@{main_at}",
        )

        binary = npm_view("@demo/widget-linux-riscv64-gnu", ver, token, workspace)
        main_pkg = npm_view("@demo/widget", ver, token, workspace)
        failures.check(binary is not None, "native package is on the registry")
        failures.check(main_pkg is not None, "JS package is on the registry")
        if binary:
            failures.check(binary.get("os") == ["linux"], "native package pins os=[linux]")
            failures.check(binary.get("cpu") == ["riscv64"], "native package pins cpu=[riscv64]")
        if main_pkg:
            failures.check(main_pkg.get("version") == ver, "version comes from the recipe")
            failures.check(
                (main_pkg.get("optionalDependencies") or {}).get(
                    "@demo/widget-linux-riscv64-gnu"
                )
                == ver,
                "JS package references the native package at the same version",
            )
            failures.check(
                main_pkg.get("description") == UPSTREAM_MANIFEST["description"],
                "description survives the manifest merge",
            )
            # npm's own metadata normalisation copies optionalDependencies into
            # dependencies, so compare as a subset rather than for equality.
            merged = main_pkg.get("dependencies") or {}
            failures.check(
                all(merged.get(key) == value for key, value in UPSTREAM_MANIFEST["dependencies"].items()),
                "upstream dependencies survive the manifest merge",
                json.dumps(merged),
            )
            failures.check(
                main_pkg.get("files") == UPSTREAM_MANIFEST["files"],
                "files survives the manifest merge",
            )
            failures.check(
                main_pkg.get("scripts") == UPSTREAM_MANIFEST["scripts"],
                "scripts survive the manifest merge",
            )
        on_disk = json.loads((workspace / "src/package.json").read_text(encoding="utf-8"))
        failures.check(
            on_disk.get("name") == "@demo/widget",
            "recipe renamed the manifest in place",
            json.dumps(on_disk)[:200],
        )

        print("\ncase 2: rerun is idempotent")
        again = run_publish(script, recipe_root, token)
        print(again.stdout.strip())
        failures.check(again.returncode == 0, "rerun exits 0")
        failures.check(
            again.stdout.count("already published") == 2,
            "both packages report 'already published'",
            again.stdout,
        )

        # ---- case 4: identity enforcement -----------------------------------
        print("\ncase 4: generate_manifest=false refuses a rename")
        ver4 = "1.1.0"
        recipe4 = make_recipe(recipe_root, ver4, main_manifest="false")
        (workspace / "src/package.json").write_text(
            json.dumps({**UPSTREAM_MANIFEST, "name": "other-name", "version": ver4}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        script4 = render_publish(recipe_root, ver4, work)
        result4 = run_publish(script4, recipe_root, token)
        print(result4.stderr.strip())
        failures.check(result4.returncode != 0, "rename is rejected")
        # The refusal has to name both sides, not just say "no".
        combined = result4.stdout + result4.stderr
        failures.check(
            "the base package is other-name" in combined
            and "the recipe publishes @demo/widget" in combined,
            "the rejection names the mismatch",
            combined,
        )

        # ---- case 5: partial failure re-entry -------------------------------
        print("\ncase 5: partial failure leaves the other package publishable")
        ver5 = "1.2.0"
        recipe5 = make_recipe(recipe_root, ver5)
        # A publishes; B's manifest is corrupt so the run dies halfway.
        (workspace / "src/package.json").write_text("{ this is not json", encoding="utf-8")
        script5 = render_publish(recipe_root, ver5, work)
        first = run_publish(script5, recipe_root, token)
        binary5 = npm_view("@demo/widget-linux-riscv64-gnu", ver5, token, workspace)
        failures.check(first.returncode != 0, "run fails on the corrupt manifest")
        failures.check(binary5 is not None, "the native package was published first")
        failures.check(
            npm_view("@demo/widget", ver5, token, workspace) is None,
            "the JS package stayed unpublished",
        )
        (workspace / "src/package.json").write_text(
            json.dumps(UPSTREAM_MANIFEST, indent=2) + "\n", encoding="utf-8"
        )
        second = run_publish(script5, recipe_root, token)
        print(second.stdout.strip())
        failures.check(second.returncode == 0, "rerun after the fix exits 0")
        failures.check(
            "already published: @demo/widget-linux-riscv64-gnu@1.2.0" in second.stdout,
            "the native package is skipped on rerun",
            second.stdout,
        )
        failures.check(
            npm_view("@demo/widget", ver5, token, workspace) is not None,
            "the JS package is published on rerun",
        )

        # ---- case 6: a version bump publishes alongside the old one ---------
        print("\ncase 6: a new version does not disturb the published one")
        ver6 = "1.3.0"
        recipe6 = make_recipe(recipe_root, ver6)
        (workspace / "src/package.json").write_text(
            json.dumps(UPSTREAM_MANIFEST, indent=2) + "\n", encoding="utf-8"
        )
        script6 = render_publish(recipe_root, ver6, work)
        result6 = run_publish(script6, recipe_root, token)
        print(result6.stdout.strip())
        failures.check(result6.returncode == 0, "new version publishes")
        failures.check(
            npm_view("@demo/widget", ver, token, workspace) is not None,
            "the previously published version is still there",
        )
    finally:
        stop_registry()

    print("\nL2 result:", "OK" if not failures.items else f"FAILED ({len(failures.items)})")
    for item in failures.items:
        print(f"  - {item}")
    return 0 if not failures.items else 1


if __name__ == "__main__":
    sys.exit(main())
