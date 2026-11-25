#!/usr/bin/env python3
"""Tests for the served-artifact assertion (`manifest.mjs --assert-served`).

The publish step used to be green on two very different outcomes: "the registry
stored this artifact" and "the registry refused and already serves something else".
The second case is how a mirror that proxies npmjs silently kept serving upstream's
package without the riscv64 increment, so the step now verifies what the registry
actually serves.

The same question has to be answered against three registry behaviours, and these
tests cover all three with a stub npm (no real registry is involved):

* a published digest (`dist.integrity`), which npmjs, verdaccio and GitHub Packages
  serve;
* only a `dist.shasum` (older hosted repositories);
* neither, only a `dist.tarball` - which is what the private registry served, and the
  reason a matching artifact once looked like a mismatch: npm >= 12 prints
  `npm view --json` as a one-element *list*, so a digest read from the top level of
  that list is always missing.
"""

import hashlib
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "publish_manifest.mjs"

STUB = """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  pack)
    # npm <=10 prints an array, npm >=11 an object keyed by name; the helper reads both.
    printf '%s' "$STUB_PACK_JSON"
    ;;
  view)
    printf '%s' "$STUB_VIEW_JSON"
    ;;
  *)
    echo "stub npm: unexpected command $1" >&2
    exit 1
    ;;
esac
"""

PAYLOAD = b"pretend this is a tarball\n"
OTHER_PAYLOAD = b"pretend this is someone else's tarball\n"


def sha512_sri(payload):
    return "sha512-" + __import__("base64").b64encode(
        hashlib.sha512(payload).digest()
    ).decode()


class Registry:
    """A tarball endpoint: serves `payload`, optionally only with the right token."""

    def __init__(self, payload=PAYLOAD, require_auth=False):
        self.payload = payload
        self.require_auth = require_auth
        self.seen_auth = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen_auth.append(self.headers.get("authorization", ""))
                if outer.require_auth and self.headers.get(
                    "authorization"
                ) != "Bearer secret":
                    self.send_response(401)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("content-length", str(len(outer.payload)))
                self.end_headers()
                self.wfile.write(outer.payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def tarball_url(self):
        return "http://127.0.0.1:%d/example-1.0.0.tgz" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class AssertServedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="dsh-assert-served-"))
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
        self.registries = []
        self.addCleanup(self._close_registries)

    def _close_registries(self):
        for registry in self.registries:
            registry.close()

    def registry(self, **kwargs):
        registry = Registry(**kwargs)
        self.registries.append(registry)
        return registry

    def spec(self, optional=None):
        return {
            "name": "example",
            "version": "1.0.0",
            "registry": "https://registry.example.test/",
            "dir": ".",
            "kind": "source-tree",
            "manifest": "upstream",
            "optional": optional or {},
        }

    def run_helper(self, pack_json, view_json, optional=None, accepted=False, env=None):
        environment = dict(os.environ)
        environment.pop("npm_execpath", None)
        for key in ("PUBLISH_VERIFY", "PUBLISH_VERIFY_MAX_BYTES", "NPM_CONFIG_USERCONFIG"):
            environment.pop(key, None)
        environment["PATH"] = "%s:%s" % (self.stub_dir, environment.get("PATH", ""))
        environment["STUB_PACK_JSON"] = pack_json
        environment["STUB_VIEW_JSON"] = view_json
        environment.update(env or {})
        argv = [
            "node",
            str(HELPER),
            "--assert-served",
            json.dumps(self.spec(optional)),
            str(self.pkg_dir),
        ]
        if accepted:
            argv.append("--accepted")
        return subprocess.run(
            argv, capture_output=True, text=True, env=environment, cwd=str(self.tmp)
        )

    # -- digests the registry publishes ------------------------------------------

    def test_identical_artifact_passes(self):
        ours = "sha512-ours=="
        result = self.run_helper(
            json.dumps([{"integrity": ours, "files": []}]),
            json.dumps({"dist": {"integrity": ours}}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the registry serves this artifact", result.stdout)

    def test_object_shaped_pack_output_is_read_too(self):
        # npm >= 11 prints {"<name>": {...}} instead of [{...}]
        ours = "sha512-ours=="
        result = self.run_helper(
            json.dumps({"example": {"integrity": ours, "files": []}}),
            json.dumps({"dist": {"integrity": ours}}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_npm_twelve_list_shaped_view_output_is_read(self):
        # npm >= 12 prints `npm view --json` as a one-element list. Reading that list as
        # the manifest made a matching digest look missing, which is how a delivered
        # package was reported as "the registry serves something else" in CI.
        ours = "sha512-ours=="
        result = self.run_helper(
            json.dumps([{"integrity": ours}]),
            json.dumps([{"name": "example", "version": "1.0.0", "dist": {"integrity": ours}}]),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the registry serves this artifact", result.stdout)

    def test_the_increment_check_also_reads_the_list_shape(self):
        ours = "sha512-ours=="
        result = self.run_helper(
            json.dumps([{"integrity": ours}]),
            json.dumps([{"dist": {"integrity": ours}, "optionalDependencies": {}}]),
            optional={"@scope/platform": "1.0.0"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("@scope/platform@1.0.0", result.stderr)

    def test_a_different_artifact_fails_and_names_both(self):
        result = self.run_helper(
            json.dumps([{"integrity": "sha512-ours=="}]),
            json.dumps({"dist": {"integrity": "sha512-theirs=="}}),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("the registry does not serve this artifact", result.stderr)
        self.assertIn("sha512-ours==", result.stderr)
        self.assertIn("sha512-theirs==", result.stderr)
        # The message has to say what to do, because this is an operator decision.
        self.assertIn("enable redeploy", result.stderr)

    def test_a_list_shaped_different_digest_still_fails(self):
        result = self.run_helper(
            json.dumps([{"integrity": "sha512-ours=="}]),
            json.dumps([{"dist": {"integrity": "sha512-theirs=="}}]),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("sha512-theirs==", result.stderr)

    def test_a_missing_increment_fails(self):
        ours = "sha512-ours=="
        result = self.run_helper(
            json.dumps([{"integrity": ours}]),
            json.dumps({"dist": {"integrity": ours}, "optionalDependencies": {}}),
            optional={"@scope/platform": "1.0.0"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("@scope/platform@1.0.0", result.stderr)
        self.assertIn("would not pull the riscv64 build", result.stderr)

    def test_a_present_increment_passes(self):
        ours = "sha512-ours=="
        result = self.run_helper(
            json.dumps([{"integrity": ours}]),
            json.dumps(
                {
                    "dist": {"integrity": ours},
                    "optionalDependencies": {"@scope/platform": "1.0.0"},
                }
            ),
            optional={"@scope/platform": "1.0.0"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_unreadable_registry_answer_fails(self):
        # Silence here would turn "could not check" into "checked and fine".
        result = self.run_helper(
            json.dumps([{"integrity": "sha512-ours=="}]),
            "not json",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read what example@1.0.0 serves", result.stderr)

    # -- registries that publish only a sha1 ------------------------------------

    def test_shasum_is_used_when_no_integrity_is_published(self):
        sha1 = hashlib.sha1(PAYLOAD).hexdigest()
        result = self.run_helper(
            json.dumps([{"integrity": "sha512-ours==", "shasum": sha1}]),
            json.dumps({"dist": {"shasum": sha1}}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dist.shasum", result.stdout)

    def test_a_different_shasum_fails(self):
        result = self.run_helper(
            json.dumps([{"integrity": "sha512-ours==", "shasum": "a" * 40}]),
            json.dumps({"dist": {"shasum": "b" * 40}}),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("the shasums differ", result.stderr)

    # -- registries that publish nothing but a tarball URL ----------------------

    def test_the_tarball_is_downloaded_when_no_digest_is_published(self):
        registry = self.registry()
        result = self.run_helper(
            json.dumps([{"integrity": sha512_sri(PAYLOAD)}]),
            json.dumps({"dist": {"tarball": registry.tarball_url}}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tarball sha512", result.stdout)
        self.assertIn("%d bytes" % len(PAYLOAD), result.stdout)

    def test_a_downloaded_tarball_with_different_bytes_fails(self):
        registry = self.registry(payload=OTHER_PAYLOAD)
        result = self.run_helper(
            json.dumps([{"integrity": sha512_sri(PAYLOAD)}]),
            json.dumps({"dist": {"tarball": registry.tarball_url}}),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("the downloaded tarball differs", result.stderr)
        self.assertIn(sha512_sri(OTHER_PAYLOAD), result.stderr)

    def test_the_download_uses_the_npmrc_credential_for_the_tarball_url(self):
        # A registry that publishes no digest still needs auth to read the tarball, so
        # the check has to look the credential up the way npm does.
        registry = self.registry(require_auth=True)
        npmrc = self.tmp / "npmrc"
        npmrc.write_text(
            "//127.0.0.1:%d/:_authToken=secret\n" % registry.server.server_address[1]
        )
        result = self.run_helper(
            json.dumps([{"integrity": sha512_sri(PAYLOAD)}]),
            json.dumps({"dist": {"tarball": registry.tarball_url}}),
            env={"NPM_CONFIG_USERCONFIG": str(npmrc)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(registry.seen_auth, ["Bearer secret"])

    def test_a_tarball_above_the_size_cap_is_not_downloaded(self):
        registry = self.registry()
        result = self.run_helper(
            json.dumps([{"integrity": sha512_sri(PAYLOAD)}]),
            json.dumps({"dist": {"tarball": registry.tarball_url}}),
            accepted=True,
            env={"PUBLISH_VERIFY_MAX_BYTES": "4"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING", result.stdout)
        self.assertIn("above the verification cap", result.stdout)

    # -- "cannot verify" is its own state ---------------------------------------

    def test_unverifiable_after_a_successful_upload_is_a_warning(self):
        # No digest and an unreadable tarball leaves nothing to compare. After a 201 the
        # artifact is there, so this must not turn a delivered package red - but it must
        # not claim verification either.
        registry = self.registry(require_auth=True)
        result = self.run_helper(
            json.dumps([{"integrity": "sha512-ours=="}]),
            json.dumps({"dist": {"tarball": registry.tarball_url}}),
            accepted=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING", result.stdout)
        self.assertIn("accepted but not verified", result.stdout)

    def test_unverifiable_after_a_refusal_fails(self):
        # After a refusal there is no evidence at all that our bytes are what a consumer
        # gets, so silence would be the very bug this check exists for.
        registry = self.registry(require_auth=True)
        result = self.run_helper(
            json.dumps([{"integrity": "sha512-ours=="}]),
            json.dumps({"dist": {"tarball": registry.tarball_url}}),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot verify what example@1.0.0 serves", result.stderr)
        self.assertIn("the publish was refused", result.stderr)
        # The operator still has to know what to do about it.
        self.assertIn("enable redeploy", result.stderr.replace("Enable redeploy", "enable redeploy"))

    def test_a_missing_tarball_url_is_unverifiable(self):
        result = self.run_helper(
            json.dumps([{"integrity": "sha512-ours=="}]),
            json.dumps({"dist": {}}),
            accepted=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no digest and no tarball URL", result.stdout)

    def test_publish_verify_digest_skips_the_download(self):
        # The documented opt-out for a slow route: never download, and report honestly
        # that the artifact was accepted but not verified.
        registry = self.registry()
        result = self.run_helper(
            json.dumps([{"integrity": "sha512-ours=="}]),
            json.dumps({"dist": {"tarball": registry.tarball_url}}),
            accepted=True,
            env={"PUBLISH_VERIFY": "digest"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PUBLISH_VERIFY=digest skips the tarball check", result.stdout)
        self.assertEqual(registry.seen_auth, [])


if __name__ == "__main__":
    unittest.main()
