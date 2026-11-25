"""Checks for the upstream-increment verifier (scripts/check_patches.py).

The point of these tests is the boundary itself: a patch that would ship a
build-time change or mention anything of this repository must not pass, because
"submit it upstream as is" is the only reason those files exist
(.dsh/plan.md D34).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_patches as cp  # noqa: E402
import recipe_lib as rl  # noqa: E402

GOOD_PATCH = """\
Upstream-PR: ci: build a riscv64 prebuild
Upstream-Repo: https://github.com/example/demo
Upstream-Base: v1.0.0 (deadbeef)
Upstream-Files: .github/workflows/ci.yml

diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1,2 +1,4 @@
 name: CI
+  build-riscv64:
+    runs-on: [self-hosted, linux, riscv64]
"""

STAT = """\
upstream:
  repo: https://github.com/example/demo
  ref: v1.0.0

artifacts:
  path: prebuilds/linux-riscv64/*.node

npm_publish:
  - role: main
    name: demo
    version: 1.0.0
    dir: .
    registry: https://mirror.example.net/repository/npm-local/
"""


class IncrementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.recipe = self.root / "packages/demo/1.0.0"
        (self.recipe / "upstream").mkdir(parents=True)
        self.patch = self.recipe / "upstream/riscv64.patch"

    def target(self) -> rl.Target:
        return rl.Target(pkg="demo", ver="1.0.0", path=self.recipe)

    def stat(self, patch_text: str, touches: list[str] | None = None, **extra) -> dict:
        self.patch.write_text(patch_text, encoding="utf-8")
        stat = yaml.safe_load(STAT)
        stat["upstream_increments"] = [
            {
                "patch": "upstream/riscv64.patch",
                "touches": touches or [".github/workflows/ci.yml"],
                "pr": "build a riscv64 prebuild",
            }
        ]
        stat.update(extra)
        return stat

    def check(self, patch_text: str, **kwargs) -> list[str]:
        return cp.check_target(self.target(), self.stat(patch_text, **kwargs))

    def test_a_well_formed_increment_passes(self):
        self.assertEqual(self.check(GOOD_PATCH), [])

    def test_the_pr_description_is_required(self):
        body_only = GOOD_PATCH[GOOD_PATCH.index("diff --git") :]
        problems = self.check(body_only)
        self.assertTrue(any("Upstream-PR" in problem for problem in problems))

    def test_paths_must_be_declared_in_touches(self):
        stat = self.stat(GOOD_PATCH, touches=["package.json"])
        problems = cp.check_target(self.target(), stat)
        self.assertTrue(any("not listed in upstream_increments.touches" in problem for problem in problems))

    def test_touches_must_match_the_diff(self):
        stat = self.stat(GOOD_PATCH, touches=[".github/workflows/ci.yml", "README.md"])
        problems = cp.check_target(self.target(), stat)
        self.assertTrue(any("does not change it" in problem for problem in problems))

    def test_repository_specific_strings_are_refused(self):
        leaky = GOOD_PATCH.replace(
            "+    runs-on: [self-hosted, linux, riscv64]",
            "+    run: echo forgestash-old.verify.oepkgs.net",
        )
        problems = self.check(leaky)
        self.assertTrue(any("forgestash" in problem for problem in problems))

    def test_the_registry_host_is_refused(self):
        leaky = GOOD_PATCH.replace(
            "+    runs-on: [self-hosted, linux, riscv64]",
            "+    run: npm publish --registry https://mirror.example.net/repository/npm-local/",
        )
        problems = self.check(leaky)
        self.assertTrue(any("registry host" in problem for problem in problems))

    def test_deleting_an_upstream_file_is_refused(self):
        deletion = GOOD_PATCH.replace(
            "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml",
            "--- a/.github/workflows/ci.yml\n+++ /dev/null",
        )
        problems = self.check(deletion)
        self.assertTrue(any("deletes an upstream file" in problem for problem in problems))

    def test_a_binding_package_must_match_the_napi_target(self):
        stat = self.stat(GOOD_PATCH)
        stat["npm_publish"] = [
            {
                "role": "binding",
                "name": "@demo/binding-linux-riscv64-glibc",
                "version": "1.0.0",
                "dir": "out/binding",
                "os": "linux",
                "cpu": "riscv64",
                "libc": "glibc",
            },
            {
                "role": "main",
                "name": "demo",
                "version": "1.0.0",
                "dir": ".",
                "optional_dependencies": ["@demo/binding-linux-riscv64-glibc"],
            },
        ]
        problems = cp.check_target(self.target(), stat)
        # napi names the package after the triple: "...-linux-riscv64-gnu".
        self.assertTrue(any("does not end with '-riscv64-gnu'" in problem for problem in problems))

        stat["npm_publish"][0]["name"] = "@demo/binding-linux-riscv64-gnu"
        stat["npm_publish"][1]["optional_dependencies"] = ["@demo/binding-linux-riscv64-gnu"]
        problems = cp.check_target(self.target(), stat)
        self.assertTrue(any("does not add a riscv64gc-unknown-linux" in problem for problem in problems))

        patched = GOOD_PATCH.replace(
            "+    runs-on: [self-hosted, linux, riscv64]",
            '+    "riscv64gc-unknown-linux-gnu",',
        )
        self.patch.write_text(patched, encoding="utf-8")
        self.assertEqual(cp.check_target(self.target(), stat), [])

    def test_a_missing_patch_file_is_reported(self):
        stat = self.stat(GOOD_PATCH)
        self.patch.unlink()
        problems = cp.check_target(self.target(), stat)
        self.assertTrue(any("missing patch file" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()
