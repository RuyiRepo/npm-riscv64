#!/usr/bin/env python3
"""Tests for the publish plan (`manifest.mjs --plan`).

The publish used npm's default `fetch-timeout` of 300 s, which is a wall-clock budget
for the whole request - upload included. A package carrying a native binding uploads
about 12 MB of base64 JSON, so the default cap is a function of the route's speed, not
of the artifact: on a slower route npm aborted mid-body, retried twice, and the registry
never saw a complete request. The plan derives the budget from the packed size instead,
and these tests pin that arithmetic with a stub npm.
"""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "publish_manifest.mjs"

STUB = """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  pack) printf '%s' "$STUB_PACK_JSON" ;;
  *) echo "stub npm: unexpected command $1" >&2; exit 1 ;;
esac
"""


class PublishPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="dsh-publish-plan-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg_dir = self.tmp / "pkg"
        self.pkg_dir.mkdir()
        (self.pkg_dir / "package.json").write_text(
            json.dumps({"name": "example", "version": "1.0.0"}, indent=2) + "\n"
        )
        self.stub_dir = self.tmp / "stub"
        self.stub_dir.mkdir()
        stub = self.stub_dir / "npm"
        stub.write_text(STUB)
        stub.chmod(0o755)

    def spec(self):
        return {
            "name": "example",
            "version": "1.0.0",
            "registry": "https://registry.example.test/",
            "dir": ".",
            "kind": "source-tree",
            "manifest": "generated",
            "optional": {},
        }

    def run_plan(self, packed_size, env=None):
        environment = dict(os.environ)
        environment.pop("npm_execpath", None)
        environment.pop("PUBLISH_FETCH_TIMEOUT", None)
        environment["PATH"] = "%s:%s" % (self.stub_dir, environment.get("PATH", ""))
        environment["STUB_PACK_JSON"] = json.dumps(
            [{"integrity": "sha512-x==", "size": packed_size, "files": []}]
        )
        environment.update(env or {})
        return subprocess.run(
            ["node", str(HELPER), "--plan", json.dumps(self.spec()), str(self.pkg_dir)],
            capture_output=True,
            text=True,
            env=environment,
            cwd=str(self.tmp),
        )

    @staticmethod
    def parse(stdout):
        values = {}
        for line in stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                values[key] = value.strip("'\"")
        return values

    def test_an_ordinary_package_keeps_npm_default_plus_its_transfer(self):
        # 200 kB -> 280 kB of body at the assumed 16 kB/s -> 18 s on top of 300 s.
        result = self.run_plan(200_000)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = self.parse(result.stdout)
        self.assertEqual(values["P_PACKED_BYTES"], "200000")
        self.assertEqual(values["P_FETCH_TIMEOUT"], "318000")
        # Retries stay at npm's default: a retry of a small body is cheap.
        self.assertEqual(values["P_FETCH_RETRIES"], "")

    def test_a_binding_sized_package_gets_minutes_not_five(self):
        result = self.run_plan(9_128_369)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = self.parse(result.stdout)
        # 12.8 MB of body at 16 kB/s is 799 s, on top of npm's 300 s floor.
        self.assertEqual(values["P_FETCH_TIMEOUT"], "1099000")
        # Re-uploading megabytes on a retry is waste, so npm retries once.
        self.assertEqual(values["P_FETCH_RETRIES"], "1")

    def test_the_retry_reduction_starts_above_four_megabytes(self):
        just_below = self.parse(self.run_plan(4 * 1024 * 1024 - 1).stdout)
        just_above = self.parse(self.run_plan(4 * 1024 * 1024).stdout)
        self.assertEqual(just_below["P_FETCH_RETRIES"], "")
        self.assertEqual(just_above["P_FETCH_RETRIES"], "1")

    def test_the_budget_is_capped_at_an_hour(self):
        values = self.parse(self.run_plan(500_000_000).stdout)
        self.assertEqual(values["P_FETCH_TIMEOUT"], "3600000")

    def test_an_operator_override_wins(self):
        result = self.run_plan(9_128_369, env={"PUBLISH_FETCH_TIMEOUT": "60000"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.parse(result.stdout)["P_FETCH_TIMEOUT"], "60000")

    def test_a_nonsense_override_is_refused(self):
        for value in ("0", "-1", "soon"):
            result = self.run_plan(1000, env={"PUBLISH_FETCH_TIMEOUT": value})
            self.assertEqual(result.returncode, 1, value)
            self.assertIn("PUBLISH_FETCH_TIMEOUT must be a positive number", result.stderr)

    def test_a_pack_failure_stops_before_publishing(self):
        self.stub_dir.joinpath("npm").write_text("#!/usr/bin/env bash\nexit 1\n")
        result = self.run_plan(1000)
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot size up this publish", result.stderr)


if __name__ == "__main__":
    unittest.main()
