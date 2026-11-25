"""Build cache key tests (L0).

The key decides whether a previous tree is restored instead of rebuilt, so it has to be
stable (same build inputs, same key) and sensitive (a changed build input, a new key).
Because the key is computed from the *rendered* commands, the tests also pin the other
direction: edits that cannot change the artifact - a template input, an ``if:`` guard, a
test snippet, a publish-only ``.stat`` field - must leave the key alone.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_key  # noqa: E402

REAL_TEMPLATE = (REPO_ROOT / ".github/templates/action.template.yml").read_text(encoding="utf-8")

STAT = """\
upstream:
  repo: https://github.com/example/demo
  ref: v1.0.0

artifacts:
  path: build/Release/*.node

npm_publish:
  - role: main
    name: "@demo/widget"
    version: 1.0.0
    dir: .
    registry: https://example.invalid
    generate_manifest: true
"""


class CacheRepo:
    """A miniature repository: recipes plus the template a key renders through."""

    def __init__(self, root: Path):
        self.root = root

    def write(self, relative: str, content: str = "") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return path

    def recipe(self, pkg: str, ver: str = "1.0.0", name: str = "@demo/widget") -> None:
        base = f"packages/{pkg}/{ver}"
        self.write(f"{base}/.stat", STAT.replace("@demo/widget", name))
        self.write(f"{base}/commands/build", "npm install --ignore-scripts\n")
        self.write(f"{base}/commands/test", "file build/Release/*.node\n")

    def scaffold(self) -> None:
        # The real template: the key hashes what rendering produces, so a stub would
        # not exercise the code path the workflow uses.
        self.write(".github/templates/action.template.yml", REAL_TEMPLATE)
        self.write("scripts/render.py", "print('render')\n")
        self.write("docker/toolchain.Dockerfile", "FROM debian:trixie-slim\n")

    def template(self) -> str:
        return (self.root / ".github/templates/action.template.yml").read_text(encoding="utf-8")

    def rewrite_template(self, mutate) -> None:
        path = self.root / ".github/templates/action.template.yml"
        path.write_text(mutate(self.template()), encoding="utf-8")

    def insert_after_step_start(self, step: str, line: str) -> None:
        """Insert ``line`` at the top of a step's ``run:`` block."""

        def mutate(text: str) -> str:
            lines = text.splitlines(keepends=True)
            start = next(
                index for index, value in enumerate(lines) if value.strip() == f"- name: {step}"
            )
            run = next(
                index for index, value in enumerate(lines[start:], start) if value.strip() == "run: |"
            )
            indent = " " * (len(lines[run]) - len(lines[run].lstrip()) + 2)
            lines.insert(run + 1, indent + line + "\n")
            return "".join(lines)

        self.rewrite_template(mutate)


class BuildKeyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = CacheRepo(Path(self._tmp.name))
        self.repo.scaffold()
        self.repo.recipe("demo")

    def tearDown(self):
        self._tmp.cleanup()

    def key(self, pkg: str = "demo", **kwargs) -> str:
        return build_key.build_key(self.repo.root, pkg, "1.0.0", **kwargs)

    def assert_key_changes(self, mutate) -> None:
        before = self.key()
        mutate()
        self.assertNotEqual(before, self.key())

    def assert_key_holds(self, mutate) -> None:
        before = self.key()
        mutate()
        self.assertEqual(before, self.key())

    # --- stability -----------------------------------------------------------

    def test_same_inputs_give_the_same_key(self):
        self.assertEqual(self.key(), self.key())

    def test_key_is_short_hex(self):
        value = self.key()
        self.assertEqual(len(value), 12)
        int(value, 16)

    # --- build inputs invalidate --------------------------------------------

    def test_recipe_build_snippet_change_invalidates(self):
        self.assert_key_changes(
            lambda: self.repo.write("packages/demo/1.0.0/commands/build", "node-gyp rebuild\n")
        )

    def test_upstream_ref_change_invalidates(self):
        self.assert_key_changes(
            lambda: self.repo.write(
                "packages/demo/1.0.0/.stat", STAT.replace("v1.0.0", "v1.0.1")
            )
        )

    def test_common_patch_invalidates(self):
        # the shared baseline sits next to the version directories and applies first
        self.assert_key_changes(
            lambda: self.repo.write("packages/demo/common/patches/0001-common.patch", "diff --git a b\n")
        )

    def test_common_snippet_overridden_by_the_version_keeps_the_key(self):
        # the version layer wins, so the effective build command did not change
        self.assert_key_holds(
            lambda: self.repo.write("packages/demo/common/commands/build", "make\n")
        )

    def test_patch_content_invalidates(self):
        self.repo.write("packages/demo/1.0.0/patches/0001-probe.patch", "diff --git a b\n")
        before = self.key()
        self.repo.write("packages/demo/1.0.0/patches/0001-probe.patch", "diff --git a c\n")
        self.assertNotEqual(before, self.key())

    def test_template_build_command_change_invalidates(self):
        self.assert_key_changes(
            lambda: self.repo.insert_after_step_start("build", "echo probe-build")
        )

    def test_template_fetch_command_change_invalidates(self):
        self.assert_key_changes(
            lambda: self.repo.insert_after_step_start("fetch upstream source", "echo probe-fetch")
        )

    def test_image_id_invalidates(self):
        before = self.key(image_id="sha256:aaaa")
        self.assertNotEqual(before, self.key(image_id="sha256:bbbb"))
        self.assertEqual(self.key(image_id="sha256:aaaa"), before)

    def test_extra_input_invalidates(self):
        before = self.key()
        self.assertNotEqual(before, self.key(extra="--publish-scope demo"))

    # --- edits that cannot change the artifact keep the key ------------------

    def test_template_input_change_keeps_the_key(self):
        self.assert_key_holds(
            lambda: self.repo.rewrite_template(
                lambda text: text.replace(
                    "  publish:\n",
                    '  unused_probe:\n    description: "probe"\n  publish:\n',
                    1,
                )
            )
        )

    def test_template_guard_change_keeps_the_key(self):
        # The step still runs the same commands; only *when* it runs changed.
        def mutate(text: str) -> str:
            return text.replace(
                "    - name: fetch upstream source\n",
                "    - name: fetch upstream source\n      if: inputs.reuse != 'true'\n",
                1,
            )

        self.assert_key_holds(lambda: self.repo.rewrite_template(mutate))

    def test_template_comment_change_keeps_the_key(self):
        self.assert_key_holds(
            lambda: self.repo.rewrite_template(lambda text: text.replace("# 3/5 - build", "# 3/5 - build (probe)"))
        )

    def test_template_test_step_change_keeps_the_key(self):
        self.assert_key_holds(
            lambda: self.repo.insert_after_step_start("test", "echo probe-test")
        )

    def test_template_publish_step_change_keeps_the_key(self):
        self.repo.write(
            "packages/demo/1.0.0/.stat",
            STAT.replace("registry: https://example.invalid", "registry: https://other.invalid"),
        )
        before = self.key()
        self.repo.write(
            "packages/demo/1.0.0/.stat",
            STAT.replace("registry: https://other.invalid", "registry: https://third.invalid"),
        )
        self.assertEqual(before, self.key())

    def test_recipe_test_snippet_change_keeps_the_key(self):
        self.assert_key_holds(
            lambda: self.repo.write("packages/demo/1.0.0/commands/test", "echo other probe\n")
        )

    def test_renderer_and_toolchain_files_keep_the_key(self):
        before = self.key()
        # The key tracks the rendered commands, so an edit that does not change them
        # cannot move it; the image id carries the toolchain.
        self.repo.write("scripts/render.py", "print('changed')\n")
        self.repo.write("docker/toolchain.Dockerfile", "FROM debian:bookworm-slim\n")
        self.assertEqual(before, self.key())

    def test_other_recipe_does_not_invalidate(self):
        before = self.key()
        self.repo.recipe("other", name="@demo/other")
        self.assertEqual(before, self.key())

    def test_pycache_is_ignored(self):
        before = self.key()
        self.repo.write("scripts/__pycache__/render.cpython-314.pyc", "junk")
        self.assertEqual(before, self.key())

    # --- shape and CLI -------------------------------------------------------

    def test_inputs_are_the_rendered_build_steps(self):
        parts = build_key.build_inputs(self.repo.root, "demo", "1.0.0")
        joined = "\n".join(parts)
        self.assertIn("npm install --ignore-scripts", joined)
        self.assertIn("fetch", joined.lower())
        self.assertNotIn("file build/Release/*.node", joined)

    def test_missing_recipe_is_an_error(self):
        with self.assertRaises(build_key.rl.RecipeError):
            self.key(pkg="absent")

    def test_cli_prints_a_key(self):
        self.assertEqual(
            build_key.main(["--pkg", "demo", "--ver", "1.0.0", "--root", str(self.repo.root)]),
            0,
        )

    def test_cli_reports_a_missing_recipe(self):
        self.assertEqual(
            build_key.main(["--pkg", "absent", "--ver", "1.0.0", "--root", str(self.repo.root)]),
            1,
        )


class RealRepositoryKeyTests(unittest.TestCase):
    """The keys of the shipped recipes must track their build inputs."""

    def test_keys_are_stable_for_every_recipe(self):
        for pkg, ver in (("bufferutil", "4.2.0"), ("utf-8-validate", "6.0.6")):
            with self.subTest(pkg=pkg):
                self.assertEqual(
                    build_key.build_key(REPO_ROOT, pkg, ver),
                    build_key.build_key(REPO_ROOT, pkg, ver),
                )

    def test_recipes_have_distinct_keys(self):
        self.assertNotEqual(
            build_key.build_key(REPO_ROOT, "bufferutil", "4.2.0"),
            build_key.build_key(REPO_ROOT, "utf-8-validate", "6.0.6"),
        )

    def test_recipe_build_snippet_is_part_of_the_inputs(self):
        joined = "\n".join(
            build_key.build_inputs(REPO_ROOT, "bufferutil", "4.2.0")
        )
        self.assertIn("node-gyp rebuild", joined)


class PublishKeyTests(unittest.TestCase):
    """The publish key answers a different question than the build key.

    It decides whether an already-built tree has to be delivered again after only the
    delivery side changed - the case that went unnoticed when a fixed publish step
    triggered nothing at all.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = CacheRepo(Path(self._tmp.name))
        self.repo.scaffold()
        self.repo.write("scripts/publish_manifest.mjs", "// helper\n")
        self.repo.recipe("demo")

    def tearDown(self):
        self._tmp.cleanup()

    def publish_key(self, pkg: str = "demo") -> str:
        return build_key.publish_key(self.repo.root, pkg, "1.0.0")

    def build_key(self, pkg: str = "demo") -> str:
        return build_key.build_key(self.repo.root, pkg, "1.0.0")

    def test_publish_key_ignores_the_build_side(self):
        before = self.publish_key()
        self.repo.write("packages/demo/1.0.0/commands/build", "npm run build:other\n")
        self.assertNotEqual(before, self.build_key())
        self.assertEqual(before, self.publish_key())

    def test_publish_key_follows_the_target_registry(self):
        # Where a package is published cannot change the artifact, so the build key
        # holds while the publish key moves - that is exactly the narrowing the
        # detector relies on.
        before_build = self.build_key()
        before_publish = self.publish_key()
        self.repo.write(
            "packages/demo/1.0.0/.stat",
            STAT.replace("registry: https://example.invalid", "registry: https://other.invalid"),
        )
        self.assertEqual(before_build, self.build_key())
        self.assertNotEqual(before_publish, self.publish_key())

    def test_publish_key_changes_when_the_publish_step_text_changes(self):
        before = self.publish_key()
        self.repo.rewrite_template(
            lambda text: text.replace('bash "{{steps.dir}}/steps/publish.sh"', 'echo probe')
        )
        self.assertNotEqual(before, self.publish_key())

    def test_publish_key_ignores_the_test_step(self):
        before = self.publish_key()
        self.repo.rewrite_template(
            lambda text: text.replace("bash \"{{steps.dir}}/steps/test.sh\"", "true")
        )
        self.assertEqual(before, self.publish_key())

    def test_publish_key_follows_the_helper(self):
        before = self.publish_key()
        self.repo.write("scripts/publish_manifest.mjs", "// helper, changed\n")
        self.assertNotEqual(before, self.publish_key())


if __name__ == "__main__":
    unittest.main()
