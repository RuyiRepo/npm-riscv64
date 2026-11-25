"""Renderer tests (L0: static/unit level).

The fixtures are built in a temporary directory instead of being committed:
each test states the recipe it needs, which keeps intent visible and avoids a
second copy of the recipe format drifting away from docs/recipe-spec.md.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import recipe_lib as rl  # noqa: E402
import render  # noqa: E402

REAL_TEMPLATE = REPO_ROOT / ".github/templates/action.template.yml"

STAT_TEMPLATE = """\
upstream:
  repo: https://github.com/example/demo
  ref: v{ver}

artifacts:
  path: build/Release/*.node

npm_publish:
  - role: main
    name: "{name}"
    version: {ver}
    dir: .
    generate_manifest: true
"""


class TempRepo:
    """Helper that materialises a small recipe tree on disk."""

    def __init__(self, root: Path):
        self.root = root
        self.packages = root / "packages"

    def _write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return path

    def add_recipe(
        self,
        pkg: str,
        ver: str,
        *,
        stat: str | None = None,
        build: str | None = "echo build",
        test: str | None = "echo test",
        patches: dict[str, str] | None = None,
        disable: list[str] | None = None,
        common_build: str | None = None,
        common_patches: dict[str, str] | None = None,
    ) -> Path:
        pkg_dir = self.packages.joinpath(*pkg.split("/"))
        recipe = pkg_dir / ver
        recipe.mkdir(parents=True, exist_ok=True)
        if stat is not False:
            (recipe / rl.STAT_FILE).write_text(
                textwrap.dedent(stat or STAT_TEMPLATE.format(ver=ver, name=f"@demo/{pkg.split('/')[-1]}")),
                encoding="utf-8",
            )
        if build is not None:
            (recipe / "commands").mkdir(exist_ok=True)
            (recipe / "commands" / "build").write_text(build + "\n", encoding="utf-8")
        if test is not None:
            (recipe / "commands").mkdir(exist_ok=True)
            (recipe / "commands" / "test").write_text(test + "\n", encoding="utf-8")
        for name, content in (patches or {}).items():
            (recipe / "patches").mkdir(exist_ok=True)
            (recipe / "patches" / name).write_text(content, encoding="utf-8")
        if disable is not None:
            (recipe / "patches").mkdir(exist_ok=True)
            (recipe / "patches" / rl.DISABLE_FILE).write_text(
                "\n".join(disable) + "\n", encoding="utf-8"
            )

        common = pkg_dir / rl.COMMON_DIR
        if common_build is not None:
            (common / "commands").mkdir(parents=True, exist_ok=True)
            (common / "commands" / "build").write_text(common_build + "\n", encoding="utf-8")
        for name, content in (common_patches or {}).items():
            (common / "patches").mkdir(parents=True, exist_ok=True)
            (common / "patches" / name).write_text(content, encoding="utf-8")
        return recipe


class EngineTests(unittest.TestCase):
    def test_multiline_values_are_reindented(self):
        template = "run: |\n  {{script}}\n"
        rendered, missing, _ = render.substitute(template, {"script": "line1\nline2\nline3"})
        self.assertEqual(rendered, "run: |\n  line1\n  line2\n  line3\n")
        self.assertEqual(missing, set())

    def test_blank_lines_in_value_are_not_indented(self):
        rendered, _, _ = render.substitute("a: |\n  {{v}}\n", {"v": "one\n\ntwo"})
        self.assertEqual(rendered, "a: |\n  one\n\n  two\n")

    def test_unresolved_placeholder_fails(self):
        with self.assertRaises(render.RenderError):
            render.render_template("x: {{nope}}\n", {})

    def test_conditionals_if_and_unless(self):
        template = textwrap.dedent(
            """\
            keep-me
            {{#if flag}}
            if-body
            {{/if}}
            {{#unless flag}}
            unless-body
            {{/unless}}
            """
        )
        on = render.apply_conditionals(template, {"flag": True})
        self.assertIn("if-body", on)
        self.assertNotIn("unless-body", on)
        off = render.apply_conditionals(template, {"flag": False})
        self.assertNotIn("if-body", off)
        self.assertIn("unless-body", off)

    def test_nested_conditionals(self):
        template = "{{#if a}}\nA\n{{#if b}}\nB\n{{/if}}\n{{/if}}\n"
        self.assertEqual(render.apply_conditionals(template, {"a": True, "b": False}).count("B"), 0)
        self.assertEqual(render.apply_conditionals(template, {"a": True, "b": True}).count("B"), 1)
        self.assertEqual(render.apply_conditionals(template, {"a": False, "b": True}).count("B"), 0)

    def test_unclosed_conditional_fails(self):
        with self.assertRaises(render.RenderError):
            render.apply_conditionals("{{#if a}}\n", {"a": True})

    def test_mismatched_close_fails(self):
        with self.assertRaises(render.RenderError):
            render.apply_conditionals("{{#if a}}\n{{/unless}}\n", {"a": True})

    def test_conditional_on_unknown_variable_fails(self):
        with self.assertRaises(render.RenderError):
            render.apply_conditionals("{{#if missing}}\n{{/if}}\n", {})

    def test_repeated_blank_lines_are_collapsed(self):
        rendered = render.render_template("a:\n\n\n\nb:\n", {})
        self.assertEqual(rendered, "a:\n\nb:\n")


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TempRepo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, pkg, ver, overrides=None, template=None, **extra):
        return render.render_target(
            self.repo.packages,
            pkg,
            ver,
            Path(template or REAL_TEMPLATE),
            overrides or {},
            **extra,
        )

    def test_reuse_guard_skips_fetch_patch_and_build_only(self):
        # A cached tree means the workspace already holds the source and artifact,
        # so exactly the three producing steps are guarded; test must still run
        # against a restored artifact (D16).
        self.repo.add_recipe("demo", "1.0.0", patches={"fix.patch": "diff\n"})
        rendered, _ = self.render("demo", "1.0.0")
        action = yaml.safe_load(rendered)
        steps = {step.get("name"): step for step in action["runs"]["steps"]}
        for name in ("fetch upstream source", "apply patches", "build"):
            self.assertEqual(
                steps[name].get("if"),
                "inputs.reuse != 'true'",
                f"{name} must be skipped when the tree came from the cache",
            )
        # The test step is the one that must NOT be skipped on a cache hit, but it is
        # skipped when the run only re-delivers a cached tree (publish_only).
        self.assertEqual(steps["test"].get("if"), "inputs.publish_only != 'true'")
        self.assertEqual(steps["publish"].get("if"), "inputs.publish == 'true'")
        self.assertEqual(action["inputs"]["reuse"].get("default"), "false")
        self.assertEqual(action["inputs"]["publish_only"].get("default"), "false")

    def test_publish_only_refuses_to_run_without_a_cached_tree(self):
        # Publishing a workspace that was never restored would ship a wrong package,
        # so the combination fails loudly instead of publishing whatever is there.
        self.repo.add_recipe("demo", "1.0.0")
        rendered, _ = self.render("demo", "1.0.0")
        action = yaml.safe_load(rendered)
        steps = {step.get("name"): step for step in action["runs"]["steps"]}
        guard = steps["require the cached tree for publish-only"]
        self.assertEqual(
            guard.get("if"),
            "inputs.publish == 'true' && inputs.publish_only == 'true' && inputs.reuse != 'true'",
        )
        steps_before_publish = [
            step.get("name") for step in action["runs"]["steps"]
        ]
        self.assertLess(
            steps_before_publish.index("require the cached tree for publish-only"),
            steps_before_publish.index("publish"),
        )

    def test_scoped_slug_and_artifact_name(self):
        self.repo.add_recipe("@demo/pkg", "1.2.3")
        target = rl.find_target(self.repo.packages, "@demo/pkg", "1.2.3")
        self.assertEqual(target.slug_pkg, "demo-pkg")
        self.assertEqual(target.artifact_name, "demo-pkg-1.2.3-riscv64")

    def test_render_produces_valid_composite_action(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        rendered, scripts = self.render("@demo/pkg", "1.0.0")
        import yaml

        data = yaml.safe_load(rendered)
        self.assertEqual(data["runs"]["using"], "composite")
        names = [step.get("name") for step in data["runs"]["steps"]]
        self.assertIn("build", names)
        self.assertIn("test", names)
        self.assertIn("publish", names)
        # no patches in this recipe -> the patch step must be gone, not empty
        self.assertNotIn("apply patches", names)
        self.assertNotIn("patch", scripts)
        self.assertEqual(sorted(scripts), ["build", "fetch", "publish", "test"])

    def test_required_snippet_missing_is_rejected(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0", test=None)
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("required command snippet 'test'", str(ctx.exception))

    def test_missing_stat_is_rejected(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=False)
        with self.assertRaises(rl.RecipeError):
            self.render("@demo/pkg", "1.0.0")

    def test_source_route_requires_sha256(self):
        stat = STAT_TEMPLATE.format(ver="1.0.0", name="@demo/pkg") + textwrap.dedent(
            """\
            source:
              url: https://example.com/demo-1.0.0.tar.gz
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("source.sha256 is required", str(ctx.exception))

    def test_mutable_ref_is_rejected(self):
        stat = STAT_TEMPLATE.format(ver="1.0.0", name="@demo/pkg").replace("v1.0.0", "main")
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("mutable reference", str(ctx.exception))

    def test_unknown_snippet_name_is_rejected(self):
        recipe = self.repo.add_recipe("@demo/pkg", "1.0.0")
        (recipe / "commands" / "biuld").write_text("echo typo\n", encoding="utf-8")
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("unknown command snippet 'biuld'", str(ctx.exception))

    # ---------------------------------------------------------- toolchain kind
    def _cross_stat(self, ver="1.0.0"):
        return STAT_TEMPLATE.format(ver=ver, name="@demo/pkg") + "toolchain: cross\n"

    def test_native_is_the_default_toolchain(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        rendered, _ = self.render("@demo/pkg", "1.0.0")
        action = yaml.safe_load(rendered)
        steps = {step.get("name"): step["run"] for step in action["runs"]["steps"]}
        # Everything runs on riscv64, and nothing mentions the cross toolchain.
        for name in ("fetch upstream source", "build", "test"):
            self.assertIn("--platform linux/riscv64", steps[name], name)
            self.assertIn("npm-riscv64/toolchain:riscv64", steps[name], name)
        # No *executed* amd64 platform anywhere (a comment may name it).
        self.assertNotIn("--platform linux/amd64", rendered)
        self.assertNotIn('pull_image "linux/amd64"', rendered)
        self.assertNotIn("riscv64-linux-gnu-gcc", rendered)
        # Both images of a native recipe are the riscv64 one, pulled by platform.
        self.assertIn('pull_image "linux/riscv64" "$image"', steps["prepare toolchain"])
        self.assertIn('pull_image "linux/riscv64" "$test_image"', steps["prepare toolchain"])
        self.assertNotIn("RISCV_TARGET", rendered)

    def test_cross_recipe_builds_on_amd64_and_tests_on_riscv64(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=self._cross_stat())
        rendered, _ = self.render("@demo/pkg", "1.0.0")
        action = yaml.safe_load(rendered)
        steps = {step.get("name"): step["run"] for step in action["runs"]["steps"]}
        # The producing steps run on the host architecture with the cross toolchain,
        # and the target triple is handed to the recipe.
        for name in ("fetch upstream source", "build"):
            self.assertIn("--platform linux/amd64", steps[name], name)
            self.assertIn("npm-riscv64/toolchain:cross", steps[name], name)
        self.assertIn("-e RISCV_TARGET=riscv64gc-unknown-linux-gnu", steps["build"])
        # The test step still runs on riscv64, on the native image.
        self.assertIn("--platform linux/riscv64", steps["test"])
        self.assertIn(
            'image="${RISCV_TEST_IMAGE:-${RISCV_IMAGE:-npm-riscv64/toolchain:riscv64}}"',
            steps["test"],
        )
        # The prepare step proves the cross toolchain works before anything else runs.
        self.assertIn("riscv64-linux-gnu-gcc --version", steps["prepare toolchain"])
        self.assertIn("docker/riscv64-cross.Dockerfile", steps["prepare toolchain"])
        # Each published image is single-platform, so a pull that does not name the
        # platform fails on the runner with "no matching manifest for linux/amd64".
        self.assertIn('pull_image "linux/amd64" "$image"', steps["prepare toolchain"])
        self.assertIn('pull_image "linux/riscv64" "$test_image"', steps["prepare toolchain"])

    def test_cross_patch_step_runs_on_the_build_platform(self):
        # Patches are applied with git inside the build container; they are not
        # riscv64-specific, so they belong to the build side of the cross split.
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=self._cross_stat(), patches={"fix.patch": "diff\n"})
        rendered, _ = self.render("@demo/pkg", "1.0.0")
        action = yaml.safe_load(rendered)
        steps = {step.get("name"): step["run"] for step in action["runs"]["steps"]}
        self.assertIn("--platform linux/amd64", steps["apply patches"])

    def test_unknown_toolchain_is_rejected(self):
        self.repo.add_recipe(
            "@demo/pkg",
            "1.0.0",
            stat=STAT_TEMPLATE.format(ver="1.0.0", name="@demo/pkg") + "toolchain: zig\n",
        )
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("toolchain 'zig' is not supported", str(ctx.exception))

    def test_version_snippet_overrides_common(self):
        self.repo.add_recipe(
            "@demo/pkg",
            "1.0.0",
            build="echo version-build",
            common_build="echo common-build",
        )
        _, scripts = self.render("@demo/pkg", "1.0.0")
        self.assertIn("version-build", scripts["build"])
        self.assertNotIn("common-build", scripts["build"])

    def test_version_inherits_common_snippet(self):
        self.repo.add_recipe(
            "@demo/pkg", "1.0.0", build=None, common_build="echo common-build"
        )
        _, scripts = self.render("@demo/pkg", "1.0.0")
        self.assertIn("common-build", scripts["build"])

    def test_patch_order_common_first_then_version(self):
        self.repo.add_recipe(
            "@demo/pkg",
            "1.0.0",
            patches={"0002-version.patch": "version patch\n"},
            common_patches={"0001-common.patch": "common patch\n"},
        )
        _, scripts = self.render("@demo/pkg", "1.0.0")
        body = scripts["patch"]
        self.assertLess(body.index("0001-common.patch"), body.index("0002-version.patch"))

    def test_disable_removes_common_patch(self):
        self.repo.add_recipe(
            "@demo/pkg",
            "1.0.0",
            patches={"0002-version.patch": "version patch\n"},
            common_patches={"0001-common.patch": "common patch\n"},
            disable=["0001-common.patch"],
        )
        _, scripts = self.render("@demo/pkg", "1.0.0")
        self.assertNotIn("0001-common.patch", scripts["patch"])
        self.assertIn("0002-version.patch", scripts["patch"])

    def test_dotfiles_are_not_patches(self):
        recipe = self.repo.add_recipe("@demo/pkg", "1.0.0", patches={"0001-a.patch": "a\n"})
        (recipe / "patches" / ".keep").write_text("", encoding="utf-8")
        _, scripts = self.render("@demo/pkg", "1.0.0")
        self.assertNotIn(".keep", scripts["patch"])

    def test_unquoted_scoped_name_is_caught(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        template = Path(self.tmp.name) / "bad-template.yml"
        template.write_text(
            "name: {{pkg.name}}\ndescription: x\nruns:\n  using: composite\n"
            "  steps:\n    - name: fetch\n      shell: bash\n      run: |\n        true\n"
            "    - name: build\n      shell: bash\n      run: |\n        true\n"
            "    - name: test\n      shell: bash\n      run: |\n        true\n"
            "    - name: publish\n      shell: bash\n      run: |\n        true\n",
            encoding="utf-8",
        )
        # `@demo/...` at the start of a scalar is not valid YAML: the quote rule
        # documented in docs/recipe-spec.md is enforced, not assumed.
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0", template=template)
        self.assertIn("not valid YAML", str(ctx.exception))

    def test_override_replaces_registry(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        _, scripts = self.render(
            "@demo/pkg",
            "1.0.0",
            overrides={"npm_publish.0.registry": "http://127.0.0.1:4873"},
        )
        self.assertIn("http://127.0.0.1:4873", scripts["publish"])

    def test_fetch_chain_covers_only_configured_routes(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        _, scripts = self.render("@demo/pkg", "1.0.0")
        fetch = scripts["fetch"]
        self.assertIn("route upstream", fetch)
        self.assertIn("not configured", fetch)  # source/npm stubs still present
        self.assertIn("fetch_upstream", fetch)

    def test_publish_order_puts_binary_first(self):
        stat = textwrap.dedent(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0

            artifacts:
              path: build/Release/*.node

            npm_publish:
              - role: main
                name: "@demo/main"
                version: 1.0.0
                dir: .
                requires: ["@demo/binding-linux-riscv64-gnu"]
                generate_manifest: true
              - role: binary
                name: "@demo/binding-linux-riscv64-gnu"
                version: 1.0.0
                dir: prebuilds-package
                os: linux
                cpu: riscv64
                generate_manifest: true
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        _, scripts = self.render("@demo/pkg", "1.0.0")
        publish = scripts["publish"]
        self.assertLess(
            publish.index("@demo/binding-linux-riscv64-gnu"),
            publish.index("@demo/main"),
        )
        self.assertIn("generate", publish)

    def test_publish_scope_rewrites_names_and_requires(self):
        stat = textwrap.dedent(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0

            artifacts:
              path: build/Release/*.node

            npm_publish:
              - role: main
                name: "@demo/main"
                version: 1.0.0
                dir: .
                requires: ["@demo/binding-linux-riscv64-gnu"]
              - role: binary
                name: "@demo/binding-linux-riscv64-gnu"
                version: 1.0.0
                dir: pkg
                os: linux
                cpu: riscv64
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        rendered, scripts = self.render("@demo/pkg", "1.0.0", publish_scope="cannedfish")
        publish = scripts["publish"]
        self.assertIn("@cannedfish/main", publish)
        self.assertIn("@cannedfish/binding-linux-riscv64-gnu", publish)
        self.assertNotIn("@demo/main", publish)
        self.assertNotIn("@demo/binding-linux-riscv64-gnu", publish)
        # the dependency follows the rename, otherwise validation would refuse
        # the recipe (requires must name a package of the same recipe)
        self.assertIn('"@cannedfish/binding-linux-riscv64-gnu"', publish)
        self.assertLess(
            publish.index("@cannedfish/binding-linux-riscv64-gnu"),
            publish.index("@cannedfish/main"),
        )

    def test_publish_scope_accepts_leading_at_and_unscoped_names(self):
        stat = textwrap.dedent(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0

            artifacts:
              path: build/*.node

            npm_publish:
              - role: main
                name: plain-name
                version: 1.0.0
                dir: .
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        _, scripts = self.render("@demo/pkg", "1.0.0", publish_scope="@CannedFish/")
        self.assertIn("@CannedFish/plain-name", scripts["publish"])

    def test_publish_scope_rejects_an_empty_scope(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0", publish_scope="@/")
        self.assertIn("non-empty scope", str(ctx.exception))

    def test_publish_registry_applies_to_every_entry(self):
        stat = textwrap.dedent(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0

            artifacts:
              path: build/*.node

            npm_publish:
              - role: main
                name: "@demo/main"
                version: 1.0.0
                dir: .
                registry: https://registry.npmjs.org
              - role: binary
                name: "@demo/binding"
                version: 1.0.0
                dir: pkg
                os: linux
                cpu: riscv64
                registry: https://npm.pkg.github.com
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        _, scripts = self.render(
            "@demo/pkg", "1.0.0", publish_registry="http://127.0.0.1:4873"
        )
        publish = scripts["publish"]
        # one setup_auth call plus one publish_one line per entry
        self.assertEqual(publish.count("http://127.0.0.1:4873"), 3)
        self.assertNotIn("npm.pkg.github.com", publish)

    def test_publish_requires_os_and_cpu_for_binary(self):
        stat = textwrap.dedent(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0

            artifacts:
              path: build/*.node

            npm_publish:
              - role: binary
                name: "@demo/binding"
                version: 1.0.0
                dir: pkg
              - role: main
                name: "@demo/main"
                version: 1.0.0
                dir: .
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("requires os and cpu", str(ctx.exception))

    def test_publish_version_must_match_directory(self):
        stat = STAT_TEMPLATE.format(ver="1.0.0", name="@demo/pkg").replace(
            "version: 1.0.0", "version: 9.9.9"
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("does not match the recipe version directory", str(ctx.exception))

    def test_requires_version_drift_is_rejected(self):
        stat = textwrap.dedent(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0

            artifacts:
              path: build/*.node

            npm_publish:
              - role: binary
                name: "@demo/binding"
                version: 1.0.0
                dir: pkg
                os: linux
                cpu: riscv64
              - role: main
                name: "@demo/main"
                version: 2.0.0
                dir: .
                requires: ["@demo/binding"]
            """
        )
        self.repo.add_recipe("@demo/pkg", "1.0.0", stat=stat)
        with self.assertRaises(render.RenderError) as ctx:
            self.render("@demo/pkg", "1.0.0")
        self.assertIn("does not match required package", str(ctx.exception))


class RepositoryStructureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TempRepo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_package_directory_is_reported(self):
        (self.repo.packages / "empty").mkdir(parents=True)
        problems = rl.repository_problems(self.repo.packages)
        self.assertTrue(any("no version subdirectory" in p for p in problems), problems)

    def test_version_directory_without_stat_is_reported(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        (self.repo.packages / "@demo/pkg/2.0.0").mkdir()
        problems = rl.repository_problems(self.repo.packages)
        self.assertTrue(any("missing .stat" in p for p in problems), problems)

    def test_stray_file_in_package_directory_is_reported(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        (self.repo.packages / "@demo/pkg/notes.txt").write_text("x", encoding="utf-8")
        problems = rl.repository_problems(self.repo.packages)
        self.assertTrue(any("unexpected file" in p for p in problems), problems)

    def test_clean_tree_has_no_problems(self):
        self.repo.add_recipe("@demo/pkg", "1.0.0")
        self.repo.add_recipe("@demo/pkg", "2.0.0")
        self.assertEqual(rl.repository_problems(self.repo.packages), [])


class PublishAuthTests(unittest.TestCase):
    """The generated publish step must send the credential the registry wants."""

    def _publish(self, publish_entries, recipe_dir: str = "packages/demo/1.0.0"):
        recipe = {
            "upstream": {"repo": "https://github.com/websockets/bufferutil", "ref": "v4.2.0"},
            "artifacts": {"path": "prebuilds/linux-riscv64/*.node"},
            "npm_publish": publish_entries,
        }
        target = rl.Target(pkg="demo", ver="1.0.0", path=Path("packages/demo/1.0.0"))
        return render.generate_publish(
            recipe, target, "prebuilds/linux-riscv64/*.node", Path(recipe_dir)
        )

    def _entry(self, **extra):
        entry = {
            "role": "main",
            "name": "demo",
            "version": "1.0.0",
            "dir": ".",
            "registry": "https://registry.example.net/repository/npm-local",
            "token_env": "NODE_AUTH_TOKEN",
        }
        entry.update(extra)
        return entry

    def test_basic_auth_writes_the_auth_line_keyed_by_the_full_url(self):
        script = self._publish([self._entry(auth="basic")])
        # The registry is normalized to end with a slash: npm derives the credential
        # key from new URL(".", registryUrl), so without it npm drops the last path
        # segment and refuses with ENEEDAUTH before sending anything.
        self.assertIn('setup_auth "https://registry.example.net/repository/npm-local/" '
                      '"NODE_AUTH_TOKEN" "basic"', script)
        self.assertIn('"registry": "https://registry.example.net/repository/npm-local/"',
                      script)
        self.assertIn('_auth=${%s}', script)
        self.assertIn('//host/repository/name/', script)  # explains the key shape
        self.assertIn('dart="${registry#*://}"', script)

    def test_token_is_the_default_and_unknown_kinds_are_refused(self):
        script = self._publish([self._entry()])
        self.assertIn('"NODE_AUTH_TOKEN" "token"', script)
        target = rl.Target(pkg="demo", ver="1.0.0", path=Path("packages/demo/1.0.0"))
        recipe = {
            "upstream": {"repo": "https://github.com/x/y", "ref": "v1.0.0"},
            "artifacts": {"path": "*.node"},
            "npm_publish": [self._entry(auth="bearer")],
        }
        problems = rl.validate_stat(target, recipe)
        self.assertTrue(any("auth 'bearer'" in problem for problem in problems))

    def test_registry_url_is_normalized_to_a_directory(self):
        self.assertEqual(
            render.normalize_registry("https://host.example.net/repository/npm-local"),
            "https://host.example.net/repository/npm-local/",
        )
        self.assertEqual(
            render.normalize_registry("https://host.example.net/repository/npm-local/"),
            "https://host.example.net/repository/npm-local/",
        )
        self.assertEqual(render.normalize_registry("https://registry.npmjs.org"),
                         "https://registry.npmjs.org/")

    def test_publish_output_is_streamed_with_request_timing(self):
        script = self._publish([self._entry()])
        # A captured npm prints nothing until it exits, which reads as "publish hangs"
        # when the upload is slow; tee keeps the log moving.
        self.assertIn('tee "$out_file"', script)
        self.assertIn("--loglevel http", script)
        self.assertIn("status=${PIPESTATUS[0]}", script)

    def test_the_upload_budget_is_derived_and_passed_to_npm(self):
        script = self._publish([self._entry()])
        # npm's fetch-timeout is a wall-clock budget for the whole request, upload
        # included, so the default 300 s caps a package by the route's speed; the step
        # derives it from the packed size and says what it chose.
        self.assertIn("--plan \"$spec_json\" \"$publish_dir\"", script)
        self.assertIn("upload $packed_bytes bytes, fetch timeout ${fetch_timeout}ms", script)
        self.assertIn('--fetch-timeout "$fetch_timeout"', script)
        # Retries cost a full re-upload, so a large package retries once.
        self.assertIn('--fetch-retries "$fetch_retries"', script)

    def test_idempotency_trusts_the_registry_answer_not_a_lookup(self):
        script = self._publish([self._entry()])
        # A lookup would be fooled by a proxy/group registry serving upstream versions.
        self.assertNotIn("$(npm view", script)
        self.assertIn("EPUBLISHCONFLICT", script)
        self.assertIn("already published: $P_NAME@$P_VERSION", script)


class PublishSourceTests(unittest.TestCase):
    """How a publish entry turns into files, and in which order packages go out.

    The rule from .dsh/plan.md D33: a published package keeps upstream's structure and
    carries one riscv64 increment. Two shapes exist, and each is checked here.
    """

    def _script(self, entries, recipe_dir: str = "packages/demo/1.0.0",
                files: dict[str, str] | None = None):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        for relative, content in (files or {}).items():
            path = root / "packages/demo/1.0.0" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        recipe = {
            "upstream": {"repo": "https://github.com/example/demo", "ref": "v1.0.0"},
            "artifacts": {"path": "*.node"},
            "npm_publish": entries,
        }
        target = rl.Target(pkg="demo", ver="1.0.0", path=root / recipe_dir)
        return render.generate_publish(recipe, target, "*.node", root / recipe_dir)

    def _specs(self, script: str) -> list[dict]:
        bodies = script.split("publish_pkg <<'PUBLISH_SPEC'\n")[1:]
        return [json.loads(body.split("\nPUBLISH_SPEC")[0]) for body in bodies]

    def test_source_tree_kind_is_the_default(self):
        spec = self._specs(self._script([{
            "role": "main", "name": "demo", "version": "1.0.0", "dir": ".",
        }]))[0]
        self.assertEqual(spec["kind"], "source-tree")
        self.assertEqual(spec["manifest"], "upstream")
        self.assertEqual(spec["dir"], ".")

    def test_generate_manifest_is_the_older_spelling_of_manifest_shape(self):
        spec = self._specs(self._script([{
            "role": "main", "name": "demo", "version": "1.0.0", "dir": ".",
            "generate_manifest": True,
        }]))[0]
        self.assertEqual(spec["manifest"], "generated")

    def test_base_kind_is_the_documented_spelling(self):
        # The plan spells the kind inside base: (.dsh/plan.md D33); a top-level kind is
        # the shorter form and must not disagree with it.
        spec = self._specs(self._script([{
            "role": "main", "name": "demo", "version": "1.0.0", "dir": ".",
            "base": {
                "kind": "upstream-tarball",
                "url": "https://registry.npmjs.org/demo/-/demo-1.0.0.tgz",
                "integrity": "sha512-abc",
            },
            "overlay": ["prebuilds/linux-riscv64"],
        }]))[0]
        self.assertEqual(spec["kind"], "upstream-tarball")
        self.assertFalse(spec["allow_overwrite"])

    def test_allow_overwrite_reaches_the_script(self):
        script = self._script([{
            "role": "main", "name": "demo", "version": "1.0.0", "dir": ".",
            "base": {"kind": "upstream-tarball",
                     "url": "https://registry.npmjs.org/demo/-/demo-1.0.0.tgz",
                     "integrity": "sha512-abc"},
            "overlay": ["prebuilds/linux-riscv64"],
            "allow_overwrite": True,
        }])
        self.assertIn('"allow_overwrite": true', script)

    def test_upstream_tarball_carries_the_base_and_the_overlay(self):
        spec = self._specs(self._script([{
            "role": "main", "name": "demo", "version": "1.0.0", "dir": ".",
            "kind": "upstream-tarball",
            "base": {
                "url": "https://registry.npmjs.org/demo/-/demo-1.0.0.tgz",
                "integrity": "sha512-abc",
            },
            "overlay": ["prebuilds/linux-riscv64"],
        }]))[0]
        self.assertEqual(spec["kind"], "upstream-tarball")
        self.assertEqual(spec["base"]["url"],
                         "https://registry.npmjs.org/demo/-/demo-1.0.0.tgz")
        self.assertEqual(spec["overlay"], ["prebuilds/linux-riscv64"])

    def test_platform_packages_are_published_before_the_main_package(self):
        script = self._script([
            {
                "role": "main", "name": "@demo/wrapper", "version": "1.0.0", "dir": "out/wrapper",
                "manifest_shape": "upstream",
                "optional_dependencies": ["@demo/binding-linux-riscv64-gnu"],
            },
            {
                "role": "binding", "name": "@demo/binding-linux-riscv64-gnu",
                "version": "1.0.0", "dir": "out/binding", "os": "linux", "cpu": "riscv64",
                "libc": "glibc",
            },
        ])
        specs = self._specs(script)
        self.assertEqual([spec["name"] for spec in specs],
                         ["@demo/binding-linux-riscv64-gnu", "@demo/wrapper"])
        # The main package names the platform package it depends on, by version.
        self.assertEqual(specs[1]["optional"],
                         {"@demo/binding-linux-riscv64-gnu": "1.0.0"})
        self.assertEqual(specs[0]["os"], "linux")
        self.assertEqual(specs[0]["cpu"], "riscv64")
        self.assertEqual(specs[0]["libc"], "glibc")

    def test_manifest_template_is_resolved_at_render_time(self):
        template = json.dumps({
            "name": "{{name}}",
            "version": "{{version}}",
            "cpu": ["{{cpu}}"],
            "os": ["{{os}}"],
            "libc": ["{{libc}}"],
            "main": "{{main}}",
            "files": ["{{main}}"],
        })
        script = self._script(
            [{
                "role": "binding", "name": "@demo/binding-linux-riscv64-gnu",
                "version": "1.0.0", "dir": "out/binding", "os": "linux", "cpu": "riscv64",
                "libc": "glibc", "main": "demo.node",
                "manifest_shape": "template",
                "manifest_template": "templates/package.json",
            }],
            files={"templates/package.json": template},
        )
        spec = self._specs(script)[0]
        self.assertEqual(spec["manifest"], "template")
        self.assertEqual(spec["manifest_json"]["name"], "@demo/binding-linux-riscv64-gnu")
        self.assertEqual(spec["manifest_json"]["cpu"], ["riscv64"])
        self.assertEqual(spec["manifest_json"]["main"], "demo.node")

    def test_readme_is_carried_into_the_spec(self):
        script = self._script(
            [{
                "role": "binding", "name": "@demo/binding-linux-riscv64-gnu",
                "version": "1.0.0", "dir": "out/binding", "os": "linux", "cpu": "riscv64",
                "manifest_shape": "template",
                "manifest_template": "templates/package.json",
                "readme": "templates/README.md",
            }],
            files={
                "templates/package.json": json.dumps({"name": "{{name}}", "version": "{{version}}"}),
                "templates/README.md": "# `{{name}}`\n\nThis is the riscv64 binary.\n",
            },
        )
        spec = self._specs(script)[0]
        self.assertIn("@demo/binding-linux-riscv64-gnu", spec["readme_text"])


class ManifestGenerationTests(unittest.TestCase):
    """What the publish helper does to a manifest, per manifest shape.

    One rule here is not cosmetic: npm prefers publishConfig.registry over --registry,
    so a manifest that pins its own registry would publish somewhere the recipe did
    not ask for. Every shape realigns it, because the registry is the recipe's decision.
    """

    NODE = shutil.which("node")
    HELPER = REPO_ROOT / "scripts/publish_manifest.mjs"

    def run_helper(self, spec: dict, *args: str, directory: Path | None = None) -> subprocess.CompletedProcess:
        spec_file = Path(self.tmp.name) / "spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        return subprocess.run(
            [self.NODE, str(self.HELPER), *args[:1], str(spec_file), *args[1:]],
            capture_output=True,
            text=True,
        )

    def prepare(self, spec: dict, directory: Path | None = None) -> subprocess.CompletedProcess:
        return self.run_helper(spec, "--prepare", str(directory or self.dir))

    def setUp(self):
        if not self.NODE:
            self.skipTest("node is required to exercise the manifest helper")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "pkg"
        self.dir.mkdir()

    def write_manifest(self, manifest: dict) -> None:
        (self.dir / "package.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                               encoding="utf-8")

    def manifest(self, directory: Path | None = None) -> dict:
        return json.loads((Path(directory or self.dir) / "package.json").read_text(encoding="utf-8"))

    def spec(self, **extra) -> dict:
        spec = {
            "name": "@demo/widget",
            "version": "1.0.0",
            "registry": "https://example.invalid",
            "dir": ".",
            "kind": "source-tree",
            "manifest": "generated",
        }
        spec.update(extra)
        return spec

    def test_generate_realigns_a_pinned_registry(self):
        # An upstream manifest that pins npmjs.org must not win over the recipe.
        self.write_manifest({"name": "widget", "version": "0.0.0", "files": ["dist"],
                             "publishConfig": {"access": "public",
                                               "registry": "https://registry.npmjs.org/"}})
        result = self.prepare(self.spec())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.manifest()["name"], "@demo/widget")
        self.assertEqual(self.manifest()["version"], "1.0.0")
        self.assertEqual(self.manifest()["publishConfig"]["registry"], "https://example.invalid")
        self.assertEqual(self.manifest()["publishConfig"]["access"], "public")
        self.assertIn("publishConfig.registry", result.stdout)

    def test_generate_keeps_an_equal_registry_untouched(self):
        self.write_manifest({"name": "@demo/widget", "version": "1.0.0",
                             "publishConfig": {"registry": "https://example.invalid"}})
        result = self.prepare(self.spec())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("publishConfig.registry", result.stdout)

    def test_generate_writes_the_platform_fields(self):
        self.write_manifest({"name": "@demo/widget", "version": "1.0.0"})
        result = self.prepare(self.spec(main="widget.node", os="linux", cpu="riscv64",
                                        libc="glibc", engines={"node": "^20.19.0 || >=22.12.0"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.manifest()
        self.assertEqual(manifest["main"], "widget.node")
        self.assertEqual(manifest["os"], ["linux"])
        self.assertEqual(manifest["cpu"], ["riscv64"])
        self.assertEqual(manifest["libc"], ["glibc"])
        self.assertEqual(manifest["engines"], {"node": "^20.19.0 || >=22.12.0"})

    def test_optional_dependencies_are_merged_and_never_trimmed(self):
        # Upstream ships 16 platform packages; adding riscv64 must not drop any.
        self.write_manifest({
            "name": "@demo/widget", "version": "1.0.0",
            "optionalDependencies": {"@demo/widget-linux-x64": "1.0.0",
                                     "@demo/widget-darwin-arm64": "1.0.0"},
        })
        result = self.prepare(self.spec(manifest="upstream",
                                       optional={"@demo/widget-linux-riscv64": "1.0.0"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        deps = self.manifest()["optionalDependencies"]
        self.assertEqual(sorted(deps), ["@demo/widget-darwin-arm64",
                                       "@demo/widget-linux-riscv64",
                                       "@demo/widget-linux-x64"])
        self.assertEqual(deps["@demo/widget-linux-riscv64"], "1.0.0")

    def test_the_merge_is_idempotent(self):
        # The publish step rewrites the manifest of the tree it publishes from, so a
        # second run sees its own entry. Refusing that turned a re-run into a failure
        # ("did not grow") instead of a no-op.
        self.write_manifest({
            "name": "@demo/widget", "version": "1.0.0",
            "optionalDependencies": {"@demo/widget-linux-x64": "1.0.0",
                                     "@demo/widget-linux-riscv64": "1.0.0"},
        })
        spec = self.spec(manifest="upstream", optional={"@demo/widget-linux-riscv64": "1.0.0"})
        first = self.prepare(spec)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn("added", first.stdout)
        bytes_after_first = self.manifest()
        second = self.prepare(spec)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.manifest(), bytes_after_first)

    def test_upstream_shape_touches_only_what_the_recipe_declares(self):
        self.write_manifest({"name": "@demo/widget", "version": "1.0.0", "main": "widget.js"})
        result = self.prepare(self.spec(manifest="upstream", main="widget.node",
                                        cpu="riscv64", os="linux"))
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.manifest()
        self.assertEqual(manifest["main"], "widget.js")   # upstream keeps its entry point
        self.assertNotIn("os", manifest)                   # and its platform fields
        self.assertNotIn("cpu", manifest)

    def test_upstream_shape_refuses_a_name_mismatch(self):
        self.write_manifest({"name": "@demo/other", "version": "1.0.0"})
        result = self.prepare(self.spec(manifest="upstream"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("@demo/other", result.stderr)

    def test_template_shape_writes_the_resolved_manifest(self):
        spec = self.spec(manifest="template",
                         manifest_json={"name": "@demo/widget", "version": "1.0.0",
                                        "main": "widget.node", "files": ["widget.node"]},
                         readme_text="# `@demo/widget`\n")
        result = self.prepare(spec)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.manifest()
        self.assertEqual(manifest["main"], "widget.node")
        self.assertEqual(manifest["files"], ["widget.node"])
        self.assertEqual((self.dir / "README.md").read_text(encoding="utf-8"),
                         "# `@demo/widget`\n")

    def test_shell_mode_emits_what_the_script_needs(self):
        result = self.run_helper(self.spec(kind="upstream-tarball",
                                           base={"url": "https://host/demo-1.0.0.tgz",
                                                 "integrity": "sha512-abc"},
                                           overlay=["prebuilds/linux-riscv64"]),
                                 "--shell")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("P_NAME='@demo/widget'", result.stdout)
        self.assertIn("P_REGISTRY='https://example.invalid'", result.stdout)
        self.assertIn("P_OVERLAY='prebuilds/linux-riscv64'", result.stdout)
        self.assertIn("P_BASE_INTEGRITY='sha512-abc'", result.stdout)

    def test_integrity_mode_verifies_the_base_tarball(self):
        tarball = Path(self.tmp.name) / "base.tgz"
        tarball.write_bytes(b"not really a tarball")
        digest = "sha512-" + base64.b64encode(hashlib.sha512(b"not really a tarball").digest()).decode()
        result = self.run_helper(self.spec(base={"url": "https://host/demo-1.0.0.tgz",
                                                "integrity": digest}),
                                 "--verify-integrity", str(tarball))
        self.assertEqual(result.returncode, 0, result.stderr)
        bad = self.run_helper(self.spec(base={"url": "https://host/demo-1.0.0.tgz",
                                             "integrity": "sha512-wrong"}),
                             "--verify-integrity", str(tarball))
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("integrity mismatch", bad.stderr)


class PublishValidationTests(unittest.TestCase):
    """Recipes must not be able to describe a package that cannot exist."""

    def _target(self, root: Path) -> rl.Target:
        return rl.Target(pkg="demo", ver="1.0.0", path=root / "packages/demo/1.0.0")

    def _problems(self, root: Path, publish, **extra) -> list[str]:
        recipe = {
            "upstream": {"repo": "https://github.com/example/demo", "ref": "v1.0.0"},
            "artifacts": {"path": "*.node"},
            "npm_publish": publish,
        }
        recipe.update(extra)
        return rl.validate_stat(self._target(root), recipe)

    def test_binding_role_needs_a_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            problems = self._problems(Path(tmp), [{
                "role": "binding", "name": "@demo/binding", "version": "1.0.0", "dir": "out",
            }, {
                "role": "main", "name": "@demo/widget", "version": "1.0.0", "dir": ".",
            }])
        self.assertTrue(any("requires os and cpu" in problem for problem in problems))

    def test_base_must_be_the_upstream_tarball_of_this_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            problems = self._problems(Path(tmp), [{
                "role": "main", "name": "demo", "version": "1.0.0", "dir": ".",
                "kind": "upstream-tarball",
                "base": {"url": "https://host/other-9.9.9.tgz", "integrity": "sha512-abc"},
                "overlay": ["prebuilds/linux-riscv64"],
            }])
        self.assertTrue(any("must end with 'demo-1.0.0.tgz'" in p for p in problems))

    def test_base_needs_an_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            problems = self._problems(Path(tmp), [{
                "role": "main", "name": "demo", "version": "1.0.0", "dir": ".",
                "kind": "upstream-tarball",
                "base": {"url": "https://host/demo-1.0.0.tgz"},
                "overlay": ["prebuilds/linux-riscv64"],
            }])
        self.assertTrue(any("base.integrity is required" in p for p in problems))

    def test_overlay_must_stay_inside_the_source_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            problems = self._problems(Path(tmp), [{
                "role": "main", "name": "demo", "version": "1.0.0", "dir": ".",
                "kind": "upstream-tarball",
                "base": {"url": "https://host/demo-1.0.0.tgz", "integrity": "sha512-abc"},
                "overlay": ["../outside"],
            }])
        self.assertTrue(any("must stay inside the source tree" in p for p in problems))

    def test_optional_dependencies_must_name_a_platform_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            problems = self._problems(Path(tmp), [{
                "role": "main", "name": "@demo/widget", "version": "1.0.0", "dir": ".",
                "optional_dependencies": ["@demo/widget"],
            }, {
                "role": "main", "name": "@demo/widget", "version": "1.0.0", "dir": "other",
            }])
        self.assertTrue(any("is duplicated" in problem for problem in problems))

        with tempfile.TemporaryDirectory() as tmp:
            problems = self._problems(Path(tmp), [{
                "role": "main", "name": "@demo/widget", "version": "1.0.0", "dir": ".",
                "optional_dependencies": ["@demo/gone"],
            }])
        self.assertTrue(any("references unknown package" in problem for problem in problems))

    def test_manifest_shape_and_generate_manifest_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            problems = self._problems(Path(tmp), [{
                "role": "main", "name": "demo", "version": "1.0.0", "dir": ".",
                "manifest_shape": "upstream", "generate_manifest": True,
            }])
        self.assertTrue(any("set either manifest_shape or generate_manifest" in p
                            for p in problems))

    def test_manifest_template_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "packages/demo/1.0.0").mkdir(parents=True)
            problems = self._problems(Path(tmp), [{
                "role": "main", "name": "demo", "version": "1.0.0", "dir": ".",
                "manifest_shape": "template", "manifest_template": "templates/package.json",
            }])
        self.assertTrue(any("manifest_template not found" in problem for problem in problems))


class UpstreamIncrementTests(unittest.TestCase):
    """Upstream increments are patches for an upstream PR, never build-time patches.

    Two tracks must stay apart (.dsh/plan.md D34): patches/ changes the build and is
    applied by the composite action, upstream/ is documentation-grade and must never be
    applied. These tests pin both sides of that boundary.
    """

    def _target(self, root: Path) -> rl.Target:
        return rl.Target(pkg="demo", ver="1.0.0", path=root / "packages/demo/1.0.0")

    def _problems(self, root: Path, increments) -> list[str]:
        recipe = {
            "upstream": {"repo": "https://github.com/example/demo", "ref": "v1.0.0"},
            "artifacts": {"path": "*.node"},
            "npm_publish": [{
                "role": "main", "name": "demo", "version": "1.0.0", "dir": ".",
            }],
            "upstream_increments": increments,
        }
        return rl.validate_stat(self._target(root), recipe)

    def test_increments_must_live_under_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            recipe = Path(tmp) / "packages/demo/1.0.0"
            (recipe / "patches").mkdir(parents=True)
            (recipe / "patches" / "riscv64.patch").write_text("diff\n", encoding="utf-8")
            problems = self._problems(Path(tmp), [{
                "patch": "patches/riscv64.patch", "touches": ["package.json"], "pr": "...",
            }])
        self.assertTrue(any("must live under 'upstream/'" in problem for problem in problems))

    def test_increments_need_touches_and_a_pr_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            recipe = Path(tmp) / "packages/demo/1.0.0"
            (recipe / "upstream").mkdir(parents=True)
            (recipe / "upstream" / "riscv64.patch").write_text("diff\n", encoding="utf-8")
            problems = self._problems(Path(tmp), [{"patch": "upstream/riscv64.patch"}])
        self.assertTrue(any("touches" in problem for problem in problems))
        self.assertTrue(any("pr is required" in problem for problem in problems))

    def test_the_generated_patch_step_never_applies_an_increment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe = root / "packages/demo/1.0.0"
            (recipe / "patches").mkdir(parents=True)
            (recipe / "patches" / "build.patch").write_text("diff\n", encoding="utf-8")
            (recipe / "upstream").mkdir()
            (recipe / "upstream" / "riscv64.patch").write_text("diff\n", encoding="utf-8")
            (recipe / "commands").mkdir()
            (recipe / "commands" / "build").write_text("echo build\n", encoding="utf-8")
            (recipe / "commands" / "test").write_text("echo test\n", encoding="utf-8")
            (recipe / rl.STAT_FILE).write_text(textwrap.dedent("""
                upstream:
                  repo: https://github.com/example/demo
                  ref: v1.0.0
                artifacts:
                  path: "*.node"
                npm_publish:
                  - role: main
                    name: demo
                    version: 1.0.0
                    dir: .
                upstream_increments:
                  - patch: upstream/riscv64.patch
                    touches: [package.json]
                    pr: "add riscv64 to the build matrix"
            """).lstrip("\n"), encoding="utf-8")
            patches = [recipe / "patches" / "build.patch"]
            script = render.generate_patch(patches, "packages/demo/1.0.0")
        self.assertIn("patches/build.patch", script)
        self.assertNotIn("upstream/", script)


class GeneratedScriptSyntaxTests(unittest.TestCase):
    """Every generated script must at least be valid bash (L0 gate)."""

    def test_generated_scripts_pass_bash_syntax_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "action"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render.py",
                    "--pkg",
                    "bufferutil",
                    "--ver",
                    "4.2.0",
                    "--out-dir",
                    str(out),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            scripts = sorted((out / "steps").glob("*.sh"))
            self.assertTrue(scripts, "no scripts were generated")
            for script in scripts:
                check = subprocess.run(
                    ["bash", "-n", str(script)], capture_output=True, text=True
                )
                self.assertEqual(check.returncode, 0, f"{script}: {check.stderr}")

    def test_action_yml_is_self_consistent(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "action"
            subprocess.run(
                [
                    sys.executable,
                    "scripts/render.py",
                    "--pkg",
                    "bufferutil",
                    "--ver",
                    "4.2.0",
                    "--out-dir",
                    str(out),
                ],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
            )
            action = yaml.safe_load((out / "action.yml").read_text(encoding="utf-8"))
            self.assertEqual(action["runs"]["using"], "composite")
            # Every script the action mentions must exist, and every generated
            # script must be mentioned: a stale reference is a broken action.
            referenced = {
                line.split("/steps/")[-1].rstrip('"')
                for line in (out / "action.yml").read_text(encoding="utf-8").splitlines()
                if "/steps/" in line
            }
            on_disk = {script.name for script in (out / "steps").glob("*.sh")}
            self.assertEqual(referenced, on_disk, f"referenced {referenced}, on disk {on_disk}")


    def test_the_manifest_helper_and_template_are_valid(self):
        # Both are real files now, so they can be checked without rendering anything.
        helper = REPO_ROOT / "scripts/publish_manifest.mjs"
        node = shutil.which("node")
        if node:
            check = subprocess.run([node, "--check", str(helper)], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
        template = REPO_ROOT / "scripts/publish_template.sh"
        check = subprocess.run(["bash", "-n", str(template)], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_the_rendered_action_carries_the_helper_next_to_the_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "action"
            subprocess.run(
                [sys.executable, "scripts/render.py", "--pkg", "bufferutil", "--ver", "4.2.0",
                 "--out-dir", str(out)],
                cwd=REPO_ROOT, check=True, capture_output=True,
            )
            self.assertTrue((out / "steps/manifest.mjs").is_file())

class GoldenTests(unittest.TestCase):
    """Snapshot the rendered actions so rendering drift is visible in review.

    Two recipes are pinned on purpose: bufferutil is the single-package shape and
    rolldown is the two-package shape, so the parts of the renderer that only differ
    between them (publish ordering, manifest sources, template substitution) cannot
    change unnoticed.
    """

    CASES = [("bufferutil", "4.2.0"), ("rolldown", "1.3.3")]
    GOLDEN_ROOT = REPO_ROOT / "tests/golden"

    def _render(self, pkg: str, ver: str, target: Path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/render.py",
                "--pkg",
                pkg,
                "--ver",
                ver,
                "--out-dir",
                str(target),
                "--steps-dir",
                ".rendered/action",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_render_matches_golden(self):
        import difflib

        for pkg, ver in self.CASES:
            golden_root = self.GOLDEN_ROOT / f"{pkg}-{ver}"
            with self.subTest(recipe=f"{pkg}@{ver}"), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "action"
                self._render(pkg, ver, target)

                # The file *sets* have to match in both directions: a step file the
                # renderer newly emits is exactly the kind of change a golden file
                # comparison alone would miss.
                expected_names = {p.name for p in (golden_root / "steps").iterdir() if p.is_file()}
                actual_names = {p.name for p in (target / "steps").iterdir() if p.is_file()}
                self.assertEqual(
                    expected_names,
                    actual_names,
                    f"steps/ differs from {golden_root.name}/steps; regenerate with "
                    "`python3 tests/regen_golden.py` and review the diff",
                )

                pairs = [(golden_root / "action.yml", target / "action.yml")]
                pairs += [(golden_root / "steps" / name, target / "steps" / name)
                          for name in sorted(expected_names)]
                for golden, actual in pairs:
                    self.assertTrue(golden.is_file(), f"missing golden {golden}")
                    expected = golden.read_text(encoding="utf-8")
                    got = actual.read_text(encoding="utf-8")
                    if expected != got:
                        diff = "".join(
                            difflib.unified_diff(
                                expected.splitlines(keepends=True),
                                got.splitlines(keepends=True),
                                fromfile=f"golden/{golden.name}",
                                tofile=f"rendered/{actual.name}",
                            )
                        )
                        self.fail(
                            "rendered output differs from the golden files; if the "
                            "change is intended run `python3 tests/regen_golden.py` "
                            f"and review the diff:\n{diff}"
                        )


class RepoIntegrationTests(unittest.TestCase):
    def test_check_all_passes_for_the_real_repository(self):
        result = subprocess.run(
            [sys.executable, "scripts/render.py", "--check-all"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[ OK ]", result.stdout)


if __name__ == "__main__":
    unittest.main()
