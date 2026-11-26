"""Recipe model shared by the renderer and the change detector.

A recipe lives at::

    packages/<pkg>/<version>/            # <pkg> may be scoped: @scope/name
        .stat                            # required metadata (YAML)
        commands/<name>                  # optional: fetch, patch, build, test, publish
        patches/*.patch                  # optional
        patches/.disable                 # optional: patch file names to skip

``packages/<pkg>/common/`` is a special version directory holding snippets and
patches shared by every version of that package. It is never built itself and
never carries a ``.stat``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

SNIPPET_NAMES = ("fetch", "patch", "build", "test", "publish", "install", "verify")
REQUIRED_SNIPPETS = ("build", "test")

#: Recipes are rendered into two kinds of action, and each kind needs a different set
#: of recipe-authored snippets. The build template renders the five steps of the
#: pipeline; the physical template renders install (generated unless the recipe has
#: its own) and verify (always recipe-authored, because what proves the artifact works
#: on hardware differs per package).
TEMPLATE_SNIPPETS = {
    "action.template.yml": REQUIRED_SNIPPETS,
    "physical.template.yml": ("verify",),
}


def required_snippets_for(template_name: str) -> tuple[str, ...]:
    """Which recipe snippets a template needs. Unknown templates need the five steps."""
    return TEMPLATE_SNIPPETS.get(template_name, REQUIRED_SNIPPETS)
COMMON_DIR = "common"
STAT_FILE = ".stat"
DISABLE_FILE = ".disable"
ROLES = ("binary", "main", "binding")
#: Roles that publish a platform-specific package. ``binding`` is the upstream term
#: (rolldown and other napi-rs projects); ``binary`` is the older spelling here.
PLATFORM_ROLES = ("binary", "binding")

#: Where a published package comes from. ``source-tree`` is the directory the build
#: produced; ``upstream-tarball`` is the upstream npm package, pinned by integrity and
#: extended with the recipe's riscv64 overlay (.dsh/plan.md D33).
PUBLISH_KINDS = ("source-tree", "upstream-tarball")
DEFAULT_PUBLISH_KIND = "source-tree"

#: How the published manifest is obtained. ``generated`` writes name/version from the
#: recipe; ``upstream`` publishes the package as it is (asserting name/version);
#: ``template`` uses a recipe file (npm_publish[].manifest_template).
MANIFEST_SHAPES = ("generated", "upstream", "template")


def publish_kind(entry: dict) -> str:
    """How the package to publish is produced.

    ``base.kind`` is the documented spelling (.dsh/plan.md D33); a top-level ``kind``
    is accepted as the shorter form. ``source-tree`` keeps the older behaviour of
    publishing the directory the build produced.
    """
    base = entry.get("base")
    base_kind = base.get("kind") if isinstance(base, dict) else None
    return str(entry.get("kind") or base_kind or DEFAULT_PUBLISH_KIND)

#: Upstream increments (patches meant for an upstream PR) live here, never in patches/.
UPSTREAM_INCREMENT_DIR = "upstream"

#: How the recipe is built. A native recipe compiles inside a riscv64
#: container (qemu); a cross recipe compiles on the host architecture with a
#: riscv64 cross toolchain and is then tested in the riscv64 container.
TOOLCHAIN_NATIVE = "native"
TOOLCHAIN_CROSS = "cross"
TOOLCHAIN_KINDS = (TOOLCHAIN_NATIVE, TOOLCHAIN_CROSS)
DEFAULT_TOOLCHAIN = TOOLCHAIN_NATIVE

# How the publish step authenticates to a registry. "token" writes an
# ``_authToken`` line (npm bearer token, GitHub Packages, Nexus npm tokens);
# "basic" writes ``_auth`` with the base64 of ``user:password``, which is what a
# registry answering ``WWW-Authenticate: Basic`` expects.
PUBLISH_AUTH_TOKEN = "token"
PUBLISH_AUTH_BASIC = "basic"
PUBLISH_AUTH_KINDS = (PUBLISH_AUTH_TOKEN, PUBLISH_AUTH_BASIC)
DEFAULT_PUBLISH_AUTH = PUBLISH_AUTH_TOKEN

#: How the hardware run reaches the registry it installs from (.dsh/plan.md D41).
#: The read registry need not be the publish registry: consumers point at a group
#: repository that aggregates the private one, and that address is configuration, not
#: a secret. "none" is a real answer - an open group needs no credential - and it is
#: kept distinct from "token"/"basic" so a read credential is never inherited from the
#: publish side by accident.
PHYSICAL_AUTH_NONE = "none"
PHYSICAL_AUTH_KINDS = (PHYSICAL_AUTH_NONE, PUBLISH_AUTH_TOKEN, PUBLISH_AUTH_BASIC)
PHYSICAL_SOURCE_REGISTRY = "registry"
PHYSICAL_SOURCE_TARBALL = "tarball"
PHYSICAL_SOURCES = (PHYSICAL_SOURCE_REGISTRY, PHYSICAL_SOURCE_TARBALL)
DEFAULT_PHYSICAL_SOURCE = PHYSICAL_SOURCE_REGISTRY

#: Exit codes the generated physical wrapper maps to verdicts. A hardware run has to
#: distinguish "the machine cannot reach the registry" from "the artifact is wrong":
#: the first is an environment problem, the second is news about the package.
EXIT_ENVIRONMENT = 10
EXIT_REGISTRY = 11
EXIT_DIGEST = 12
EXIT_INSTALL = 13
EXIT_EXECUTE = 14

#: Reference names that move and therefore cannot pin a source tree.
MUTABLE_REFS = frozenset(
    {"main", "master", "develop", "dev", "head", "latest", "trunk", "next"}
)


class RecipeError(Exception):
    """Raised when a recipe is malformed."""


@dataclass(frozen=True)
class Target:
    """A buildable (package, version) pair.

    ``mode`` says what this target needs from the workflow:

    * ``build`` - the artifact changed (or the recipe did): fetch, patch, build, test,
      then publish;
    * ``publish`` - only the publish step changed: the tree is the same one the cache
      already holds, so the workflow restores it and delivers it again without
      rebuilding or re-testing.
    """

    pkg: str
    ver: str
    path: Path
    mode: str = "build"

    @property
    def slug_pkg(self) -> str:
        """Filesystem/identifier-safe form of the package name."""
        return slugify(self.pkg)

    @property
    def slug_ver(self) -> str:
        return slugify(self.ver)

    @property
    def artifact_name(self) -> str:
        return f"{self.slug_pkg}-{self.slug_ver}-riscv64"

    @property
    def ident(self) -> str:
        return f"{self.pkg}@{self.ver}"


def slugify(value: str) -> str:
    """Turn ``@scope/name`` into ``scope-name``.

    ``@`` and ``/`` are not usable in job names, artifact names or directory
    names, and ``.``/``-`` are kept so versions stay readable.
    """
    out = value[1:] if value.startswith("@") else value
    out = out.replace("/", "-")
    keep = []
    for ch in out:
        keep.append(ch if (ch.isalnum() or ch in ".-_") else "-")
    return "".join(keep)


def split_recipe_path(relative: str) -> tuple[str, str] | None:
    """Parse a path below ``packages/`` into ``(pkg, version)``.

    ``rolldown/1.0.0/...``      -> ("rolldown", "1.0.0")
    ``@scope/name/1.0.0/...``   -> ("@scope/name", "1.0.0")
    ``@scope`` and anything else that is not a concrete version -> None
    """
    parts = [p for p in relative.split("/") if p]
    if not parts:
        return None
    if parts[0].startswith("@"):
        if len(parts) < 3:
            return None
        return f"{parts[0]}/{parts[1]}", parts[2]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def package_names(root: Path) -> list[str]:
    """Every package name under ``root``, in sorted order (scopes expanded)."""
    names: list[str] = []
    if not root.is_dir():
        return names
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("@"):
            for sub in sorted(entry.iterdir()):
                if sub.is_dir():
                    names.append(f"{entry.name}/{sub.name}")
        else:
            names.append(entry.name)
    return names


def versions_of(root: Path, pkg: str) -> list[str]:
    """Version directories of ``pkg`` (``common`` excluded), sorted."""
    pkg_dir = root.joinpath(*pkg.split("/"))
    if not pkg_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in pkg_dir.iterdir()
        if entry.is_dir() and entry.name != COMMON_DIR
    )


def enumerate_targets(root: Path) -> list[Target]:
    targets: list[Target] = []
    for pkg in package_names(root):
        pkg_dir = root.joinpath(*pkg.split("/"))
        for ver in versions_of(root, pkg):
            targets.append(Target(pkg=pkg, ver=ver, path=pkg_dir / ver))
    return targets


def find_target(root: Path, pkg: str, ver: str) -> Target:
    path = root.joinpath(*pkg.split("/")) / ver
    if not path.is_dir():
        raise RecipeError(f"recipe directory not found: {path}")
    return Target(pkg=pkg, ver=ver, path=path)


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------


def toolchain_kind(stat: dict) -> str:
    """Return the toolchain a recipe asks for (native when unspecified)."""
    value = str(stat.get("toolchain") or DEFAULT_TOOLCHAIN).strip().lower()
    return value if value in TOOLCHAIN_KINDS else DEFAULT_TOOLCHAIN


def load_stat(target: Target) -> dict:
    """Load and minimally shape ``.stat``."""
    stat_path = target.path / STAT_FILE
    if not stat_path.is_file():
        raise RecipeError(f"{target.ident}: missing {STAT_FILE} ({stat_path})")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RecipeError("PyYAML is required to read .stat") from exc

    text = stat_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RecipeError(f"{target.ident}: cannot parse {STAT_FILE}: {exc}") from exc
    if not isinstance(data, dict):
        raise RecipeError(f"{target.ident}: {STAT_FILE} must be a mapping")
    return data


def is_mutable_ref(ref: str) -> bool:
    """True when ``ref`` is a branch-like name that can move under our feet."""
    cleaned = ref.strip().rstrip("/")
    if cleaned.startswith("refs/heads/"):
        return True
    base = cleaned.rsplit("/", 1)[-1]
    return base.lower() in MUTABLE_REFS


def validate_stat(target: Target, stat: dict) -> list[str]:
    """Return a list of human readable problems; empty when the recipe is sane."""
    problems: list[str] = []

    upstream = stat.get("upstream") or {}
    source = stat.get("source") or {}
    npm_src = stat.get("npm") or {}
    artifacts = stat.get("artifacts") or {}
    publish = stat.get("npm_publish") or []

    if not (upstream or source or npm_src):
        problems.append(
            "no source route: at least one of upstream / source / npm is required"
        )

    if upstream:
        repo = upstream.get("repo")
        ref = upstream.get("ref")
        if not repo:
            problems.append("upstream.repo is required")
        if not ref:
            problems.append("upstream.ref is required")
        elif is_mutable_ref(str(ref)):
            problems.append(
                f"upstream.ref '{ref}' is a mutable reference; "
                "use a tag or a commit SHA"
            )

    if source:
        url = source.get("url")
        sha = source.get("sha256")
        if not url:
            problems.append("source.url is required")
        elif is_mutable_ref(str(url)):
            problems.append(
                f"source.url '{url}' points at a mutable reference; "
                "pin a specific version"
            )
        if not sha:
            problems.append(
                "source.sha256 is required: an unpinned archive makes the "
                "source tree ambiguous"
            )
        elif len(str(sha)) != 64 or any(c not in "0123456789abcdefABCDEF" for c in str(sha)):
            problems.append("source.sha256 must be a 64 character hex digest")

    if npm_src:
        if not npm_src.get("name"):
            problems.append("npm.name is required")
        if not npm_src.get("version"):
            problems.append("npm.version is required")

    if not artifacts.get("path"):
        problems.append("artifacts.path is required (glob of the built artifact)")

    raw_toolchain = stat.get("toolchain")
    if raw_toolchain is not None and str(raw_toolchain).strip().lower() not in TOOLCHAIN_KINDS:
        problems.append(
            f"toolchain '{raw_toolchain}' is not supported; use one of {TOOLCHAIN_KINDS} "
            f"(omit the field for the default '{DEFAULT_TOOLCHAIN}')"
        )

    if not isinstance(publish, list) or not publish:
        problems.append(
            "npm_publish must be a non-empty list (at least one publish target)"
        )
    else:
        problems.extend(_validate_publish_entries(target, publish))

    problems.extend(_validate_upstream_increments(target, stat.get("upstream_increments")))
    problems.extend(validate_physical(target, stat.get("physical")))

    return problems


def validate_physical(target: Target, physical) -> list[str]:
    """Rules for the optional ``physical:`` section (hardware verification, D41).

    The section says where a hardware run installs from, not what it publishes, so it
    is validated separately from ``npm_publish``: a recipe may publish to a private
    registry with basic auth and read from an open group anonymously.
    """
    problems: list[str] = []
    if physical is None:
        return problems
    if not isinstance(physical, dict):
        return [
            "physical must be a mapping (registry, auth, source, token_env)"
        ]

    registry = str(physical.get("registry") or "").strip()
    if not registry:
        problems.append(
            "physical.registry is required: the hardware run installs from a registry, "
            "which need not be the one the recipe publishes to"
        )
    elif not registry.startswith(("https://", "http://")):
        problems.append(
            f"physical.registry must be an http(s) URL, got '{registry}'"
        )

    auth = str(physical.get("auth") or PHYSICAL_AUTH_NONE).strip().lower()
    if auth not in PHYSICAL_AUTH_KINDS:
        problems.append(
            f"physical.auth '{auth}' is not supported; use one of {PHYSICAL_AUTH_KINDS}"
        )
    elif auth != PHYSICAL_AUTH_NONE and not str(physical.get("token_env") or "").strip():
        problems.append(
            f"physical.auth '{auth}' needs physical.token_env: the variable holding "
            "the credential for the read registry"
        )

    source = str(physical.get("source") or DEFAULT_PHYSICAL_SOURCE).strip().lower()
    if source not in PHYSICAL_SOURCES:
        problems.append(
            f"physical.source '{source}' is not supported; use one of {PHYSICAL_SOURCES}"
        )

    return problems


def _validate_base(where: str, name: str, version: str, base) -> list[str]:
    """A publish base must be the upstream tarball of exactly this name and version."""
    problems: list[str] = []
    if not isinstance(base, dict):
        return [f"{where}.base must be a mapping with url and integrity"]
    url = str(base.get("url") or "")
    integrity = str(base.get("integrity") or "")
    if not url:
        problems.append(f"{where}.base.url is required")
    else:
        expected = f"{name}-{version}.tgz"
        if not url.endswith(expected):
            problems.append(
                f"{where}.base.url must end with '{expected}': the base is the upstream "
                "package for exactly this name and version, nothing else"
            )
        if not url.startswith(("https://", "http://")):
            problems.append(f"{where}.base.url must be an http(s) URL")
    if not integrity:
        problems.append(
            f"{where}.base.integrity is required: an unpinned base tarball makes the "
            "published package unverifiable"
        )
    elif not integrity.startswith("sha512-"):
        problems.append(
            f"{where}.base.integrity must be npm's sha512 form ('sha512-<base64>')"
        )
    return problems


def _validate_overlay(where: str, overlay: Iterable) -> list[str]:
    """Overlay entries are copied into the base package, so they must stay relative."""
    problems: list[str] = []
    for item in overlay:
        text = str(item)
        if text.startswith("/") or ".." in Path(text).parts:
            problems.append(
                f"{where}.overlay entry '{text}' must stay inside the source tree"
            )
        if any(ch.isspace() for ch in text):
            problems.append(f"{where}.overlay entry '{text}' must not contain whitespace")
    return problems


def _validate_upstream_increments(target: Target, increments) -> list[str]:
    """Upstream increments are documentation-grade patches; they never build anything."""
    problems: list[str] = []
    if increments is None:
        return problems
    if not isinstance(increments, list) or not increments:
        return ["upstream_increments must be a non-empty list when present"]
    for index, item in enumerate(increments):
        where = f"upstream_increments[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be a mapping")
            continue
        patch = str(item.get("patch") or "")
        if not patch:
            problems.append(f"{where}.patch is required")
        elif not patch.startswith(UPSTREAM_INCREMENT_DIR + "/"):
            problems.append(
                f"{where}.patch '{patch}' must live under '{UPSTREAM_INCREMENT_DIR}/': "
                "build-time patches belong in patches/ because they change the build, "
                "upstream increments do not"
            )
        elif not (target.path / patch).is_file():
            problems.append(f"{where}.patch not found: {target.path / patch}")
        if not isinstance(item.get("touches"), list) or not item.get("touches"):
            problems.append(f"{where}.touches must list the upstream files the patch edits")
        if not item.get("pr"):
            problems.append(f"{where}.pr is required (the one line an upstream PR would say)")
    return problems


def _validate_publish_entries(target: Target, publish: Iterable) -> list[str]:
    problems: list[str] = []
    names: list[str] = []
    versions: dict[str, str] = {}
    roles: dict[str, str] = {}

    for index, entry in enumerate(publish):
        where = f"npm_publish[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a mapping")
            continue
        role = entry.get("role")
        name = entry.get("name")
        version = entry.get("version")
        if role not in ROLES:
            problems.append(f"{where}.role must be one of {ROLES}")
        if not name:
            problems.append(f"{where}.name is required")
        elif name in names:
            problems.append(f"{where}.name '{name}' is duplicated")
        else:
            names.append(name)
            versions[name] = str(version or "")
            roles[name] = str(role or "")
        if not version:
            problems.append(f"{where}.version is required")
        elif str(version) != target.ver:
            problems.append(
                f"{where}.version '{version}' does not match the recipe version "
                f"directory '{target.ver}' (one directory describes one release)"
            )
        if not entry.get("dir"):
            problems.append(f"{where}.dir is required (path inside the source tree)")
        if role in PLATFORM_ROLES and not (entry.get("os") and entry.get("cpu")):
            problems.append(
                f"{where}: role={role} requires os and cpu "
                "(npm installs a platform package by those fields)"
            )
        kind = publish_kind(entry)
        base = entry.get("base")
        overlay = entry.get("overlay")
        if kind not in PUBLISH_KINDS:
            problems.append(f"{where}.kind '{kind}' is not supported; use one of {PUBLISH_KINDS}")
        elif kind == "upstream-tarball":
            problems.extend(_validate_base(where, str(name or ""), str(version or ""), base))
            if not isinstance(overlay, list) or not overlay:
                problems.append(
                    f"{where}: kind=upstream-tarball needs a non-empty overlay "
                    "(the riscv64 files added on top of the upstream package)"
                )
            else:
                problems.extend(_validate_overlay(where, overlay))
        elif base is not None or overlay is not None:
            problems.append(
                f"{where}: base/overlay only apply to kind=upstream-tarball"
            )

        shape = entry.get("manifest_shape")
        if shape is not None and str(shape) not in MANIFEST_SHAPES:
            problems.append(
                f"{where}.manifest_shape '{shape}' is not supported; use one of {MANIFEST_SHAPES}"
            )
        elif str(shape or "") == "template":
            template = entry.get("manifest_template")
            if not template:
                problems.append(f"{where}: manifest_shape=template needs manifest_template")
            elif not (target.path / str(template)).is_file():
                problems.append(f"{where}.manifest_template not found: {target.path / str(template)}")
        if shape is not None and entry.get("generate_manifest") is not None:
            problems.append(
                f"{where}: set either manifest_shape or generate_manifest, not both "
                "(manifest_shape is the newer spelling)"
            )
        allow_overwrite = entry.get("allow_overwrite")
        if allow_overwrite is not None and not isinstance(allow_overwrite, bool):
            problems.append(
                f"{where}.allow_overwrite must be a boolean (true lets the overlay replace "
                "upstream files, which has to be a deliberate decision)"
            )
        if allow_overwrite is not None and publish_kind(entry) != "upstream-tarball":
            problems.append(f"{where}.allow_overwrite only applies to kind=upstream-tarball")
        readme = entry.get("readme")
        if readme is not None and not (target.path / str(readme)).is_file():
            problems.append(f"{where}.readme not found: {target.path / str(readme)}")
        if entry.get("libc") is not None and not isinstance(entry.get("libc"), str):
            problems.append(f"{where}.libc must be a single value (npm takes one libc per entry)")

        auth = entry.get("auth")
        if auth is not None and str(auth).strip().lower() not in PUBLISH_AUTH_KINDS:
            problems.append(
                f"{where}.auth '{auth}' is not supported; use one of "
                f"{PUBLISH_AUTH_KINDS} (omit the field for "
                f"'{DEFAULT_PUBLISH_AUTH}')"
            )

    for index, entry in enumerate(publish):
        if not isinstance(entry, dict):
            continue
        deps = entry.get("optional_dependencies")
        if deps is None:
            continue
        if not isinstance(deps, list) or not deps:
            problems.append(f"npm_publish[{index}].optional_dependencies must be a non-empty list")
            continue
        if entry.get("role") != "main":
            problems.append(
                f"npm_publish[{index}].optional_dependencies belongs on the main package "
                "(the package that references the platform ones)"
            )
        for dep in deps:
            dep_name = str(dep)
            if dep_name not in versions:
                problems.append(
                    f"npm_publish[{index}].optional_dependencies references unknown package "
                    f"'{dep_name}'"
                )
            elif roles.get(dep_name) not in PLATFORM_ROLES:
                problems.append(
                    f"npm_publish[{index}].optional_dependencies '{dep_name}' is "
                    f"role={roles.get(dep_name)}; it must be a platform package"
                )
            elif versions[dep_name] != str(entry.get("version") or ""):
                problems.append(
                    f"npm_publish[{index}] version {entry.get('version')} does not match "
                    f"optional dependency {dep_name}@{versions[dep_name]}"
                )

    for index, entry in enumerate(publish):
        if not isinstance(entry, dict):
            continue
        for dep in entry.get("requires") or []:
            if dep not in versions:
                problems.append(
                    f"npm_publish[{index}].requires references unknown package '{dep}'"
                )
            else:
                dep_version = versions[dep]
                entry_version = str(entry.get("version") or "")
                if dep_version and entry_version and dep_version != entry_version:
                    problems.append(
                        f"npm_publish[{index}] version {entry_version} does not "
                        f"match required package {dep}@{dep_version}"
                    )

    if not any(isinstance(e, dict) and e.get("role") == "main" for e in publish):
        problems.append("npm_publish needs at least one role=main entry")
    return problems


def repository_problems(root: Path) -> list[str]:
    """Structural problems of the whole recipe tree.

    Every version directory is a buildable recipe (D1-C), so an empty package
    directory or a version directory without .stat is a half-finished recipe and
    must fail loudly instead of being skipped silently.
    """
    problems: list[str] = []
    for pkg in package_names(root):
        pkg_dir = root.joinpath(*pkg.split("/"))
        versions = [
            entry
            for entry in sorted(pkg_dir.iterdir())
            if entry.is_dir() and entry.name != COMMON_DIR
        ]
        for entry in sorted(pkg_dir.iterdir()):
            if entry.is_file():
                problems.append(
                    f"{pkg}: unexpected file '{entry.name}' in the package directory "
                    "(versions hold the data, the package directory only groups them)"
                )
        if not versions:
            problems.append(f"{pkg}: package directory has no version subdirectory")
        for version_dir in versions:
            if not (version_dir / STAT_FILE).is_file():
                problems.append(
                    f"{pkg}/{version_dir.name}: version directory is missing {STAT_FILE}"
                )
    return problems


def main_publish_name(stat: dict, fallback: str) -> str:
    for entry in stat.get("npm_publish") or []:
        if isinstance(entry, dict) and entry.get("role") == "main" and entry.get("name"):
            return str(entry["name"])
    return fallback


# --------------------------------------------------------------------------
# layering
# --------------------------------------------------------------------------


@dataclass
class Layers:
    """Common + version overlay resolution for one recipe."""

    commands: dict[str, Path] = field(default_factory=dict)
    patches: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def read_disable_list(directory: Path) -> set[str]:
    disable = directory / DISABLE_FILE
    if not disable.is_file():
        return set()
    names = set()
    for raw in disable.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            names.add(line)
    return names


def resolve_layers(root: Path, target: Target) -> Layers:
    """Merge ``common`` (baseline) with the version directory (override).

    Same-named snippet or patch in the version directory replaces the common
    one; other files are inherited/added. ``patches/.disable`` removes entries
    from the effective patch list.
    """
    layers = Layers()
    common = target.path.parent / COMMON_DIR

    disabled: set[str] = set()
    for directory in (common, target.path):
        disabled |= read_disable_list(directory / "patches")

    patch_files: list[Path] = []
    seen: dict[str, Path] = {}
    for directory in (common, target.path):
        patches_dir = directory / "patches"
        if not patches_dir.is_dir():
            continue
        for entry in sorted(patches_dir.iterdir()):
            if not entry.is_file() or entry.name.startswith("."):
                continue
            if entry.suffix != ".patch":
                layers.problems.append(f"ignoring non-.patch file: {entry}")
                continue
            seen[entry.name] = entry
    for name in sorted(seen):
        if name not in disabled:
            patch_files.append(seen[name])
    layers.patches = patch_files

    for directory in (common, target.path):
        commands_dir = directory / "commands"
        if not commands_dir.is_dir():
            continue
        for entry in sorted(commands_dir.iterdir()):
            if not entry.is_file() or entry.name.startswith("."):
                continue
            if entry.name not in SNIPPET_NAMES:
                layers.problems.append(
                    f"unknown command snippet '{entry.name}' in {commands_dir} "
                    f"(expected one of {', '.join(SNIPPET_NAMES)})"
                )
                continue
            layers.commands[entry.name] = entry
    return layers


def read_snippet(path: Path) -> str:
    return path.read_text(encoding="utf-8").rstrip("\n")


def relative_recipe_dir(root: Path, target: Target) -> str:
    """POSIX path of the recipe directory relative to ``root``'s parent."""
    return os.path.relpath(target.path, start=root.parent).replace(os.sep, "/")
