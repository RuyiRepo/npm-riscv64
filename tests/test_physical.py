#!/usr/bin/env python3
"""Tests for the hardware verification layer (.dsh/plan.md D41).

The physical layer exists because a package that builds and passes its tests inside a
riscv64 container has still not been installed by anyone on riscv64 silicon. These
tests cover the parts of it that can be decided without that machine:

* the second template renders a one-step action that only installs and verifies, and
  that refuses a recipe which has nothing to verify;
* the generated install step treats "the registry cannot be read" and "the artifact is
  wrong" as different verdicts, and only uses the build's tarball when the publish
  report admits the registry serves nothing checkable (stub npm, no network);
* the publish step now writes the report those decisions are read from;
* the plan/summary helper that turns the reports into work.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import recipe_lib as rl  # noqa: E402
import render  # noqa: E402

PHYSICAL_TEMPLATE = ROOT / ".github/templates/physical.template.yml"
ACTION_TEMPLATE = ROOT / ".github/templates/action.template.yml"
PLAN_HELPER = ROOT / "scripts" / "physical_plan.mjs"
PUBLISH_HELPER = ROOT / "scripts" / "publish_manifest.mjs"

STAT_WITH_PHYSICAL = """\
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

physical:
  registry: https://registry.example.test/
  auth: none
  source: registry
"""


def stage_recipe(root: Path, pkg="demo", ver="1.0.0", stat=None, verify="echo verify\n", extra=""):
    """A minimal recipe with the files the physical template needs."""
    recipe = root / "packages" / pkg / ver
    (recipe / "commands").mkdir(parents=True, exist_ok=True)
    (recipe / rl.STAT_FILE).write_text(
        textwrap.dedent(stat if stat is not None else STAT_WITH_PHYSICAL.format(
            name="@demo/%s" % pkg.replace("/", "-"), ver=ver
        )) + textwrap.dedent(extra),
        encoding="utf-8",
    )
    for name in ("fetch", "patch", "build", "test", "publish"):
        (recipe / "commands" / name).write_text("echo %s\n" % name, encoding="utf-8")
        (recipe / "commands" / name).chmod(0o755)
    if verify is not None:
        (recipe / "commands" / "verify").write_text(verify, encoding="utf-8")
        (recipe / "commands" / "verify").chmod(0o755)
    return recipe


def helper_block(text: str, name: str):
    """The definition of one shell function, as it appears in a rendered script."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(name + "() {"):
            if line.rstrip().endswith("}"):
                return line
            for end in range(index + 1, len(lines)):
                if lines[end] == "}":
                    return "\n".join(lines[index:end + 1])
            return None
    return None


def render_physical(root: Path, pkg="demo", ver="1.0.0", physical_registry=None):
    """Render the physical template and write it the way the CLI does."""
    out = root / "out" / pkg.replace("/", "-")
    rendered, scripts = render.render_target(
        root / "packages", pkg, ver, PHYSICAL_TEMPLATE, {},
        steps_dir=out.as_posix(), physical_registry=physical_registry,
    )
    out.mkdir(parents=True, exist_ok=True)
    action = out / "action.yml"
    action.write_text(rendered, encoding="utf-8")
    steps = out / "steps"
    steps.mkdir(exist_ok=True)
    outputs = {"action.yml": action, "steps": steps}
    for name, content in scripts.items():
        path = steps / ("%s.sh" % name)
        path.write_text(content.rstrip("\n") + "\n", encoding="utf-8")
        path.chmod(0o755)
        outputs["steps/%s.sh" % name] = path
    return outputs


def read(path):
    return Path(path).read_text(encoding="utf-8")


class PhysicalTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-physical-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stage_recipe(self.tmp)

    def test_the_action_only_installs_and_verifies(self):
        outputs = render_physical(self.tmp)
        action = outputs["action.yml"]
        text = read(action)
        # GitHub expressions (`${{ inputs.plan }}`) stay in the output by design; a
        # template placeholder that survived looks like `{{pkg.name}}`.
        leftovers = [m for m in __import__("re").findall(r"(?<!\$)\{{[a-zA-Z_.]+\}}", text)]
        self.assertEqual(leftovers, [], "the rendered action still holds a placeholder")
        doc = __import__("yaml").safe_load(text)
        names = [step.get("name") for step in doc["runs"]["steps"]]
        self.assertEqual(names, ["verify on hardware"], names)
        step = doc["runs"]["steps"][0]
        self.assertEqual(step["shell"], "bash")
        # The step runs the generated wrapper, which runs install.sh then verify.
        self.assertIn("steps/physical.sh", step["run"])
        self.assertEqual(
            set(doc["inputs"]), {"plan", "expected_integrity", "source", "tarball", "registry", "report"}
        )
        for key in ("PHYSICAL_PLAN", "PHYSICAL_SOURCE", "PHYSICAL_TARBALL", "PHYSICAL_REPORT"):
            self.assertIn(key, step["env"], key)
        # Recipe scripts: install and physical are generated, verify comes from the
        # recipe. Nothing from the build pipeline belongs here.
        scripts = sorted(p.name for p in (action.parent / "steps").iterdir())
        self.assertEqual(scripts, ["install.sh", "physical.sh", "verify.sh"], scripts)

    def test_a_recipe_may_override_the_generated_install_step(self):
        # §3 of the spec says install is generated but overridable: a recipe that knows
        # something the generated step cannot (a registry alias, a wrapper package)
        # replaces it, and the physical action must run that file instead.
        recipe = self.tmp / "packages" / "demo" / "1.0.0"
        override = "#!/usr/bin/env bash\necho \"installed by the recipe\"\n"
        (recipe / "commands" / "install").write_text(override, encoding="utf-8")
        (recipe / "commands" / "install").chmod(0o755)
        outputs = render_physical(self.tmp)
        rendered = read(outputs["steps/install.sh"])
        self.assertIn("installed by the recipe", rendered)
        # The generated step's machinery must be gone, not merely preceded.
        self.assertNotIn("PHYSICAL_EXPECTED_INTEGRITY", rendered)

    def test_the_docker_less_contract_is_stated_where_it_is_written(self):
        # The runner is a container and docker-in-docker is unproven on it, so no step
        # may grow a `docker run` through a later edit: the rendered steps are checked,
        # not the template's own comment about the rule.
        outputs = render_physical(self.tmp)
        yaml = read(outputs["action.yml"])
        start = yaml.index("runs:")
        self.assertNotIn("docker", yaml[start:])
        self.assertIn("no docker", read(PHYSICAL_TEMPLATE))
        # Comments may mention the rule; executable lines may not break it.
        for name in ("install", "physical", "verify"):
            for number, line in enumerate(read(outputs["steps/%s.sh" % name]).splitlines(), 1):
                if "docker" in line.lower() and not line.lstrip().startswith("#"):
                    self.fail("%s.sh:%d runs docker: %s" % (name, number, line))

    def test_a_recipe_without_a_verify_step_is_refused(self):
        recipe = self.tmp / "packages" / "demo" / "1.0.0"
        (recipe / "commands" / "verify").unlink()
        with self.assertRaises(render.RenderError) as caught:
            render.render_target(self.tmp / "packages", "demo", "1.0.0", PHYSICAL_TEMPLATE, {})
        # The message has to say which file the recipe is missing, not just "missing".
        self.assertIn("'verify'", str(caught.exception))
        self.assertIn("commands/", str(caught.exception))

    def test_a_recipe_without_physical_config_is_refused(self):
        stat = STAT_WITH_PHYSICAL.format(name="@demo/demo", ver="1.0.0").replace(
            "\nphysical:\n  registry: https://registry.example.test/\n  auth: none\n  source: registry\n",
            "",
        )
        stage_recipe(self.tmp, stat=stat)
        with self.assertRaises(render.RenderError) as caught:
            render.render_target(self.tmp / "packages", "demo", "1.0.0", PHYSICAL_TEMPLATE, {})
        self.assertIn("physical", str(caught.exception))

    def test_a_verify_step_without_physical_config_is_refused(self):
        stat = STAT_WITH_PHYSICAL.format(name="@demo/demo", ver="1.0.0").replace(
            "\nphysical:\n  registry: https://registry.example.test/\n  auth: none\n  source: registry\n",
            "",
        )
        stage_recipe(self.tmp, stat=stat)
        with self.assertRaises(render.RenderError) as caught:
            render.render_target(
                self.tmp / "packages", "demo", "1.0.0", ACTION_TEMPLATE, {}
            )
        # The pipeline template ignores verify, but a verify step that nothing runs is
        # a contract half-written: the error says which half is missing.
        self.assertIn("physical", str(caught.exception))

    def test_the_read_registry_comes_from_the_physical_section(self):
        outputs = render_physical(self.tmp)
        install = read(outputs["steps/install.sh"])
        self.assertIn('REGISTRY="${PHYSICAL_REGISTRY:?', install)
        wrapper = read(outputs["steps/physical.sh"])
        self.assertIn("https://registry.example.test/", wrapper)

    def test_the_read_registry_can_be_rewritten_for_a_fork(self):
        outputs = render_physical(self.tmp, physical_registry="https://fork.example.test/repository/npm-riscv-group")
        wrapper = read(outputs["steps/physical.sh"])
        self.assertIn("fork.example.test", wrapper)
        self.assertNotIn("registry.example.test", wrapper)

    def test_auth_basic_writes_the_shared_setup_call(self):
        recipe = self.tmp / "packages" / "demo" / "1.0.0"
        stat = read(recipe / rl.STAT_FILE).replace(
            "auth: none", "auth: basic\n  token_env: NPM_READ_TOKEN"
        )
        (recipe / rl.STAT_FILE).write_text(stat, encoding="utf-8")
        outputs = render_physical(self.tmp)
        wrapper = read(outputs["steps/physical.sh"])
        self.assertIn("setup_auth", wrapper)
        # The same function is emitted for the publish step, so both steps explain them-
        # selves identically when the credential is missing.
        self.assertIn("npm_config_userconfig", wrapper.lower())

    def test_auth_none_carries_no_credential_code(self):
        outputs = render_physical(self.tmp)
        wrapper = read(outputs["steps/physical.sh"])
        self.assertIn('physical.auth is "none"', wrapper)
        self.assertNotIn("setup_auth ", wrapper)


    def test_helpers_exported_to_a_snippet_can_log(self):
        # A snippet runs in its own shell (bash "$SCRIPT_DIR/verify.sh"), so anything it
        # calls has to be exported. physical_elf is exported and logs - an unexported log
        # turned a note into "log: command not found" on the first hardware run, which is
        # a message about the wrapper masquerading as a message about the artifact.
        outputs = render_physical(self.tmp)
        wrapper = read(outputs["steps/physical.sh"])
        exported = [
            line for line in wrapper.splitlines() if line.startswith("export -f ")
        ]
        self.assertTrue(exported)
        names = set()
        for line in exported:
            names|= set(line[len("export -f "):].split())
        for helper in ("log", "physical_fact", "physical_check", "physical_elf"):
            self.assertIn(helper, names, "the snippet contract exports " + helper)

    def test_a_snippet_shell_can_call_the_exported_elf_check_without_file(self):
        # The regression this pins: the recipe snippets call `physical_elf`, which logs.
        # Run in its own shell with `file` absent, the old wrapper printed
        # "log: command not found" and dropped the note - a message about the wrapper that
        # reads like a message about the artifact.
        outputs = render_physical(self.tmp)
        wrapper = read(outputs["steps/physical.sh"])
        helpers = []
        for name in ("log", "physical_fact", "physical_check", "physical_elf"):
            block = helper_block(wrapper, name)
            self.assertIsNotNone(block, "the wrapper defines " + name)
            helpers.append(block)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            elf = work / "demo.node"
            # ELF64, little endian, e_machine 243 (RISC-V): header only, which is what the
            # degraded check reads.
            header = bytearray(64)
            header[0:4] = b"\x7fELF"
            header[4] = 2
            header[5] = 1
            header[16:18] = (3).to_bytes(2, "little")
            header[18:20] = (243).to_bytes(2, "little")
            elf.write_bytes(bytes(header))
            # The export line comes from the wrapper, not from this test: that is the line
            # whose contents the regression is about.
            exports = [
                line for line in wrapper.splitlines() if line.startswith("export -f ")
            ]
            self.assertEqual(len(exports), 1, exports)
            setup = work / "helpers.sh"
            setup.write_text(
                "\n\n".join(helpers)
                + '\nexport PHYSICAL_FACTS="%s/facts" PHYSICAL_CHECKS="%s/checks.jsonl"\n' % (work, work)
                + ": > \"$PHYSICAL_FACTS\"; : > \"$PHYSICAL_CHECKS\"\n"
                + exports[0] + "\n",
                encoding="utf-8",
            )
            path = work / "PATH"
            path.mkdir()
            for name in ("bash", "sh", "node", "grep", "cat", "head", "awk", "printf"):
                target = shutil.which(name)
                if target:
                    (path / name).symlink_to(target)
            script = (
                'source "%s"\n'
                'bash -c \'set -euo pipefail; physical_elf "$1"\' _ "%s"\n'
            ) % (setup, elf)
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True, text=True, timeout=120,
                env=dict(os.environ, PATH=str(path)),
            )
            checks = (work / "checks.jsonl").read_text(encoding="utf-8")
            facts = (work / "facts").read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("command not found", result.stdout + result.stderr)
        self.assertIn("file' is missing", result.stdout + result.stderr)
        self.assertIn('"name":"elf"', checks.replace(" ", ""))
        self.assertIn("elf-header(degraded)", facts)

    def test_the_board_model_is_read_beyond_proc_cpuinfo(self):
        # /proc/cpuinfo is often hidden inside the container a runner lives in, and not
        # every riscv64 kernel has `model name`; a real board carries a device tree.
        outputs = render_physical(self.tmp)
        wrapper = read(outputs["steps/physical.sh"])
        self.assertIn("/proc/device-tree/model", wrapper)
        for key in ("uarch", "Processor"):
            self.assertIn(key, wrapper)

class PhysicalConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-physical-stat-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _stat_problems(self, text):
        """The validator's answer for a stat file: a list of problems, empty when fine."""
        recipe = self.tmp / "packages" / "demo" / "1.0.0"
        recipe.mkdir(parents=True, exist_ok=True)
        (recipe / rl.STAT_FILE).write_text(textwrap.dedent(text), encoding="utf-8")
        target = rl.find_target(self.tmp / "packages", "demo", "1.0.0")
        return rl.validate_stat(target, rl.load_stat(target))

    def test_a_registry_is_required(self):
        problems = self._stat_problems(
                """\
                upstream:
                  repo: https://github.com/example/demo
                  ref: v1.0.0
                npm_publish:
                  - role: main
                    name: demo
                    version: 1.0.0
                    dir: .
                physical:
                  auth: none
                """
            )
        self.assertTrue(any("physical.registry" in p for p in problems), problems)

    def test_an_unknown_auth_kind_is_refused(self):
        problems = self._stat_problems(
                """\
                upstream:
                  repo: https://github.com/example/demo
                  ref: v1.0.0
                npm_publish:
                  - role: main
                    name: demo
                    version: 1.0.0
                    dir: .
                physical:
                  registry: https://registry.example.test/
                  auth: oauth
                """
            )
        self.assertTrue(any("physical.auth" in p for p in problems), problems)

    def test_an_unknown_source_is_refused(self):
        problems = self._stat_problems(
                """\
                upstream:
                  repo: https://github.com/example/demo
                  ref: v1.0.0
                npm_publish:
                  - role: main
                    name: demo
                    version: 1.0.0
                    dir: .
                physical:
                  registry: https://registry.example.test/
                  source: git
                """
            )
        self.assertTrue(any("physical.source" in p for p in problems), problems)

    def test_a_token_env_must_name_what_it_reads(self):
        problems = self._stat_problems(
                """\
                upstream:
                  repo: https://github.com/example/demo
                  ref: v1.0.0
                npm_publish:
                  - role: main
                    name: demo
                    version: 1.0.0
                    dir: .
                physical:
                  registry: https://registry.example.test/
                  auth: token
                """
            )
        self.assertTrue(any("physical.token_env" in p for p in problems), problems)

    def test_the_defaults_are_the_open_group(self):
        problems = self._stat_problems(
            """\
            upstream:
              repo: https://github.com/example/demo
              ref: v1.0.0
            artifacts:
              path: build/Release/*.node
            npm_publish:
              - role: main
                name: demo
                version: 1.0.0
                dir: .
            physical:
              registry: https://registry.example.test/
            """
        )
        self.assertEqual(problems, [], problems)
        # Absent auth/source mean the open group and the registry: a recipe that names
        # only a registry still renders, and the defaults are the ones the runner uses.
        target = rl.find_target(self.tmp / "packages", "demo", "1.0.0")
        doc = rl.load_stat(target)
        self.assertNotIn("auth", doc["physical"])
        self.assertEqual(rl.PHYSICAL_AUTH_NONE, "none")
        self.assertEqual(rl.DEFAULT_PHYSICAL_SOURCE, "registry")


STUB_NPM = """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  pack)
    if [ "${STUB_PACK_FAILS:-}" = "true" ]; then
      echo "stub npm: cannot reach the registry" >&2
      exit 1
    fi
    # A real `npm pack` writes the tarball into --pack-destination and prints JSON.
    dest=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --pack-destination) dest="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    name="${STUB_TARBALL_NAME:-demo-1.0.0.tgz}"
    if [ -n "$dest" ] && [ -f "${STUB_TARBALL_SOURCE:-}" ]; then
      cp "${STUB_TARBALL_SOURCE}" "$dest/$name"
    fi
    printf '%s' "${STUB_PACK_JSON}"
    ;;
  install)
    # The consumer install: the stub records that it was reached.
    if [ "${STUB_INSTALL_FAILS:-}" = "true" ]; then
      echo "stub npm: install failed" >&2
      exit 1
    fi
    mkdir -p "node_modules/${STUB_PKG_NAME:-demo}"
    printf '%s' "${STUB_MANIFEST:-}" > "node_modules/${STUB_PKG_NAME:-demo}/package.json"
    ;;
  -v|--version)
    echo "9.2.0"
    ;;
  *)
    echo "stub npm: unexpected command $1" >&2
    exit 1
    ;;
esac
"""


class InstallStepTests(unittest.TestCase):
    """The generated install step, driven by a stub npm (no registry, no network)."""

    PAYLOAD = b"pretend this is the published tarball\n"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-install-step-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stage_recipe(self.tmp)
        self.outputs = render_physical(self.tmp)
        self.install = self.outputs["steps/install.sh"]
        self.integrity = "sha512-" + base64.b64encode(
            hashlib.sha512(self.PAYLOAD).digest()
        ).decode()
        self.tarball = self.tmp / "demo-1.0.0.tgz"
        self.tarball.write_bytes(self.PAYLOAD)
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        npm = self.stub / "npm"
        npm.write_text(STUB_NPM)
        npm.chmod(0o755)
        self.work = self.tmp / "work"
        self.work.mkdir()
        # The wrapper creates the consumer project before it runs the step; the step
        # installs into it.
        self.consumer = self.work / "consumer"
        self.consumer.mkdir(parents=True)

    DRIVER = """#!/usr/bin/env bash
set -euo pipefail
# The wrapper exports these collectors (export -f) before it runs the recipes'
# snippets, so the step under test is driven the same way here.
physical_fact() { printf '%s\\t%s\\n' "$1" "$2" >>"$STUB_FACTS"; }
physical_check() { printf '%s\\t%s\\t%s\\n' "$1" "$2" "$3" >>"$STUB_CHECKS"; }
export -f physical_fact physical_check
bash "$STUB_STEP"
"""

    def run_install(self, env=None):
        driver = self.tmp / "driver.sh"
        driver.write_text(textwrap.dedent(self.DRIVER))
        (self.tmp / "facts.tsv").write_text("")
        (self.tmp / "checks.jsonl").write_text("")
        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (self.stub, environment.get("PATH", ""))
        environment.update(
            {
                "PHYSICAL_WORK": str(self.work),
                "PHYSICAL_CONSUMER": str(self.consumer),
                "PHYSICAL_REGISTRY": "https://registry.example.test/",
                "PHYSICAL_EXPECTED_INTEGRITY": self.integrity,
                "PHYSICAL_SOURCE": "registry",
                "PHYSICAL_TARBALL": str(self.tarball),
                "PHYSICAL_FALLBACK": "false",
                "STUB_PACK_JSON": json.dumps(
                    [{"name": "demo", "version": "1.0.0", "filename": "demo-1.0.0.tgz"}]
                ),
                "STUB_TARBALL_SOURCE": str(self.tarball),
                "STUB_TARBALL_NAME": "demo-1.0.0.tgz",
                "STUB_MANIFEST": json.dumps({"name": "@demo/demo", "version": "1.0.0"}),
                "STUB_PKG_NAME": "@demo/demo",
            }
        )
        environment.update(env or {})
        environment.setdefault("STUB_STEP", str(self.install))
        environment.setdefault("STUB_FACTS", str(self.tmp / "facts.tsv"))
        environment.setdefault("STUB_CHECKS", str(self.tmp / "checks.jsonl"))
        return subprocess.run(
            ["bash", str(driver)],
            capture_output=True, text=True, env=environment,
        )

    def test_a_matching_registry_tarball_passes(self):
        result = self.run_install({"STUB_PACK_FAILS": "false"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("installed @demo/demo@1.0.0", result.stdout)
        # The consumer install ran: optionalDependencies are exercised by npm itself.
        self.assertTrue((self.consumer / "node_modules" / "@demo" / "demo").is_dir())

    def test_a_registry_the_machine_cannot_read_is_not_an_artifact_problem(self):
        # The report holds a digest, so the registry does serve these bytes; failing to
        # download them is this machine's problem and must not read as "the artifact is
        # broken" (and must not silently install the build's tarball instead).
        result = self.run_install({"STUB_PACK_FAILS": "true"})
        self.assertEqual(result.returncode, 11, result.stderr)
        self.assertIn("network or credential problem", result.stderr)
        self.assertFalse((self.consumer / "node_modules" / "@demo" / "demo").exists())

    def test_the_fallback_tarball_is_used_when_the_report_is_unverified(self):
        result = self.run_install({"STUB_PACK_FAILS": "true", "PHYSICAL_FALLBACK": "true"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("using the build's tarball", result.stdout)

    def test_a_fallback_that_is_absent_is_reported_as_a_registry_problem(self):
        result = self.run_install(
            {"STUB_PACK_FAILS": "true", "PHYSICAL_FALLBACK": "true", "PHYSICAL_TARBALL": ""}
        )
        self.assertEqual(result.returncode, 11, result.stderr)
        self.assertIn("none was given", result.stderr)

    def test_the_fallback_tarball_is_still_checked_against_the_report(self):
        # Installing the build's own tarball is not an excuse to skip the digest: the
        # report says which bytes were uploaded.
        other = self.tmp / "other.tgz"
        other.write_bytes(b"not the published tarball\n")
        result = self.run_install(
            {
                "STUB_PACK_FAILS": "true",
                "PHYSICAL_FALLBACK": "true",
                "PHYSICAL_TARBALL": str(other),
            }
        )
        self.assertEqual(result.returncode, 12, result.stderr)
        self.assertIn("not the ones the pipeline published", result.stderr)

    def test_a_changed_registry_tarball_is_a_digest_failure(self):
        other = self.tmp / "other.tgz"
        other.write_bytes(b"the mirror restored something else\n")
        result = self.run_install({"STUB_TARBALL_SOURCE": str(other)})
        self.assertEqual(result.returncode, 12, result.stderr)

    def test_the_answer_is_read_in_both_npm_shapes(self):
        # npm <= 10 prints an array, npm >= 11 a bare object.
        object_shaped = json.dumps(
            {"demo": {"name": "demo", "version": "1.0.0", "filename": "demo-1.0.0.tgz"}}
        )
        result = self.run_install({"STUB_PACK_JSON": object_shaped})
        self.assertEqual(result.returncode, 0, result.stderr)


class PreflightTests(unittest.TestCase):
    """The readiness report the runner prints, and the install advice it gives.

    A hardware run must never stop because a tool is missing - it reports the machine as
    the environment verdict and keeps going - so these tests assert the script exits zero
    and names the fix, including the no-sudo path.
    """

    SCRIPT = ROOT / "scripts/physical_preflight.sh"

    def run_preflight(self, path=None):
        env = dict(os.environ)
        if path is not None:
            env["PATH"] = str(path)
        return subprocess.run(
            ["bash", str(self.SCRIPT)], capture_output=True, text=True, env=env, timeout=120
        )

    def test_it_always_exits_zero_even_with_nothing_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Only bash: no node, no npm, no coreutils, not even uname.
            bare = Path(tmp)
            for name in ("bash", "sh"):
                target = shutil.which(name)
                if target:
                    (bare / name).symlink_to(target)
            result = self.run_preflight(bare)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("readiness:", result.stdout)
        self.assertIn("node >= 20", result.stdout)

    def test_a_missing_recommended_tool_says_what_to_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp)
            for name in ("bash", "sh", "uname", "node", "npm", "awk", "mktemp", "df",
                         "getconf", "date", "grep", "sed", "tar", "head", "cat", "flock"):
                target = shutil.which(name)
                if target:
                    (staged / name).symlink_to(target)
            result = self.run_preflight(staged)   # `file` is deliberately absent
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("recommended file: MISSING", result.stdout)
        # The advice has to be actionable in a container somebody else owns.
        self.assertRegex(
            result.stdout,
            r"apt-get install -y file|dnf install -y file|apk add",
        )
        self.assertIn("no sudo", result.stdout)

    def test_the_summary_table_names_the_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            subprocess.run(
                ["bash", str(self.SCRIPT), "--summary", str(summary)],
                capture_output=True, text=True, check=True, timeout=120,
            )
            text = summary.read_text(encoding="utf-8")
        self.assertIn("| runner check | result |", text)
        self.assertIn("| readiness |", text)

    def test_it_names_this_machine_when_it_is_not_riscv64(self):
        result = self.run_preflight()
        # The rehearsal host is not riscv64, so this is the branch that runs here.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("required uname:", result.stdout)
        self.assertNotIn("readiness: ok", result.stdout)


class PlanHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-plan-helper-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.reports = self.tmp / "reports"
        self.reports.mkdir()

    def write_report(self, doc, name=None):
        (self.reports / (name or doc.get("name", doc.get("pkg")) + ".json")).write_text(
            json.dumps(doc, indent=2) + "\n", encoding="utf-8"
        )

    def run_helper(self, *args):
        return subprocess.run(
            ["node", str(PLAN_HELPER)] + list(args),
            capture_output=True, text=True,
        )

    def publish_report(self, served="dist.integrity", integrity="sha512-x", tarball=""):
        return {
            "name": "demo",
            "version": "1.0.0",
            "registry": "https://registry.example.test/",
            "integrity": integrity,
            "shasum": "shasum",
            "size": 10,
            "served": served,
            "tarball": tarball,
        }

    def test_the_plan_carries_what_the_hardware_run_reads(self):
        self.write_report(self.publish_report())
        out = self.tmp / "plan.json"
        result = self.run_helper("--merge", str(self.reports), "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(out.read_text())
        self.assertEqual(plan["demo"]["integrity"], "sha512-x")
        self.assertEqual(plan["demo"]["served"], "dist.integrity")
        self.assertIn("demo@1.0.0", result.stdout)

    def test_a_report_without_a_digest_is_marked_not_dropped(self):
        # The hardware run can still install and exercise the package; it just cannot
        # prove the bytes are the published ones, and the report says so.
        self.write_report(self.publish_report(integrity=""))
        out = self.tmp / "plan.json"
        self.run_helper("--merge", str(self.reports), "--out", str(out))
        self.assertTrue(json.loads(out.read_text())["demo"]["digest_missing"])

    def test_the_summary_reads_hardware_reports(self):
        self.write_report(
            {
                "pkg": "demo",
                "ver": "1.0.0",
                "verdict": "execute",
                "expected_integrity": "sha512-x",
                "installed_integrity": "sha512-x",
                "binding": "/w/x/prebuilds/linux-riscv64/demo.node",
                "host": {"uname_m": "riscv64", "model": "Sophgo", "glibc": "glibc 2.38"},
                "checks": [{"name": "load", "result": "error", "detail": "illegal instruction"}],
            },
            name="physical-report-demo.json",
        )
        result = self.run_helper("--summary", str(self.reports))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("`demo@1.0.0`", result.stdout)
        self.assertIn("execute", result.stdout)
        self.assertIn("load: illegal instruction", result.stdout)

    def test_a_failure_makes_the_verdict_step_fail(self):
        self.write_report({"pkg": "demo", "ver": "1.0.0", "verdict": "ok"}, name="a.json")
        self.write_report({"pkg": "other", "ver": "1.0.0", "verdict": "install"}, name="b.json")
        result = self.run_helper("--verdict", str(self.reports))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("other", result.stderr)

    def test_clean_runs_pass_the_verdict_step(self):
        self.write_report({"pkg": "demo", "ver": "1.0.0", "verdict": "ok"}, name="a.json")
        self.write_report({"pkg": "other", "ver": "1.0.0", "verdict": "skipped"}, name="b.json")
        result = self.run_helper("--verdict", str(self.reports))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_reports_is_not_a_failure_of_the_helper(self):
        result = self.run_helper("--merge", str(self.reports), "--out", str(self.tmp / "p.json"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no publish report", result.stdout)


class PublishReportTests(unittest.TestCase):
    """The publish step's report is the input of every hardware decision."""

    STUB = """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  pack)  printf '%s' "$STUB_PACK_JSON" ;;
  view)  printf '%s' "$STUB_VIEW_JSON" ;;
  *)     exit 1 ;;
esac
"""

    PAYLOAD = b"pretend this is a tarball\n"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-publish-report-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg_dir = self.tmp / "pkg"
        self.pkg_dir.mkdir()
        (self.pkg_dir / "package.json").write_text(
            json.dumps({"name": "demo", "version": "1.0.0"}, indent=2) + "\n"
        )
        self.stub_dir = self.tmp / "stub"
        self.stub_dir.mkdir()
        stub = self.stub_dir / "npm"
        stub.write_text(self.STUB)
        stub.chmod(0o755)
        self.report = self.tmp / "report.json"
        self.served = {}
        outer = self
        payload = self.PAYLOAD

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    @property
    def tarball_url(self):
        return "http://127.0.0.1:%d/demo-1.0.0.tgz" % self.server.server_address[1]

    def spec(self):
        return {
            "name": "demo",
            "version": "1.0.0",
            "registry": "https://registry.example.test/",
            "dir": ".",
            "kind": "source-tree",
            "manifest": "upstream",
            "optional": {},
        }

    def run_helper(self, view, accepted=False, report=True):
        environment = dict(os.environ)
        for key in ("PUBLISH_VERIFY", "PUBLISH_VERIFY_MAX_BYTES", "NPM_CONFIG_USERCONFIG"):
            environment.pop(key, None)
        environment["PATH"] = "%s:%s" % (self.stub_dir, environment.get("PATH", ""))
        environment["STUB_PACK_JSON"] = json.dumps([{"integrity": self.integrity, "files": []}])
        environment["STUB_VIEW_JSON"] = json.dumps(view)
        argv = ["node", str(PUBLISH_HELPER), "--assert-served", json.dumps(self.spec()), str(self.pkg_dir)]
        if accepted:
            argv.append("--accepted")
        if report:
            argv += ["--report", str(self.report)]
        return subprocess.run(argv, capture_output=True, text=True, env=environment, cwd=str(self.tmp))

    @property
    def integrity(self):
        return "sha512-" + base64.b64encode(hashlib.sha512(self.PAYLOAD).digest()).decode()

    def test_a_confirmed_digest_is_written_to_the_report(self):
        result = self.run_helper({"dist": {"integrity": self.integrity}})
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(self.report.read_text())
        self.assertEqual(doc["served"], "dist.integrity")
        self.assertEqual(doc["integrity"], self.integrity)
        self.assertEqual(doc["accepted"], False)
        self.assertEqual(doc["name"], "demo")

    def test_an_accepted_upload_with_no_digest_is_unverified_not_a_failure(self):
        # What the hardware run needs to know: the upload worked, nobody can say which
        # bytes the registry serves - so a tarball is worth keeping.
        result = self.run_helper({"dist": {}}, accepted=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(self.report.read_text())
        self.assertEqual(doc["served"], "unverified")
        self.assertEqual(doc["accepted"], True)
        # The report must keep the reason: a reader decides from it whether a fallback
        # tarball is worth keeping.
        self.assertIn("no digest", doc["detail"])

    def test_a_downloaded_tarball_is_reported_as_the_evidence(self):
        result = self.run_helper({"dist": {"tarball": self.tarball_url}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.report.read_text())["served"], "tarball")

    def test_a_mismatch_records_what_the_registry_serves(self):
        result = self.run_helper({"dist": {"integrity": "sha512-someone-else"}})
        self.assertEqual(result.returncode, 1)
        doc = json.loads(self.report.read_text())
        self.assertEqual(doc["served"], "mismatch")
        self.assertIn("sha512-someone-else", doc["detail"])
        # The digest this pipeline uploaded is recorded too: it is what the report of a
        # mismatch is useful for.
        self.assertEqual(doc["integrity"], self.integrity)

    def test_report_is_optional(self):
        result = self.run_helper({"dist": {"integrity": self.integrity}}, report=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.report.exists())

    def test_a_report_without_a_path_is_refused(self):
        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (self.stub_dir, environment.get("PATH", ""))
        environment["STUB_PACK_JSON"] = json.dumps(
            [{"integrity": self.integrity, "files": []}]
        )
        environment["STUB_VIEW_JSON"] = json.dumps({"dist": {"integrity": self.integrity}})
        result = subprocess.run(
            ["node", str(PUBLISH_HELPER), "--assert-served", json.dumps(self.spec()), str(self.pkg_dir), "--report"],
            capture_output=True, text=True, env=environment, cwd=str(self.tmp),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--report", result.stderr)


def real_recipes():
    """(package name, version, recipe directory) for every recipe in this repository.

    Scoped packages nest one directory deeper (`packages/@scope/name/version`), so the
    package name is reconstructed from the path rather than assumed.
    """
    found = []
    for stat in sorted((ROOT / "packages").glob("**/.stat")):
        parts = stat.parent.relative_to(ROOT / "packages").parts
        if len(parts) == 2:
            found.append((parts[0], parts[1], stat.parent))
        elif len(parts) == 3:
            found.append(("%s/%s" % (parts[0], parts[1]), parts[2], stat.parent))
    return found


class WorkflowArtifactTests(unittest.TestCase):
    """The build -> hardware handoff, as the workflows actually wire it.

    Two failure modes were paid for by a red run in the fork, and neither is visible in
    the Python or the shell: `actions/upload-artifact` skips hidden files and
    directories (so a ".physical-action" path uploaded nothing), and it rejects `/` in
    an artifact name (so `matrix.target.slug`, which is "pkg/version", cannot be used).
    """

    INVALID_NAME_CHARS = set('/\\:*?"<>|')
    BUILD = ROOT / ".github/workflows/build.yml"
    PHYSICAL = ROOT / ".github/workflows/verify-physical.yml"

    def steps(self, path):
        doc = __import__("yaml").safe_load(path.read_text(encoding="utf-8"))
        for job in doc["jobs"].values():
            for step in job.get("steps") or []:
                yield job, step

    def test_uploaded_paths_are_not_hidden(self):
        for job, step in self.steps(self.BUILD):
            if not str(step.get("uses", "")).startswith("actions/upload-artifact"):
                continue
            path = str((step.get("with") or {}).get("path", ""))
            hidden = [
                part for part in path.split("/")
                if part.startswith(".") and part not in (".", "..")
            ]
            self.assertEqual(
                hidden, [],
                "%s uploads the hidden path %r, which upload-artifact skips"
                % (step.get("name"), path),
            )

    def test_the_workflow_checks_the_runner_with_the_preflight(self):
        # One place decides what a runner needs. If the workflow grew its own copy of that
        # list again, the install advice in the docs could drift away from what runs.
        runs = []
        for job, step in self.steps(self.PHYSICAL):
            runs.append(str(step.get("run", "")))
        joined = "\n".join(runs)
        self.assertIn("scripts/physical_preflight.sh", joined)
        self.assertIn("GITHUB_STEP_SUMMARY", joined)

    def test_artifact_names_carry_no_slash(self):
        for job, step in self.steps(self.BUILD):
            if not str(step.get("uses", "")).startswith("actions/upload-artifact"):
                continue
            name = str((step.get("with") or {}).get("name", ""))
            literal = name.split("${{")[0]
            self.assertEqual(
                sorted(set(literal) & self.INVALID_NAME_CHARS), [],
                "artifact name %r has an invalid character" % name,
            )
            # `matrix.target.slug` is "pkg/version"; `matrix.target.artifact` is the
            # slugified form (no slash, scoped packages included).
            self.assertNotIn("matrix.target.slug", name,
                             "artifact name %r would contain a slash" % name)

    def test_the_hardware_run_reads_the_names_the_build_writes(self):
        download = [
            str((step.get("with") or {}).get("pattern", ""))
            for job, step in self.steps(self.PHYSICAL)
            if str(step.get("uses", "")).startswith("actions/download-artifact")
        ]
        uploaded = [
            str((step.get("with") or {}).get("name", ""))
            for job, step in self.steps(self.BUILD)
            if str(step.get("uses", "")).startswith("actions/upload-artifact")
        ]
        self.assertTrue(download and uploaded)
        for pattern in download:
            prefix = pattern.rstrip("*")
            self.assertTrue(
                any(name.startswith(prefix) for name in uploaded),
                "no uploaded artifact matches %r (uploaded: %s)" % (pattern, uploaded),
            )

    def test_no_local_directory_the_workflows_use_is_hidden(self):
        for job, step in self.steps(self.PHYSICAL):
            run = str(step.get("run", ""))
            for part in run.split():
                if part.startswith(".") and len(part) > 1 and part[1].isalpha():
                    self.fail("hidden path in a run block: %s" % part)


class ScopeSlugTests(unittest.TestCase):
    def test_a_scoped_package_yields_a_slash_free_artifact_name(self):
        target = rl.Target(pkg="@scope/demo", ver="2.0.0", path=Path("/tmp/x"))
        self.assertNotIn("/", target.artifact_name)
        self.assertNotIn("@", target.artifact_name)
        self.assertEqual(target.artifact_name, "scope-demo-2.0.0-riscv64")


class RealRecipeTests(unittest.TestCase):
    """The recipes in this repository carry a hardware step that renders."""

    PACKAGES = real_recipes()

    def test_every_recipe_renders_for_hardware(self):
        for pkg, ver, recipe in self.PACKAGES:
            with self.subTest(recipe=str(recipe.relative_to(ROOT))):
                rendered, scripts = render.render_target(
                    ROOT / "packages", pkg, ver, PHYSICAL_TEMPLATE, {}
                )
                self.assertIn("verify on hardware", rendered)
                self.assertEqual(sorted(scripts), ["install", "physical", "verify"])

    def test_the_read_registry_is_the_group_not_the_publish_repository(self):
        for pkg, ver, recipe in self.PACKAGES:
            with self.subTest(recipe=str(recipe.relative_to(ROOT))):
                doc = rl.load_stat(rl.find_target(ROOT / "packages", pkg, ver))
                registry = doc["physical"]["registry"]
                self.assertTrue(registry.startswith("https://"), registry)
                self.assertTrue(registry.rstrip("/").endswith("npm-riscv-group"), registry)
                self.assertNotIn("npm-riscv-local", registry)

    def test_the_verification_steps_are_scripts_that_parse(self):
        for pkg, ver, recipe in self.PACKAGES:
            verify = recipe / "commands" / "verify"
            self.assertTrue(verify.is_file())
            self.assertTrue(os.access(verify, os.X_OK), "%s is not executable" % verify)
            result = subprocess.run(["bash", "-n", str(verify)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_verification_does_not_reach_into_the_build_tree(self):
        # A hardware run installs what consumers install; reading ../src would verify
        # something nobody can get from a registry.
        for pkg, ver, recipe in self.PACKAGES:
            text = read(recipe / "commands" / "verify")
            with self.subTest(recipe=str(recipe.relative_to(ROOT))):
                self.assertNotIn("../src", text)
                self.assertIn("PHYSICAL_WORK", text)


if __name__ == "__main__":
    unittest.main()
