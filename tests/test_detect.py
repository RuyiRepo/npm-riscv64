"""Change-detector tests (L0: static/unit level).

Each test builds a throw-away git repository, so the diff logic is exercised the
way GitHub Actions drives it (three-dot for pull requests, two-dot for pushes).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import detect_targets as dt  # noqa: E402
import recipe_lib as rl  # noqa: E402

TEMPLATE = ".github/templates/action.template.yml"

STAT = """\
upstream:
  repo: https://github.com/example/demo
  ref: v{ver}

artifacts:
  path: build/*.node

npm_publish:
  - role: main
    name: "@demo/{name}"
    version: {ver}
    dir: .
"""


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


class GitFixture:
    def __init__(self, root: Path):
        self.root = root
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "test@example.com")
        git(root, "config", "user.name", "test")

    def write(self, relative: str, content: str = "x\n") -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def recipe(self, pkg: str, ver: str, *, stat: bool = True) -> None:
        base = f"packages/{pkg}/{ver}"
        if stat:
            name = pkg.split("/")[-1]
            self.write(f"{base}/.stat", STAT.format(ver=ver, name=name))
        self.write(f"{base}/commands/build", "echo build\n")
        self.write(f"{base}/commands/test", "echo test\n")

    def common(self, pkg: str) -> None:
        self.write(f"packages/{pkg}/common/commands/build", "echo common\n")

    def commit(self, message: str = "change", allow_empty: bool = False) -> str:
        git(self.root, "add", "-A")
        args = ["commit", "-qm", message] + (["--allow-empty"] if allow_empty else [])
        git(self.root, *args)
        return git(self.root, "rev-parse", "HEAD").strip()

    def mark_origin_main(self, sha: str) -> None:
        git(self.root, "update-ref", "refs/remotes/origin/main", sha)

    def branch(self, name: str = "feature") -> None:
        git(self.root, "checkout", "-q", "-b", name)


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = GitFixture(Path(self.tmp.name))
        self.repo.write(TEMPLATE, "name: x\n")
        self.repo.recipe("plain", "1.0.0")
        self.repo.recipe("@scope/demo", "2.0.0")
        self.repo.recipe("plain", "1.1.0")
        base = self.repo.commit("base")
        self.repo.mark_origin_main(base)
        self.repo.branch()
        self.packages = self.repo.root / "packages"

    def tearDown(self):
        self.tmp.cleanup()

    def run_detect(self, *, event="pull_request", before="", base_ref="main") -> dict:
        head = self.repo.commit("feature change")
        changed, rebuild_all = dt.changed_files(
            event, base_ref, head, before or head, cwd=self.repo.root
        )
        # No base commit is compared here: path rules alone, which is what the
        # narrowing tests start from.
        explicit, candidates, self_check = dt.path_rules(
            self.packages, changed, TEMPLATE, rebuild_all
        )
        targets = dt.merge(explicit, candidates)
        return {
            "targets": [(t.pkg, t.ver) for t in targets],
            "self_check": self_check,
            "problems": dt.verify_recipes(targets),
        }

    def test_version_change_selects_one_target(self):
        self.repo.write("packages/plain/1.0.0/commands/build", "echo changed\n")
        result = self.run_detect()
        self.assertEqual(result["targets"], [("plain", "1.0.0")])
        self.assertEqual(result["problems"], [])

    def test_scoped_path_is_parsed(self):
        self.repo.write("packages/@scope/demo/2.0.0/commands/test", "echo t2\n")
        result = self.run_detect()
        self.assertEqual(result["targets"], [("@scope/demo", "2.0.0")])

    def test_common_change_expands_to_every_version(self):
        self.repo.common("plain")
        result = self.run_detect()
        self.assertEqual(sorted(result["targets"]), [("plain", "1.0.0"), ("plain", "1.1.0")])

    def test_template_change_rebuilds_everything(self):
        self.repo.write(TEMPLATE, "name: y\n")
        result = self.run_detect()
        self.assertEqual(
            sorted(result["targets"]),
            [("@scope/demo", "2.0.0"), ("plain", "1.0.0"), ("plain", "1.1.0")],
        )

    def test_scripts_change_requests_self_check_and_candidates(self):
        # The renderer generates the fetch/patch commands, so it is a global build input:
        # every recipe becomes a candidate and is narrowed by key comparison (see
        # BuildInputNarrowingTests). Without a base commit nothing is narrowed away.
        self.repo.write("scripts/render.py", "# changed\n")
        result = self.run_detect()
        self.assertTrue(result["self_check"])
        self.assertEqual(
            sorted(result["targets"]),
            [("@scope/demo", "2.0.0"), ("plain", "1.0.0"), ("plain", "1.1.0")],
        )

    def test_documentation_change_builds_nothing(self):
        self.repo.write("README.md", "docs\n")
        result = self.run_detect()
        self.assertEqual(result["targets"], [])
        self.assertFalse(result["self_check"])

    def test_deleted_version_is_skipped(self):
        git(self.repo.root, "rm", "-q", "-r", "packages/plain/1.1.0")
        result = self.run_detect()
        self.assertEqual(result["targets"], [])

    def test_missing_stat_is_reported(self):
        self.repo.recipe("plain", "3.0.0", stat=False)
        result = self.run_detect()
        self.assertEqual(result["targets"], [("plain", "3.0.0")])
        self.assertTrue(result["problems"])
        self.assertIn("missing .stat", result["problems"][0])

    def test_push_with_zero_before_rebuilds_everything(self):
        self.repo.write("packages/plain/1.0.0/commands/build", "echo z\n")
        head = self.repo.commit("push change")
        changed, rebuild_all = dt.changed_files(
            "push", "main", head, "0" * 40, cwd=self.repo.root
        )
        self.assertTrue(rebuild_all)
        explicit, candidates, _ = dt.path_rules(self.packages, changed, TEMPLATE, rebuild_all)
        targets = dt.merge(explicit, candidates)
        self.assertEqual(len(targets), 3)

    def test_push_range_selects_only_changed_recipe(self):
        before = git(self.repo.root, "rev-parse", "HEAD").strip()
        self.repo.write("packages/@scope/demo/2.0.0/commands/test", "echo z\n")
        head = self.repo.commit("push change")
        changed, rebuild_all = dt.changed_files(
            "push", "main", head, before, cwd=self.repo.root
        )
        self.assertFalse(rebuild_all)
        explicit, candidates, _ = dt.path_rules(self.packages, changed, TEMPLATE, rebuild_all)
        targets = dt.merge(explicit, candidates)
        self.assertEqual([(t.pkg, t.ver) for t in targets], [("@scope/demo", "2.0.0")])


class DetectOutputTests(unittest.TestCase):
    def test_github_output_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = GitFixture(root)
            repo.write(TEMPLATE, "name: x\n")
            repo.recipe("@scope/demo", "2.0.0")
            base = repo.commit("base")
            repo.mark_origin_main(base)
            repo.branch()
            repo.write("packages/@scope/demo/2.0.0/commands/build", "echo changed\n")
            head = repo.commit("change")

            output = root / "github_output"
            output.write_text("", encoding="utf-8")
            env = dict(os.environ)
            env.update(
                EVENT_NAME="pull_request",
                BASE_REF="main",
                HEAD_SHA=head,
                GITHUB_OUTPUT=str(output),
                RECIPE_ROOT="packages",
                TEMPLATE_PATH=TEMPLATE,
            )
            saved = dict(os.environ)
            os.environ.update(env)
            try:
                code = dt.main(cwd=root)
            finally:
                os.environ.clear()
                os.environ.update(saved)

            self.assertEqual(code, 0)
            lines = output.read_text(encoding="utf-8").splitlines()
            keys = dict(line.split("=", 1) for line in lines)
            self.assertEqual(keys["has_targets"], "true")
            self.assertEqual(keys["publish"], "false")
            payload = json.loads(keys["targets"])
            self.assertEqual(payload[0]["pkg"], "@scope/demo")
            self.assertEqual(payload[0]["slug"], "scope-demo/2.0.0")
            self.assertEqual(payload[0]["artifact"], "scope-demo-2.0.0-riscv64")
            self.assertEqual(payload[0]["artifact_glob"], "build/*.node")


class BuildInputNarrowingTests(unittest.TestCase):
    """A global input change selects every recipe, then narrows by comparing keys.

    The narrowing is what keeps a template-only pull request from rebuilding packages it
    never touched: the edit may change the action without changing a single executed
    command. The tests drive the same three steps the job runs - path rules, base commit,
    key comparison - plus ``main`` for the fallbacks.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.repo = GitFixture(root)
        # The narrowing runs the *base* checkout's own tool, so the fixture carries the
        # real scripts and the real template; a stub would not render.
        for path in sorted((REPO_ROOT / "scripts").iterdir()):
            if path.is_file() and path.suffix in {".py", ".sh", ".mjs"}:
                self.repo.write(f"scripts/{path.name}", path.read_text(encoding="utf-8"))
        self.repo.write(TEMPLATE, (REPO_ROOT / TEMPLATE).read_text(encoding="utf-8"))
        self.repo.recipe("plain", "1.0.0")
        self.repo.recipe("@scope/demo", "2.0.0")
        base = self.repo.commit("base")
        self.repo.mark_origin_main(base)
        self.repo.branch()
        self.packages = root / "packages"
        self.everything = [("@scope/demo", "2.0.0"), ("plain", "1.0.0")]

    def tearDown(self):
        self.tmp.cleanup()

    def rewrite_template(self, mutate) -> None:
        path = self.root / TEMPLATE
        path.write_text(mutate(path.read_text(encoding="utf-8")), encoding="utf-8")

    def insert_after_step_start(self, step: str, line: str) -> None:
        def mutate(text: str) -> str:
            lines = text.splitlines(keepends=True)
            start = next(i for i, v in enumerate(lines) if v.strip() == f"- name: {step}")
            run = next(i for i, v in enumerate(lines[start:], start) if v.strip() == "run: |")
            indent = " " * (len(lines[run]) - len(lines[run].lstrip()) + 2)
            lines.insert(run + 1, indent + line + "\n")
            return "".join(lines)

        self.rewrite_template(mutate)

    def add_template_input(self) -> None:
        self.rewrite_template(
            lambda text: text.replace("  publish:\n", '  probe:\n    default: "x"\n  publish:\n', 1)
        )

    def detect(self) -> dict:
        head = self.repo.commit("feature change")
        changed, rebuild_all = dt.changed_files("pull_request", "main", head, head, cwd=self.root)
        explicit, candidates, self_check = dt.path_rules(
            self.packages, changed, TEMPLATE, rebuild_all
        )
        narrowed = dt.narrow_by_build_inputs(
            self.packages,
            TEMPLATE,
            dt.base_commit("pull_request", "main", head, "", cwd=self.root),
            candidates,
            cwd=self.root,
        )
        targets = dt.merge(explicit, narrowed if narrowed is not None else candidates)
        return {
            "targets": [(t.pkg, t.ver) for t in targets],
            "candidates": [(t.pkg, t.ver) for t in candidates],
            "explicit": [(t.pkg, t.ver) for t in explicit],
            "self_check": self_check,
            "compared": narrowed is not None,
            "modes": {(t.pkg, t.ver): t.mode for t in targets},
        }

    def run_main(self, *, event: str, before: str, republish: str = "") -> dict:
        # A manual republish changes nothing in the tree, so the commit may be empty.
        head = self.repo.commit("feature change", allow_empty=True)
        output = self.root / "github_output"
        output.write_text("", encoding="utf-8")
        env = dict(os.environ)
        env.update(
            EVENT_NAME=event,
            BASE_REF="main",
            HEAD_SHA=head,
            BEFORE_SHA=before,
            GITHUB_OUTPUT=str(output),
            RECIPE_ROOT="packages",
            TEMPLATE_PATH=TEMPLATE,
            REPUBLISH=republish,
        )
        saved = dict(os.environ)
        os.environ.update(env)
        try:
            code = dt.main(cwd=self.root)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(code, 0)
        keys = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        return json.loads(keys["targets"])

    def test_cosmetic_template_change_builds_nothing(self):
        # an extra input and an if: guard change the action, not the build
        self.add_template_input()
        self.rewrite_template(
            lambda text: text.replace(
                "    - name: fetch upstream source\n",
                "    - name: fetch upstream source\n      if: inputs.reuse != 'true'\n",
                1,
            )
        )
        result = self.detect()
        self.assertTrue(result["compared"])
        self.assertEqual(result["candidates"], self.everything)
        self.assertEqual(result["targets"], [])

    def test_template_build_command_change_rebuilds_everything(self):
        self.insert_after_step_start("build", "echo probe-build")
        result = self.detect()
        self.assertEqual(result["targets"], self.everything)

    def test_recipe_change_survives_narrowing(self):
        # The recipe's own change is explicit: kept even though it compares equal.
        self.repo.write("packages/plain/1.0.0/commands/test", "echo new test\n")
        self.add_template_input()
        result = self.detect()
        self.assertEqual(result["explicit"], [("plain", "1.0.0")])
        self.assertEqual(result["targets"], [("plain", "1.0.0")])

    def test_renderer_edit_without_output_change_builds_nothing(self):
        before = (REPO_ROOT / "scripts" / "render.py").read_text(encoding="utf-8")
        self.repo.write("scripts/render.py", before + "\n# comment only\n")
        result = self.detect()
        self.assertTrue(result["self_check"])
        self.assertEqual(result["targets"], [])

    def test_base_without_the_tool_still_compares(self):
        # A base that predates scripts/build_key.py: the head tool renders its tree.
        (self.root / "scripts" / "build_key.py").unlink()
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "drop the tool")
        self.repo.mark_origin_main(git(self.root, "rev-parse", "HEAD").strip())
        self.repo.write(
            "scripts/build_key.py",
            (REPO_ROOT / "scripts" / "build_key.py").read_text(encoding="utf-8"),
        )
        self.add_template_input()
        result = self.detect()
        self.assertTrue(result["compared"])
        self.assertEqual(result["targets"], [])

    def test_a_publish_step_change_redelivers_without_building(self):
        # The delivery changed; the artifact did not. Rebuilding would be waste, and
        # doing nothing is what used to happen when a publish fix landed.
        self.insert_after_step_start("publish", 'echo "[publish] replaced"')
        result = self.detect()
        self.assertEqual(sorted(result["targets"]), self.everything)
        self.assertEqual(set(result["modes"].values()), {"publish"})

    def test_publish_only_targets_are_dropped_when_the_run_cannot_publish(self):
        self.insert_after_step_start("publish", 'echo "[publish] replaced"')
        payload = self.run_main(event="pull_request", before="")
        self.assertEqual(payload, [])

    def test_publish_only_targets_survive_on_a_push(self):
        self.insert_after_step_start("publish", 'echo "[publish] replaced"')
        payload = self.run_main(event="push", before=self.repo_previous_commit())
        self.assertEqual(
            sorted((entry["pkg"], entry["ver"], entry["mode"]) for entry in payload),
            [(pkg, ver, "publish") for pkg, ver in self.everything],
        )

    def test_republish_publishes_every_recipe_from_the_cache(self):
        payload = self.run_main(event="workflow_dispatch", before="", republish="true")
        self.assertEqual(
            sorted((entry["pkg"], entry["ver"], entry["mode"]) for entry in payload),
            [(pkg, ver, "publish") for pkg, ver in self.everything],
        )

    def test_new_branch_still_rebuilds_everything(self):
        # No base commit to compare with: fall back to building every recipe.
        self.add_template_input()
        payload = self.run_main(event="push", before="0" * 40)
        self.assertEqual(
            sorted((entry["pkg"], entry["ver"]) for entry in payload), self.everything
        )

    def test_main_narrows_a_cosmetic_template_change(self):
        self.add_template_input()
        payload = self.run_main(event="push", before=self.repo_previous_commit())
        self.assertEqual(payload, [])

    def repo_previous_commit(self) -> str:
        return git(self.root, "rev-parse", "HEAD").strip()


class PayloadTests(unittest.TestCase):
    """The matrix payload is the contract with build.yml: a field going missing here
    breaks the workflow, not the renderer."""

    def test_payload_carries_the_toolchain(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        stat = (
            "upstream:\n  repo: https://github.com/example/demo\n  ref: v1.0.0\n"
            "toolchain: cross\n"
            "artifacts:\n  path: out/pkg/dist/shared/*.node\n"
            'npm_publish:\n  - role: main\n    name: "@demo/widget"\n    version: 1.0.0\n'
            "    dir: out/pkg\n    generate_manifest: true\n"
        )
        (root / "packages/demo/1.0.0").mkdir(parents=True)
        (root / "packages/demo/1.0.0/.stat").write_text(stat, encoding="utf-8")
        (root / "packages/demo/1.0.0/commands").mkdir()
        (root / "packages/demo/1.0.0/commands/build").write_text("echo build\n", encoding="utf-8")
        (root / "packages/demo/1.0.0/commands/test").write_text("echo test\n", encoding="utf-8")

        target = rl.find_target(root / "packages", "demo", "1.0.0")
        payload = dt.target_payload(target, rl.load_stat(target))
        self.assertEqual(payload["toolchain"], "cross")
        self.assertEqual(payload["artifact_glob"], "out/pkg/dist/shared/*.node")
        self.assertEqual(payload["artifact_dir"], "out/pkg/dist/shared")

    def test_payload_defaults_to_native(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "packages/demo/1.0.0").mkdir(parents=True)
        (root / "packages/demo/1.0.0/.stat").write_text(
            "upstream:\n  repo: https://github.com/example/demo\n  ref: v1.0.0\n"
            "artifacts:\n  path: build/Release/*.node\n"
            'npm_publish:\n  - role: main\n    name: "@demo/widget"\n    version: 1.0.0\n'
            "    dir: .\n    generate_manifest: true\n",
            encoding="utf-8",
        )
        (root / "packages/demo/1.0.0/commands").mkdir()
        (root / "packages/demo/1.0.0/commands/build").write_text("echo build\n", encoding="utf-8")
        (root / "packages/demo/1.0.0/commands/test").write_text("echo test\n", encoding="utf-8")
        target = rl.find_target(root / "packages", "demo", "1.0.0")
        self.assertEqual(dt.target_payload(target, rl.load_stat(target))["toolchain"], "native")



if __name__ == "__main__":
    unittest.main()
