#!/usr/bin/env python3
"""Detect which recipes need to be built for the current change.

Two modes, both driven by environment variables so the same script serves the
pull-request and the post-merge workflow:

* ``pull_request`` - diff the merge base against the head commit.
* ``push``         - diff ``before..head``; an all-zero ``before`` (new branch or
  force push) means "rebuild everything".

Output (``GITHUB_OUTPUT``)::

    targets=[{"pkg":"bufferutil","ver":"4.2.0","dir":"packages/...","slug":"..."}]
    has_targets=true
    self_check=true|false

Rules:

* ``packages/<pkg>/<ver>/**``      -> that (package, version)
* ``packages/<pkg>/common/**``     -> every version of that package
* a global build input changed (the template, ``scripts/``, ``docker/``)
  -> every recipe is a *candidate*, then narrowed by comparing the build cache keys of
  the base commit and the head: a template edit that does not change a single executed
  command leaves every key intact and therefore rebuilds nothing (see
  ``narrow_by_build_inputs``). ``scripts/**`` additionally self-checks the renderer.
* a candidate whose build key is unchanged but whose *publish* key changed is a
  ``publish`` target: the artifact is what the cache holds, only the delivery changed.
  Rebuilding it would be waste and, worse, would not happen at all: publishing is
  skipped when nothing is built, which is how a fixed publish step once stayed
  undelivered.

``REPUBLISH=true`` (workflow_dispatch only) makes every recipe a ``publish`` target,
so a delivery fix can be rolled out without touching a recipe.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import recipe_lib as rl  # noqa: E402

DEFAULT_ROOT = "packages"
DEFAULT_TEMPLATE = ".github/templates/action.template.yml"
ZERO_SHA = "0" * 40

#: Inputs outside the recipes that can change every built artifact.
GLOBAL_INPUT_PREFIXES = (".github/templates/", "scripts/", "docker/")

#: Tool that computes the build cache key (relative to the repository root).
BUILD_KEY_TOOL = "scripts/build_key.py"


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, cwd=cwd
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def changed_files(
    event: str,
    base_ref: str,
    head_sha: str,
    before_sha: str,
    cwd: Path | None = None,
) -> tuple[list[str], bool]:
    """Return (changed paths, rebuild_everything)."""
    if event == "push":
        if not before_sha or set(before_sha) == {"0"}:
            return [], True
        # A pushed commit range: plain two-dot diff of the actual change.
        out = git("diff", "--name-only", f"{before_sha}..{head_sha}", cwd=cwd)
    else:
        # Pull request: three-dot diff against the merge base, so unrelated
        # commits that landed on the base branch do not leak into the change set.
        out = git("diff", "--name-only", f"origin/{base_ref}...{head_sha}", cwd=cwd)
    return [line for line in out.splitlines() if line.strip()], False


def path_rules(
    root: Path, changed: list[str], template_path: str, rebuild_all: bool = False
) -> tuple[list[rl.Target], list[rl.Target], bool]:
    """Map changed paths onto targets.

    Returns ``(explicit, global_candidates, self_check)``:

    * ``explicit`` - recipes whose own directory (or their ``common`` layer) changed.
      They are always built: a new test still has to run even when the artifact would
      be the same.
    * ``global_candidates`` - every recipe, when a build input *outside* the recipes
      changed (the template, the renderer, the toolchain). The caller narrows this set
      by comparing build inputs with the base commit; the artifacts are not assumed to
      have changed just because a template byte did.
    * ``self_check`` - whether the renderer should be checked with ``--check-all``.
    """
    self_check = False
    global_inputs = bool(rebuild_all)
    selected: dict[tuple[str, str], rl.Target] = {}
    root_prefix = root.name + "/"

    for path in changed:
        if path == template_path or path.startswith(GLOBAL_INPUT_PREFIXES):
            global_inputs = True
            if path.startswith("scripts/"):
                self_check = True
            print(f"# global build input changed: {path}", file=sys.stderr)
            continue
        if not path.startswith(root_prefix):
            continue

        relative = path[len(root_prefix) :]
        parsed = rl.split_recipe_path(relative)
        if parsed is None:
            print(f"# ignoring {path}: not inside a package directory", file=sys.stderr)
            continue
        pkg, ver = parsed

        if ver == rl.COMMON_DIR:
            versions = rl.versions_of(root, pkg)
            if not versions:
                print(f"# {path}: common changed but {pkg} has no version yet", file=sys.stderr)
                continue
            for one in versions:
                selected[(pkg, one)] = rl.Target(pkg=pkg, ver=one, path=root.joinpath(*pkg.split("/")) / one)
            continue

        target = rl.Target(pkg=pkg, ver=ver, path=root.joinpath(*pkg.split("/")) / ver)
        if not target.path.is_dir():
            print(f"# {path}: version directory is gone, nothing to build", file=sys.stderr)
            continue
        selected[(pkg, ver)] = target

    candidates = rl.enumerate_targets(root) if global_inputs else []
    return [selected[key] for key in sorted(selected)], candidates, self_check


def merge(*groups: list[rl.Target]) -> list[rl.Target]:
    """Union of target lists, sorted and de-duplicated.

    A recipe that needs a build wins over the same recipe needing only a publish: the
    build mode runs the publish step as well, so keeping the weaker mode would drop the
    build.
    """
    selected: dict[tuple[str, str], rl.Target] = {}
    for group in groups:
        for target in group:
            previous = selected.get((target.pkg, target.ver))
            if previous is not None and previous.mode == "build":
                continue
            selected[(target.pkg, target.ver)] = target
    return [selected[key] for key in sorted(selected)]


def base_commit(
    event: str,
    base_ref: str,
    head_sha: str,
    before_sha: str,
    cwd: Path | None = None,
) -> str | None:
    """The commit whose build inputs the change should be compared against."""
    if event == "push":
        if not before_sha or set(before_sha) == {"0"}:
            return None
        # A brand new branch has no base to compare with, only a full rebuild.
        return before_sha
    try:
        return git("merge-base", f"origin/{base_ref}", head_sha, cwd=cwd).strip() or None
    except RuntimeError:
        return None


def narrow_by_build_inputs(
    root: Path,
    template_path: str,
    base_sha: str | None,
    candidates: list[rl.Target],
    cwd: Path | None = None,
) -> list[rl.Target] | None:
    """Keep the candidates whose build inputs differ from the base commit.

    A template edit that adds an input or an ``if:`` guard changes the action but not a
    single executed command, so the built artifact is unchanged and the cache would hit
    anyway - rebuilding it is pure waste. Comparing the cache keys of the two revisions
    answers exactly that question, using the same definition the cache itself relies on.

    Returns ``None`` when the comparison is not possible (no base commit, no usable base
    checkout, unreadable recipe): the caller then falls back to building everything,
    which is what a conservative job should do.
    """
    if not base_sha or not candidates:
        return None

    import shutil
    import subprocess
    import tempfile

    import build_key

    cwd = Path(cwd or Path.cwd()).resolve()
    tool = cwd / BUILD_KEY_TOOL
    base_dir = Path(tempfile.mkdtemp(prefix="npm-riscv64-base-"))
    try:
        try:
            git("worktree", "add", "--detach", str(base_dir), base_sha, cwd=cwd)
        except RuntimeError as exc:
            print(f"# cannot check out {base_sha[:12]}: {exc}", file=sys.stderr)
            return None

        # Prefer the tool that the base revision shipped: then a renderer change shows up
        # as a changed key. Older bases have no tool; use ours on their tree instead.
        base_tool = base_dir / BUILD_KEY_TOOL
        base_tool_command: list[str] = []
        if base_tool.is_file():
            command_prefix = [sys.executable, str(base_tool)]
            base_tool_command = command_prefix
        elif tool.is_file():
            command_prefix = [sys.executable, str(tool)]
        else:
            print("# no build_key tool available to compare with", file=sys.stderr)
            return None
        template = str(base_dir / template_path)

        # The publish key is asked of the base's own tool first, so a renderer change
        # shows up here too. A base that predates the flag cannot answer it; then our
        # tool is used on the base tree, which still catches a changed recipe or
        # template but not a changed renderer.
        def publish_key_commands() -> list[list[str]]:
            commands = []
            if base_tool_command:
                commands.append(base_tool_command)
            if tool.is_file():
                commands.append([sys.executable, str(tool)])
            return commands

        def base_publish_key(target: "rl.Target") -> str | None:
            for prefix in publish_key_commands():
                result = subprocess.run(
                    [*prefix, "--root", str(base_dir), "--template", template,
                     "--pkg", target.pkg, "--ver", target.ver, "--publish-key"],
                    capture_output=True, text=True, check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip().splitlines()[-1]
            return None

        kept: list[rl.Target] = []
        for target in candidates:
            result = subprocess.run(
                [*command_prefix, "--root", str(base_dir), "--template", template,
                 "--pkg", target.pkg, "--ver", target.ver],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                print(
                    f"# {target.ident}: cannot compute the base key "
                    f"({result.stderr.strip().splitlines()[-1:] or 'no output'}), keeping it",
                    file=sys.stderr,
                )
                kept.append(target)
                continue
            base_key = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
            try:
                head_key = build_key.build_key(
                    cwd, target.pkg, target.ver, template=cwd / template_path
                )
                head_publish = build_key.publish_key(
                    cwd, target.pkg, target.ver, template=cwd / template_path
                )
            except Exception as exc:  # noqa: BLE001 - keep the recipe on any surprise
                print(f"# {target.ident}: cannot compute the head key ({exc}), keeping it", file=sys.stderr)
                kept.append(target)
                continue
            if base_key != head_key:
                print(
                    f"# {target.ident}: build inputs changed ({base_key or 'none'} -> {head_key})",
                    file=sys.stderr,
                )
                kept.append(target)
                continue
            base_publish = base_publish_key(target)
            if base_publish is None:
                print(
                    f"# {target.ident}: build inputs unchanged (key {head_key}), "
                    "cannot compare publish inputs, skipping the build and not re-publishing",
                    file=sys.stderr,
                )
                continue
            if base_publish != head_publish:
                print(
                    f"# {target.ident}: build inputs unchanged (key {head_key}), "
                    f"publish inputs changed ({base_publish} -> {head_publish}): "
                    "restoring the cached tree and publishing only",
                    file=sys.stderr,
                )
                kept.append(rl.Target(pkg=target.pkg, ver=target.ver, path=target.path, mode="publish"))
                continue
            print(
                f"# {target.ident}: build inputs unchanged (key {head_key}), skipping",
                file=sys.stderr,
            )
        return kept
    finally:
        try:
            git("worktree", "remove", "--force", str(base_dir), cwd=cwd)
        except RuntimeError:
            pass
        shutil.rmtree(base_dir, ignore_errors=True)


def verify_recipes(targets: list[rl.Target]) -> list[str]:
    """Every version directory must carry a .stat; fail early otherwise."""
    problems = []
    for target in targets:
        stat = target.path / rl.STAT_FILE
        if not stat.is_file():
            problems.append(
                f"{target.ident}: missing {rl.STAT_FILE} in {target.path} "
                "(every version directory is a buildable recipe and needs metadata)"
            )
    return problems


def target_payload(target: "rl.Target", stat: dict) -> dict:
    """Describe one matrix entry: everything build.yml needs to run this recipe."""
    glob = str((stat.get("artifacts") or {}).get("path") or "")
    return {
        "pkg": target.pkg,
        "ver": target.ver,
        "dir": target.path.as_posix(),
        "slug": f"{target.slug_pkg}/{target.slug_ver}",
        "artifact": target.artifact_name,
        # Which images the job needs: a cross recipe builds on amd64 and tests on
        # riscv64, so the workflow pulls two images instead of one.
        "toolchain": rl.toolchain_kind(stat),
        "artifact_glob": glob,
        "artifact_dir": str(Path(glob).parent).replace("\\", "/") if glob else "",
        # "build" or "publish": the workflow restores a cached tree for the latter.
        "mode": getattr(target, "mode", "build"),
    }


def main(cwd: Path | None = None) -> int:
    cwd = Path(cwd or Path.cwd())
    root_option = os.environ.get("RECIPE_ROOT", DEFAULT_ROOT)
    root = Path(root_option)
    if not root.is_absolute():
        root = cwd / root
    template_path = os.environ.get("TEMPLATE_PATH", DEFAULT_TEMPLATE)
    event = os.environ.get("EVENT_NAME", "pull_request")
    base_ref = os.environ.get("BASE_REF", "main")
    head_sha = os.environ.get("HEAD_SHA") or git("rev-parse", "HEAD", cwd=cwd).strip()
    before_sha = os.environ.get("BEFORE_SHA", "")

    try:
        changed, rebuild_all = changed_files(event, base_ref, head_sha, before_sha, cwd=cwd)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    explicit, candidates, self_check = path_rules(root, changed, template_path, rebuild_all)

    republish = os.environ.get("REPUBLISH", "").lower() in ("1", "true", "yes")
    if republish and event == "workflow_dispatch":
        # Every recipe, publish only: no build input changed, the cache holds the trees.
        # This is the lever for rolling out a delivery fix - a publish step that had
        # never run, a registry that moved - without touching a single recipe.
        targets = [
            rl.Target(pkg=t.pkg, ver=t.ver, path=t.path, mode="publish")
            for t in rl.enumerate_targets(root)
        ]
        print(
            f"# republish requested: {len(targets)} recipe(s) publish from the cache",
            file=sys.stderr,
        )
        explicit = []
        candidates = []
    else:
        targets = merge(explicit, candidates)
    if not republish and candidates and any(path.startswith("docker/") for path in changed):
        # The toolchain itself changed. Its effect reaches the key through the image id,
        # which is only known after the image is rebuilt, so nothing here can be narrowed
        # away: build everything and let the build job's key decide about the cache.
        print(
            "# toolchain changed: every recipe is kept, the image digest is resolved later",
            file=sys.stderr,
        )
    elif not republish and candidates:
        narrowed = narrow_by_build_inputs(
            root,
            template_path,
            base_commit(event, base_ref, head_sha, before_sha, cwd=cwd),
            candidates,
            cwd=cwd,
        )
        if narrowed is None:
            print(
                "# no base to compare build inputs with, keeping every recipe",
                file=sys.stderr,
            )
        else:
            # Explicit changes stay: a recipe whose test or .stat changed still has to
            # run even when its artifact would be identical.
            targets = merge(explicit, narrowed)

    publish_allowed = event == "push" or (event == "workflow_dispatch" and republish)
    if not publish_allowed:
        dropped = [target for target in targets if target.mode == "publish"]
        if dropped:
            print(
                "# publish step changed but this run cannot publish "
                f"({event}): {len(dropped)} recipe(s) have nothing to build",
                file=sys.stderr,
            )
        targets = [target for target in targets if target.mode != "publish"]

    problems = rl.repository_problems(root) + verify_recipes(targets)
    if problems:
        print("error: the recipe tree is not valid, refusing to build:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    payload = []
    for t in targets:
        try:
            stat = rl.load_stat(t)
        except rl.RecipeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        payload.append(target_payload(t, stat))
    compact = json.dumps(payload, separators=(",", ":"))
    # publish is only allowed after the merge lands on the default branch (D5), or when
    # an operator explicitly asks for a re-delivery of the cached trees.
    publish = "true" if (publish_allowed and payload) else "false"
    outputs = {
        "targets": compact,
        "has_targets": "true" if payload else "false",
        "self_check": "true" if self_check else "false",
        "publish": publish,
        "changed_count": str(len(changed)),
    }

    print(f"# event={event} changed={len(changed)} targets={len(payload)} self_check={self_check}")
    for entry in payload:
        print(f"  - {entry['pkg']}@{entry['ver']}  ({entry['dir']})  [{entry['mode']}]")

    for key, value in outputs.items():
        print(f"{key}={value}")
        target_file = os.environ.get("GITHUB_OUTPUT")
        if target_file:
            with open(target_file, "a", encoding="utf-8") as handle:
                handle.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
