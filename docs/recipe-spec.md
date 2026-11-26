# Recipe specification

A **recipe** describes how to build one version of one npm package for riscv64:
where the source comes from, which patches to apply, how to build and test it, and
which npm packages the result becomes. Everything else - the composite action, the
container invocation, the workflow matrix - is generated from the recipe.

## 1. Layout

```
packages/
  <pkg>/                      # unscoped:  packages/bufferutil
    <version>/                # scoped:    packages/@scope/name/<version>
      .stat                   # required, see §2
      commands/               # optional
        fetch  patch  build  test  publish
      patches/                # optional
        0001-*.patch
        .disable              # optional, see §4
      upstream/               # optional: the same increment, written for upstream
        riscv64-*.patch       # see §8 - never applied here
      templates/              # optional: originals for files the publish step writes
        package.json  README.md
      tests/                  # optional: what the test step runs, if it needs files
        riscv64-skip.txt  vitest.config.mjs  package.json
    common/                   # optional baseline layer, never built on its own
      commands/  patches/
```

Rules:

* A directory under `packages/` is a **package**, named after the package as it is
  *published upstream*: `packages/bufferutil/4.2.0` for the unscoped `bufferutil`,
  `packages/@scope/name/1.2.3` for a scoped one (two path segments). The path is the
  identity of the recipe, so it never carries a registry or a re-brand - that is what
  `npm_publish[].name` is for.
* Every directory inside a package - except `common` - is a **version** and must
  contain `.stat`. The set of version directories *is* the set of buildable
  recipes; a version directory without `.stat` is an error, not a draft.
* `common` is a shared baseline layer. It has no `.stat`, is never built, and is
  merged into every version of that package.

## 2. `.stat`

YAML. All keys are optional unless a rule below says otherwise.

```yaml
# Where the source comes from. Routes are tried in this priority order:
# upstream > source > npm. Each one may be omitted; at least one is required.
upstream:
  repo: https://github.com/websockets/bufferutil
  ref: v4.2.0            # must be immutable: tag name or commit sha
  subdir: ""             # optional, source root inside the checkout
source:
  url: https://example.com/pkg-1.0.0.tar.gz
  sha256: "…"            # required whenever `source` is present
npm:
  name: some-package     # tarball route; the registry metadata API is used to
  version: 1.0.0         # resolve dist.tarball and dist.integrity
  registry: https://registry.npmjs.org

# How the recipe is built. Omitted = native.
#   native - fetch/patch/build/test run inside a riscv64 container (qemu)
#   cross  - fetch/patch/build run in an amd64 container holding the riscv64 cross
#            toolchain (rustc + riscv64-linux-gnu gcc); only the test step runs
#            inside the riscv64 container, so the artifact is still executed on
#            the target CPU
toolchain: native        # native | cross

artifacts:
  path: prebuilds/linux-riscv64/*.node    # glob, relative to the source root

npm_publish:
  - role: main           # main | binary | binding
    # The name to publish under, i.e. what a consumer types. Keeping the upstream
    # name (and scope) makes `npm i <pkg>` transparent; a fork that must re-brand
    # says so here, or passes --publish-scope at render time.
    name: example
    version: 1.0.0
    dir: .               # directory to publish, relative to the source root
    registry: https://npm.example.com/repository/npm-local   # the registry this recipe publishes to
    token_env: NODE_AUTH_TOKEN   # env var holding the credential (see §6)
    auth: basic          # token | basic
    # Which package the published tarball starts from (see §6):
    #   omitted / source-tree    - the built tree under `dir`
    #   upstream-tarball         - upstream's published tarball plus our overlay
    base:
      kind: upstream-tarball
      url: https://registry.npmjs.org/example/-/example-1.0.0.tgz
      integrity: sha512-…      # required: the tarball is verified before it is used
    overlay:                   # copied in, same relative paths as in the tarball
      - prebuilds/linux-riscv64
    allow_overwrite: false      # overlay may replace upstream files (deliberate)
    # The manifest of the published package (see §6):
    #   upstream  - take upstream's manifest, touch only what the recipe says
    #   generated - create/update it from these fields
    #   template  - render templates/package.json (placeholders substituted)
    manifest_shape: upstream
    manifest_template: templates/package.json
    readme: templates/README.md
    optional_dependencies:      # main only: platform packages it references
      - "@scope/binding-linux-riscv64-gnu"
    # platform fields, written for generated/template shapes and for role=binding
    os: linux
    cpu: riscv64
    libc: glibc
    main: example.node
    engines:
      node: ^20.19.0 || >=22.12.0

# Where the published package is verified on real riscv64 silicon (§12). Optional: a
# recipe without this section is verified only inside the emulated container.
physical:
  registry: https://forgestash.artifact.yuantu.ac.cn/repository/npm-riscv-group
  # none (default, an open group) | token | basic - independent of npm_publish auth:
  # publishing uses a credential, reading a group repository need not.
  auth: none
  # the environment variable holding the credential, required for token/basic
  token_env: NPM_READ_TOKEN
  # registry (default) or tarball (install the build's fallback tarball instead)
  source: registry

# The increment offered back to upstream. Recorded, checked, never applied.
upstream_increments:
  - patch: upstream/riscv64-prebuild.patch
    touches: [.github/workflows/ci.yml]       # must equal the files in the diff
    pr: build a linux-riscv64 prebuild and ship it in the release tarballs
```

Validation (the renderer refuses to build otherwise):

| Rule | Why |
| --- | --- |
| `upstream.ref` must not be a moving ref (`main`, `master`, `develop`, `HEAD`, `latest`, `v1`, `v1.2`, or a branch-looking name) | a rebuild must produce the same bytes |
| `source.sha256` is mandatory when `source` is present | a mirror without a checksum is not reproducible |
| `npm_publish[].version` must equal the recipe's version directory | one version directory describes exactly one release |
| every `npm_publish` entry needs `name` and `dir` | publishing an unnamed or unrooted package is meaningless |
| `role: binary` / `role: binding` entries need `os` and `cpu` | that is what makes npm pick the right optional dependency |
| `base` needs `url` ending in `<name>-<version>.tgz` and a `sha512` `integrity` | that tarball must be the release being republished, and it is verified before use |
| `base`/`overlay` require `kind: upstream-tarball` | a source tree has nothing to overlay |
| `overlay` paths must be relative and whitespace-free | they are copied, and a path with spaces is a shell trap |
| `allow_overwrite` only applies to `upstream-tarball` | an overlay that replaces upstream files must be an explicit decision |
| `manifest_shape: template` needs `manifest_template`, and it must name and version the package as the recipe does | a template that disagrees with `.stat` publishes something nobody declared |
| `manifest_shape` and `generate_manifest` are exclusive | two spellings of one decision |
| `optional_dependencies` belongs on `role: main` and must name platform packages of this recipe at this version | npm resolves those by name, so a typo downloads the wrong package or nothing |
| `upstream_increments[].patch` must live under `upstream/` and declare `touches` and `pr` | see §11 - those files are pull requests, not build inputs |
| a `requires` entry must name another `npm_publish` entry of the same recipe, at the same version | a renamed dependency must not be silently accepted |
| `toolchain` must be `native` or `cross` | the action only knows how to run those two build models |
| `physical.registry` is required and must be an http(s) URL | a hardware run installs from a registry a consumer can read, which need not be the one the recipe publishes to |
| `physical.auth` must be `none`, `token` or `basic`, and `token`/`basic` need `physical.token_env` | the credential is read from an environment variable, never written into a recipe |
| `physical.source` must be `registry` or `tarball` | two ways to get the bytes, nothing else |
| `commands/verify` is required exactly when `physical:` is present | a hardware step with nothing to assert would report a green light it did not earn |
| package directories must not be empty | an empty directory is a half-finished recipe, not a recipe |

## 3. `commands/` snippets

Six fixed names: `fetch`, `patch`, `build`, `test`, `publish`, `verify`.

* `build` and `test` are **required** (from the version layer or `common`).
* `fetch`, `patch`, `publish` are **generated** when absent (see §5, §6).
* `install` and `verify` exist only for the hardware template (§12): `install` is
  generated (a recipe may override it), `verify` comes from the recipe and is
  required exactly when `.stat` has a `physical:` section.
* Any other file name in `commands/` is an error - a typo like `biuld` must not
  silently do nothing.
* A snippet is plain shell, run with `bash` (`set -euo pipefail` is yours to add).
  It runs either inside the riscv64 container (fetch/patch/build/test), on the
  runner (publish), or on the hardware runner (install/verify), with the working
  directory as described in §7.

## 4. Merging `common` and `patches`

For every file name, the version layer wins:

* **same name in both** -> the version's file replaces the `common` one entirely
  (file-level replace, not a line-level merge);
* **only in `common`** -> inherited;
* **patches** -> the `common` patches are collected first, then the version
  patches, each list sorted by file name;
* **`patches/.disable`** -> one patch file name per line (`#` comments allowed);
  listed patches are dropped. This is how a version turns off an inherited patch
  without copying the whole directory;
* **dotfiles** (`.disable`, `.keep`, …) are never treated as patches.

## 5. Generated `fetch`

```bash
if fetch_upstream; then          # upstream > source > npm
  :
elif fetch_source; then
  :
elif fetch_npm; then
  :
else
  exit 1
fi
```

* `upstream` clones the repo at the pinned ref into `src/`, shallow first and with
  a full-clone + checkout fallback (a shallow clone cannot resolve a bare sha).
  `.git` is kept so `git apply --3way` stays available to the patch step.
* `source` downloads the URL, verifies `sha256` when set, and extracts with
  `--strip-components=1`.
* `npm` fetches the registry metadata for the package, reads
  `versions[<v>].dist.tarball` and `.integrity`, verifies the sha512 checksum when
  present, and extracts with `--strip-components=1`. Scoped names are
  percent-encoded (`@scope%2Fname`) - URLs are never assembled by hand.
* A route that is not configured is a stub that returns 1, so the chain has the
  same shape whether one or three routes exist.

## 6. Generated `publish`

A recipe publishes one or more npm packages, in the order `.stat` lists them, with
platform roles (`binary`, `binding`) first and then `main` following `requires` - a
platform package is always published before the package that references it. The
mapping of an npm release onto those packages is upstream's, not ours:

| upstream's release | packages a recipe publishes |
| --- | --- |
| one package that carries prebuilds for several platforms | the same package, with `prebuilds/linux-riscv64` added |
| one JS package plus one platform package per target (napi-rs) | the same two, the platform one for riscv64 |

So a published tarball is always **upstream's release plus the riscv64 increment**,
never a rebuilt tree: an x64 consumer that installs from a mirror carrying these
recipes gets exactly what npmjs.org would have served.

### 6.1 Where the tarball starts (`base.kind`)

* `source-tree` (default) - `dir/` in the build tree. Used when the build produced
  the package that gets published (a generated manifest, a platform package, or a
  wrapper that the fetch step staged from the upstream tarball).
* `upstream-tarball` - upstream's published tarball, downloaded **inside the publish
  step**, verified against `base.integrity`, unpacked, and then the `overlay` paths
  are copied into it. `allow_overwrite: false` (the default) makes the copy refuse to
  replace an existing file, so an overlay can only add. This is how a package whose
  release artifacts are built in upstream's CI (bufferutil, utf-8-validate) is
  republished without rebuilding the other platforms: the recipe never had them in
  the first place, and a source tree cannot reproduce them.

Downloading the base inside the publish step, rather than in `fetch`, is deliberate:
the publish side never influences the build key (§9), so a recipe can switch its
publish shape - or fix its integrity hash - without invalidating every cached build.

### 6.2 The manifest (`manifest_shape`)

| shape | behaviour |
| --- | --- |
| `upstream` | the manifest that came with the package is kept field for field; only `optionalDependencies` (merged, see below) and `publishConfig.registry` are written |
| `generated` | created when missing, updated when present: `name`/`version` come from `.stat`, and `os`/`cpu`/`libc`/`main`/`engines` are written when declared |
| `template` | `templates/package.json` with `{{name}} {{version}} {{registry}} {{os}} {{cpu}} {{libc}} {{main}}` substituted; a leftover placeholder is an error |

`manifest_shape: upstream` is what makes a republished package indistinguishable from
upstream's, and the recipes assert it after publishing (`tests/verify_published.py`,
§10). `template` is for platform packages, whose manifests upstream generates with
napi at release time: the template mirrors upstream's own platform manifest field for
field (`files`, `engines`, `repository.directory`, `bugs`, `publishConfig.access`,
`os`, `cpu`, `libc`), so the riscv64 package differs from the x64 one only where the
architecture differs.

`optionalDependencies` are merged additively and **idempotently**: every platform
upstream lists survives, nothing but the packages this recipe publishes is added, and
running the publish step twice produces the same manifest. "The count must grow" was
wrong - a recipe that publishes out of its own build tree writes the entry into that
manifest, so the second run would have refused a no-op.

`publishConfig.registry` in the manifest is realigned to the recipe's registry: npm
gives `publishConfig` precedence over `--registry`, so an upstream manifest pinning
npmjs.org would send the tarball somewhere the recipe never asked for. With
`upstream` the realignment is the only edit a republished package carries, and it is
recorded in the log.

### 6.3 Visibility, replacement and what green means

1. visibility: the publish runs with `--loglevel http` and its output is streamed
   (`| tee`) instead of captured, because a captured npm looks frozen in CI logs
   until it exits - a slow upload then reads as "publish hangs" with no evidence.
   The streamed text is still classified in (2).
2. `npm publish` runs with `--force`. npm >= 11 refuses client-side, before sending
   anything, to publish a version the registry already knows ("You cannot publish
   over the previously published versions"); a mirror carrying upstream's release
   always answers that way, and refusing to replace is the wrong rule here, because
   the tarball being published *is* that release plus the riscv64 increment. With
   `--force` the registry decides, which is the only party that can.
3. **the step is green only if the registry serves these exact bytes.** After the
   upload (accepted or refused) the step recomputes the digest of what npm would
   upload (`npm pack --dry-run --json`) and asks the registry what it serves
   (`npm view <name>@<version> --json`, which uses `preferOnline` so it cannot answer
   from a pre-publish cache). A match, plus a version that references the recipe's
   platform packages, is "already published: identical artifact, nothing to do" -
   the truth of a re-run. A different digest is a hard failure naming both.
   This is the check that was missing: a refusal used to be logged as
   "already published" and the step stayed green while the mirror went on serving
   upstream's `rolldown@1.3.3` without `@rolldown/binding-linux-riscv64-gnu` -
   a green build that delivered no riscv64 support (witnessed in the fork's L3 run).
   A registry lookup is still never used to *decide whether to publish*: a registry
   that proxies or groups another one serves upstream versions of the same name, so
   such a lookup reports our own package as published and the publish silently never
   happens (witnessed with verdaccio and `bufferutil@4.2.0`).

   What "serves these bytes" is established *from* matters, because registries
   answer in three different ways, and the check has to read all three:

   | what the served document carries | how the check answers |
   | --- | --- |
   | `dist.integrity` (npmjs, verdaccio, GitHub Packages) | compare the sha512 |
   | only `dist.shasum` (older hosted repositories) | compare the sha1 |
   | only `dist.tarball` (the private registry this repository rehearses against) | download it and compare the bytes |

   Two implementation notes, both paid for by a red run:

   * `npm view --json` prints an **object** up to npm 11 and a one-element **list**
     from npm 12. Reading the list as a manifest leaves every `dist` lookup empty,
     which once reported a delivered package as "the registry serves something else"
     - absence of a field must never be read as a mismatch.
   * the tarball download authenticates the way npm does: the credential is looked
     up in the generated `.npmrc` by longest matching nerf-dart (scheme dropped, last
     path segment dropped), because a registry that publishes no digest still
     requires auth to read the tarball.

   An unverifiable answer is its own state, not a verdict: after an accepted upload
   it is a **warning** (the package is there; the registry simply publishes no digest
   and/or the tarball cannot be read), while after a refusal it is a **failure**
   (nothing at all shows our bytes are what a consumer would get), which is the case
   the check exists for. `PUBLISH_VERIFY=digest` skips the download for a route too
   slow to read packages back (the answer then says so instead of pretending),
   and `PUBLISH_VERIFY_MAX_BYTES` (default 64 MB) caps what it will download.
4. credentials: `token_env` names an environment variable; when it is set, a line
   keyed by the **full registry URL** (path included, npm looks credentials up by the
   URL it talks to) is written to a throwaway `.npmrc`. The registry URL is
   normalized to end with `/` first: npm computes the credential key with
   `new URL(".", registryUrl)`, so a URL without the slash loses its last path
   segment (`.../repository/npm-local` → key `//host/repository/`) and the publish
   pre-flight refuses with `ENEEDAUTH` without sending a request:
   `//<host>/<path>/:_authToken=${TOKEN_ENV}` for `auth: token` (the default), or
   `//<host>/<path>/:_auth=${TOKEN_ENV}` for `auth: basic`, which is what a registry
   answering `WWW-Authenticate: Basic` expects (the value is base64 `user:password`).
5. `npm publish <absolute dir> --registry <registry> --access public --force --ignore-scripts`.

### 6.4 How long the upload may take

npm's `fetch-timeout` is not a per-read idle timeout: `minipass-fetch` starts one timer
when the socket is assigned, covering **the upload and the answer together**, and
npm 9, 11 and 12 behave identically here (verified by publishing the same 9.3 MB
binding to the same throttled registry with each: abort at the timeout, success above
it, same error text). The default is 300 s, so for a package carrying a native binding
the default is a *cap on the route's speed*: 9.3 MB becomes 12.2 MB of base64 JSON, and
anything slower than ~40 kB/s cannot fit inside 300 s.

What that looks like when it bites (witnessed in the fork's L3 run, npm 12, binding):

```
npm http fetch PUT ... attempt 1 failed with FETCH_ERROR     (aborted mid-body)
npm http fetch PUT ... attempt 2 failed with FETCH_ERROR
npm http fetch PUT ... attempt 3 failed with FETCH_ERROR
npm error network timeout at: https://<registry>/@rolldown%2fbinding-linux-riscv64-gnu
```

Three attempts, ~12 MB re-uploaded each time, and the registry logs nothing - a
buffering proxy in front of it never passes on a request that was never finished, which
is why the private registry's own log looked empty.

So the step derives the budget from the packed size before publishing
(`manifest.mjs --plan`, below) and passes it explicitly:

| packed size | fetch timeout | `--fetch-retries` |
| --- | --- | --- |
| < 4 MiB | 300 s + (size × 1.4 / 16 kB/s) | npm's default (2) |
| >= 4 MiB | same formula, capped at 1 h | 1 (a retry re-uploads megabytes) |

Both numbers are logged (`upload 9128369 bytes, fetch timeout 1099000ms`) so a slow
route is visible instead of invisible, and `PUBLISH_FETCH_TIMEOUT` (milliseconds)
overrides the formula for an operator who knows the route.

`scripts/publish_manifest.mjs` is the one helper the publish step calls, so the shell
never builds JSON by hand (one apostrophe in an embedded literal used to break the step
outright). Each mode prints shell assignments, JSON, or a verdict:

| mode | what it does |
| --- | --- |
| `--shell <spec>` | `P_*` assignments the step `eval`s: name, version, registry, kind, base, overlay, platform fields |
| `--verify-integrity <spec> <tarball>` | sha512 of the base tarball against `base.integrity` |
| `--prepare <spec> <dir>` | writes `<dir>/package.json` (and `README.md`) for the kind and manifest shape, merging rather than replacing |
| `--plan <spec> <dir>` | `P_PACKED_BYTES`, `P_FETCH_TIMEOUT`, `P_FETCH_RETRIES`: what the upload will cost |
| `--assert-served <spec> <dir> [--accepted]` | the verdict about what the registry serves (3 above) |

`name` is identity: publishing a package whose manifest is named differently is refused
rather than published under a second name - a rename ships "another package", not an
update, and the original name's consumers get no riscv64 support.

Large packages need a registry that accepts them: verdaccio's default
`max_body_size` is 10 MB, so an L2 rehearsal of a package carrying a native binding
needs `max_body_size: 100mb` in its config.

## 7. What the renderer generates

```bash
python3 scripts/render.py --pkg bufferutil --ver 4.2.0 --out-dir .rendered/action
python3 scripts/render.py --check-all          # every recipe, YAML validated
python3 scripts/render.py --pkg … --ver … --out .rendered/action/action.yml
```

`--out-dir` writes `action.yml` plus `steps/{fetch,patch,build,test,publish}.sh`.
Wrapping shell in YAML by hand is where quoting bugs live, so the scripts stay
separate files and the action only calls them.

Generated variables available in the template: `pkg.name`, `pkg.dirname`,
`pkg.full`, `pkg.version`, `pkg.ident`, `pkg.slug`, `pkg.artifact`,
`pkg.target`, `recipe.dir`, `src.dir`, `steps.dir`, `has.patches`, plus every
scalar from `.stat` flattened with dots (`upstream.repo`, `upstream.subdir`,
`npm_publish.0.registry`, …).

The action also exports `RECIPE_DIR` (`/w/<recipe.dir>` inside the build container)
to the steps. It is how recipe-local data reaches a step - `tests/riscv64-skip.txt`,
`tests/vitest.config.mjs`, `templates/package.json` - so a test step never has to
hard-code a path that only exists in this repository. Only the `test` step receives it
today; the publish step reads what it needs on the runner.

The toolchain adds its own variables, so a recipe never has to spell out where
the steps run:

| Variable | native | cross |
| --- | --- | --- |
| `job.toolchain`, `job.cross` | `native`, false | `cross`, true |
| `build.platform`, `build.image`, `build.dockerfile` | `linux/riscv64`, `npm-riscv64/toolchain:riscv64`, `docker/riscv64-toolchain.Dockerfile` | `linux/amd64`, `npm-riscv64/toolchain:cross`, `docker/riscv64-cross.Dockerfile` |
| `test.platform`, `test.image` | `linux/riscv64`, the riscv64 image | `linux/riscv64`, the riscv64 image |
| `riscv.triple` | `riscv64gc-unknown-linux-gnu` (passed to the build step only for cross) | same |

Two images can therefore be involved in one job: `RISCV_IMAGE` is the build image
and `RISCV_TEST_IMAGE` the test image (unset ⇒ the same image). The cross image is
pulled only by the cross recipes that need it.

Every published image is single-platform, so pulls name the platform
(`docker pull --platform linux/riscv64`); without it the daemon asks for the host
platform and fails with `no matching manifest for linux/amd64`. The action does that
in its prepare step and `build.yml` before it, so a recipe never has to care.

Template features:

* `{{key}}` substitution, indentation aware: a multi-line value gets the current
  indentation prefixed to every continuation line, so block scalars stay valid;
* `{{#if key}} … {{/if}}` and `{{#unless key}} … {{/unless}}` blocks (nestable) so
  optional steps disappear instead of becoming empty `run:` blocks;
* an unresolved placeholder is an error, not an empty string.

`--steps-dir` overrides the workspace-relative directory written into the action,
so the same render can be written anywhere (that is how the golden fixtures stay
stable).

`--override key=value` (repeatable, dotted paths, list indices as numbers) patches
`.stat` at render time. It exists for local runs, e.g.
`--override npm_publish.0.registry=http://127.0.0.1:4873`; promoting a recipe to a
different registry never requires editing it.

Two dedicated flags cover the common fork cases:

* `--publish-scope cannedfish` rewrites the npm scope of every publish target (and
  of the `requires` references between them) to `@cannedfish/...`. GitHub Packages
  only accepts packages whose scope equals the repository owner.
* `--publish-registry URL` points every publish target at one registry.

The rendered action is validated before it is written: YAML must parse,
`runs.using` must be `composite`, every step must have `shell` and a non-empty
`run`, and the fetch/build/test/publish steps must all be present.

### Container user

Every step that touches the workspace runs its container as **the calling user**:

```bash
docker run --rm --platform linux/riscv64 \
  --user "${RISCV_DOCKER_USER:-$(id -u):$(id -g)}" \
  -e HOME=/w/.container-home -e npm_config_cache=/w/.container-home/.npm \
  ... -v "$GITHUB_WORKSPACE:/w" ...
```

Running as root leaves root-owned files in the workspace, and the publish step - which
runs on the host, outside emulation - then has to write a manifest it does not own. On
a GitHub runner the step user is `runner`, so the mismatch surfaced as
`EACCES: permission denied, open 'src/package.json'`.

HOME and the npm cache point inside the workspace because the container user has no
writable home of its own. `RISCV_DOCKER_USER` overrides the mapping.

### Quoting

`@` is a YAML reserved indicator character. Any template scalar that *starts*
with a variable must be quoted:

```yaml
name: "build {{pkg.ident}} (riscv64)"     # correct
name: build {{pkg.ident}}                 # parses as a tag, fails
```

The renderer catches this by parsing its own output.

## 8. Change detection

`scripts/detect_targets.py` decides what to build, from environment variables so
the same script serves both triggers:

| change | selection |
| --- | --- |
| `packages/<pkg>/<ver>/**` | that version, always built |
| `packages/<pkg>/common/**` | every version of that package |
| a **global build input** (`.github/templates/**`, `scripts/**`) | every recipe is a *candidate*, then narrowed by comparing build cache keys with the base commit |
| `docker/**` | every recipe (the image digest is only known after the image is rebuilt) |
| anything else | nothing |

Pull requests use a three-dot diff against the merge base, pushes a two-dot diff;
an all-zero `before` (new branch, force push) rebuilds everything. A selected
version directory without `.stat` fails the run before any build starts.

The narrowing matters because a global input can change the *action* without changing
a single executed command - adding an input, an `if:` guard or a comment. The artifact
cannot differ then, so the recipe is not rebuilt. Narrowing reuses the cache key
(`narrow_by_build_inputs`), so "changed" means exactly what the cache means by it. It
compares the base commit's own `scripts/build_key.py` when that revision shipped one
(a renderer change then shows up as changed commands); if it did not, the current tool
renders the base tree instead. Whenever the comparison is impossible - no base commit,
unreadable recipe, unusable checkout - every candidate is kept.

`scripts/**` also keeps the renderer self-check (`--check-all`) running.

## 9. Build cache

Rebuilding the same recipe with the same inputs produces the same bytes, so a
finished workspace tree is cached as an artifact and restored instead of rebuilt.

Key (`scripts/build_key.py`) - the *rendered* commands that produce the artifact:

* the rendered `run:` blocks of the fetch, patch and build steps (image, mounts,
  working directory, environment, flags),
* the rendered `steps/{fetch,patch,build}.sh` bodies (fetch and patch are generated
  from the template and the `.stat`; build is the recipe snippet),
* the patch files, which the patch step reads from the repository at run time,
* the resolved container image id,
* anything passed with `--extra`.

Hashing the rendered commands rather than the source files makes the key precise in
both directions: a renderer or template change that alters the generated commands
changes the key (no separate input needed), while an edit that only touches an input,
an `if:` guard, a comment, the `test` snippet, the `publish` snippet or a publish-only
`.stat` field leaves it alone - the artifact cannot differ.

Nothing else can influence the output, so a hit cannot be stale: no diff against the
source branch is needed, and the same key is produced by a pull request and by the
merge commit that follows it.

Cache unit is the **tree**, not just the artifact: publish needs the source tree and
the test needs `node_modules` to resolve the prebuild like a consumer would. `.git` is
excluded. Artifact name: `<slug>-<ver>-riscv64-tree-<key>` (e.g.
`ruyisdk-bufferutil-4.2.0-riscv64-tree-77f1d80d3571`), retention 90 days.

On a hit the action receives `reuse: true`, which skips fetch, patch and build. The
**test step still runs** - a restored artifact is validated like a fresh one - and
publish runs normally. Any failed lookup (no artifact, expired, API error) falls back
to a full build.

### 9.1 The publish key, and targets that only deliver

The build key deliberately ignores the publish side, which leaves a hole: fixing the
publish step changed nothing, so nothing ran, and the fix stayed undelivered (witnessed:
a fork run reported "build inputs unchanged" for every recipe while the package the
registry served was still the wrong one). A second key answers the other question -
**does this tree have to be delivered again?**

`scripts/build_key.py --publish-key` hashes:

* the rendered `run:` block of the `publish` step,
* the rendered `steps/publish.sh` body (which embeds the resolved publish specs, so the
  registry, the platform packages and the manifest template are all covered),
* `scripts/publish_manifest.mjs`, copied into the steps verbatim.

The detector compares both keys against the base commit and labels each target:

| build key | publish key | mode | what the run does |
| --- | --- | --- | --- |
| changed | either | `build` | fetch, patch, build, test, publish |
| unchanged | changed | `publish` | restore the cached tree, publish only |
| unchanged | unchanged | - | skip |

`publish` targets exist only where publishing is allowed (a push to `main`, or an
explicit `workflow_dispatch` with `republish: true`); a pull request that only touches
the publish side has nothing to deliver and builds nothing. `workflow_dispatch` with
`republish: true` makes *every* recipe a `publish` target - the lever for rolling out a
delivery fix without touching a recipe.

The action takes `publish_only: true` for those targets: fetch, patch, build and test
are skipped, and a guard step fails before publishing when `reuse` is not `true`, because
delivering a tree that was never restored would ship an empty or stale package. The
local runner reproduces both paths:

```bash
python3 tests/run_action_locally.py --pkg bufferutil --ver 4.2.0 \
  --publish --publish-only --reuse          # skips fetch/build/test, publishes
python3 tests/run_action_locally.py --pkg bufferutil --ver 4.2.0 \
  --publish --publish-only                  # fails in the guard, as it should
```

## 10. Verifying locally

| level | command | what it proves |
| --- | --- | --- |
| L0 | `python3 -m unittest discover -s tests` | renderer, layering, detection, generated YAML validity, golden snapshot (`tests/golden/`, refresh with `python3 tests/regen_golden.py`) |
| L1 | `python3 tests/run_action_locally.py --pkg bufferutil --ver 4.2.0` | the rendered action really fetches, builds and tests under riscv64 emulation |
| L0 | `python3 -m unittest tests.test_publish_plan` | the upload budget: size-derived timeout, retry reduction, cap and override |
| L2 | `python3 tests/l2_publish.py` | publish order, idempotency, manifest merge, identity, partial-failure re-entry against verdaccio (`DSH_L2_PORT=4874` when a leftover container holds the default port); the first publish is a real 201, so this also rehearses the served-bytes assertion |
| L2 (delivered) | `python3 tests/verify_published.py <pkg> <ver> [--shape …]` | the bytes a registry serves compared with upstream's: file by file, manifest field by field |
| L2 (hardware) | `python3 tests/rehearse_physical.py --pkg <pkg> --ver <ver> --registry <url> [--token-env-file <file>]` | the hardware path end to end: publish, install from a registry, digest check, execute - everything except the silicon (§12.6) |
| patches | `python3 scripts/check_patches.py [--network]` | `upstream/*.patch` are real diffs, declared, free of this repository's traces, and still apply |

L1 and L2 need docker. L1 uses the toolchain images: `--image` (or `RISCV_IMAGE`)
is the build image and `--test-image` (or `RISCV_TEST_IMAGE`) the test image, so a
cross recipe is reproduced the way CI runs it - e.g.

```bash
RISCV_IMAGE=npm-riscv64/toolchain:cross \
RISCV_TEST_IMAGE=npm-riscv64/toolchain:riscv64 \
python3 tests/run_action_locally.py --pkg rolldown --ver 1.3.3
```

It takes a local image when one exists and otherwise pulls it. In CI the image is published once
by the `toolchain-image` workflow and pulled per run - building it per run costs
minutes of emulated package installs and can silently succeed without producing a
local image. Set `RISCV_IMAGE` to override the image and `NPM_REGISTRY` to point
publishing at a local registry. L3 (real GitHub Actions) runs in a fork; there the
registry and its credential are the ones the `.stat` names, and `--override` (or the
`PUBLISH_REGISTRY` / `PUBLISH_SCOPE` variables) moves a build elsewhere without
touching the recipes.

## 11. Upstream increments

A recipe that adds riscv64 support usually has two kinds of patch, and mixing them is
how a "submit it upstream" promise quietly stops being true:

| directory | what it is | applied by |
| --- | --- | --- |
| `patches/` | build-time patches: fixes this repository needs to make a build work | the generated `patch` step |
| `upstream/` | the same increment written as a pull request for upstream | nobody here - it is offered as a PR |

`upstream/<name>.patch` carries a short header, then a real unified diff generated
against a checkout of the pinned ref:

```
Upstream-PR: build a linux-riscv64 prebuild and ship it in the release tarballs
Upstream-Repo: https://github.com/websockets/bufferutil
Upstream-Base: v4.2.0 (d8aa572)
Upstream-Files: .github/workflows/ci.yml

Why: …
What: …
How: …
Note: …

diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
```

The patch is declared in `.stat: upstream_increments[]` with the files it touches and
the PR description, and `scripts/check_patches.py` (run by `--check-all`, and
`--check-patches` for the summary) enforces the boundary:

* a genuine diff: no `/dev/null` sides (no file deletions or creations), `a/` and `b/`
  prefixes intact, a real `diff --git` header per file;
* `touches` and the diff's file list are equal in both directions;
* nothing about this repository: no registry host, mirror, container file, `dsh`
  path or `render.py` mention - a patch that mentions those cannot be submitted;
* platform package names match the triple napi-rs derives (`riscv64gc-unknown-linux-gnu`
  → `-riscv64-gnu`), and the increment actually adds that target;
* `--check-patches-network` runs `git apply --check` against the pinned ref, so a
  patch that no longer applies is caught before a pull request is opened.

The generated `patch` step never mentions `upstream/`, and a test asserts that:
build-time and upstream-facing patches cannot drift into each other.

## 12. Hardware verification (`physical:`)

A package that builds and passes its tests inside a riscv64 container has still not
been installed by a real user on real silicon. The emulated container answers
"the recipe works"; this layer answers "the published package works where it is
going to be used": install what a consumer installs, from the registry a consumer
reads, and run the recipe's assertions on the machine it targets.

It is deliberately a **second, separate action**, not a sixth step of the build:

| | build action (`action.template.yml`) | hardware action (`physical.template.yml`) |
| --- | --- | --- |
| where | a riscv64 container (qemu) | a `riscv64` self-hosted runner, no container |
| what | fetch, patch, build, test, publish | install + verify, nothing else |
| when | on every push / pull request | after a successful build that published |
| question | does the recipe build? | does the *delivered* package work? |

Why it is not part of the build pipeline: the hardware runner is scarce, its results
must never gate a delivery, and the container already proves everything the build
can prove. Why it installs instead of reusing the build tree: a hardware run that
reads `src/` verifies bytes nobody can download.

### 12.1 What the renderer produces

`python3 scripts/render.py --pkg <name> --ver <version> --template .github/templates/physical.template.yml
--out-dir <dir>` writes one composite action with exactly one step, which runs
`steps/physical.sh`. That wrapper is generated (`scripts/physical_template.sh`) and
is where the environment work happens:

* the four scripts: `steps/install.sh` (generated, overridable by
  `commands/install`), `steps/verify.sh` (the recipe's `commands/verify`), plus the
  wrapper. There is no `fetch`, `patch`, `build`, `test` or `publish` script here,
  and a rendered action that grows one is a bug;
* `--check-all` checks both templates for every recipe that has one: the pipeline
  action always, the hardware action when `.stat` has `physical:`, and says
  `[SKIP]` for the rest instead of failing them. `--physical-registry <url>` rewrites
  `physical.registry`, so a fork verifies against its own group repository without
  editing a recipe.

### 12.2 The install step

Installing is the assertion here: npm's own resolution has to succeed, so a broken
`optionalDependencies` entry (how a platform package is picked) fails the run even
when the postinstall happens to pass. The step:

1. reads the tarball npm would serve for `<name>@<version>` from `physical.registry`
   (`npm pack --json`), and reads that answer in **both** shapes - an array (npm <=
   10) and an object keyed by package name (npm >= 11). Reading one shape leaves
   `filename` empty, which reads as "the registry has nothing", which is the D39
   mistake in a new place;
2. hashes the tarball it got (sha512 SRI, no network, no extra tooling) and compares
   it with the digest the build's publish report recorded. Different bytes are a
   failure naming both digests - not a retry, and not a fallback;
3. writes the digest it installed into the report, so the evidence is the report and
   not a log line;
4. installs into a temporary consumer project (`npm install <tgz> --no-audit
   --no-fund`), and refuses to continue when the installed manifest is not the
   version asked for.

The **fallback tarball** exists for exactly one situation: the publish report says
`served: unverified` - the registry accepted the upload and serves nothing
checkable - so the bytes cannot be confirmed by reading them back. Then the build
keeps the tarball it uploaded, and the hardware run installs that file, comparing it
against the same recorded digest. It is not a way around a network problem: with a
digest in hand and a registry that cannot be read, the run reports a **registry**
failure and stops, because a machine that cannot download is not evidence about the
artifact.

### 12.3 The verify step

`commands/verify` is a plain shell snippet, and the verdict it reaches is the only
thing that makes the run green. The contract it gets from the wrapper:

| variable | what it is |
| --- | --- |
| `PHYSICAL_WORK` | the run's temporary directory (the consumer project is `$PHYSICAL_WORK/consumer`) |
| `PHYSICAL_CONSUMER` | that consumer project |
| `PHYSICAL_REGISTRY` | the read registry |
| `PHYSICAL_SOURCE` | `registry` or `tarball` |
| `PHYSICAL_TARBALL` | the fallback tarball, when one was used |
| `PHYSICAL_EXPECTED_INTEGRITY` | the digest the pipeline published |

and the collectors it writes with, which are exported functions:

* `physical_fact <key> <value>` - one machine-readable fact (`binding`, `tarball`, …);
  repeated keys keep the last value;
* `physical_check <name> <result> <detail>` - one assertion, `ok`/`error`/`unknown`;
* `physical_elf <path>` - asserts the file is a RISC-V ELF object. `file` answers
  precisely; when `file` is not installed the ELF header is read instead (`e_machine
  243`) and the report carries `elf_check: elf-header(degraded)` plus a note, because
  a weaker check must not look like the same check.

Exit codes are verdicts, and the report keeps them apart because they are different
news for different people:

| code | verdict | who has to act |
| --- | --- | --- |
| 0 | `ok` | nobody |
| 10 | `environment` | the runner: wrong architecture, node < 20, no disk, another run holding the lock |
| 11 | `registry` | the network or the credential, not the package |
| 12 | `digest` | the artifact: the bytes here are not the bytes published |
| 13 | `install` | the artifact: npm could not install it |
| 14 | `execute` | the artifact: it installed and does not work here |

### 12.4 Runner hygiene

The hardware runner is a long-lived container that other jobs use, and it is
configured by hand (no sudo in the step). So:

* every run works inside one `mktemp -d` directory with its own `HOME`,
  `npm_config_cache` and `npm_config_userconfig`, removed by a trap on every exit
  path - including failure. Nothing is installed globally, ever;
* a `flock` on `${PHYSICAL_LOCK:-$RUNNER_TEMP/physical-verify.lock}` plus
  `concurrency: physical-riscv` (with `cancel-in-progress: false`) keep two runs
  apart, and the workflow sweeps temporary directories older than six hours so a
  killed run cannot poison the next one;
* node >= 20 is a hard gate: verifying under a runtime a user does not run would
  produce a green light nobody can reproduce. The failure names the one-time install
  commands for the host instead of attempting them;
* `PHYSICAL_KEEP_WORK=true` keeps the directory for inspection (a deliberate
  debugging lever, never set by a workflow).

### 12.5 The report

`physical-report-<slug>.json` is written on **every** exit path, including
`environment`, and holds the verdict, the digests (expected and installed), the
binding the recipe found, what `file` said, the collected facts and checks, and the
host (architecture, cpu model, ISA string, glibc, node, npm). The workflow renders it
as a table in the job summary, uploads it as an artifact, and exits non-zero on
anything but `ok`/`skipped`. Nothing is written back into the repository: the report
is evidence, not state.

### 12.6 Rehearsing without hardware

Everything except the silicon can be rehearsed locally, and the rehearsal runs the
same wrapper the hardware runner runs:

```bash
python3 tests/rehearse_physical.py --pkg bufferutil --ver 4.2.0 \
    --registry http://127.0.0.1:4890 --token-env-file .dsh/tmp/token
```

It renders the publish action and publishes into the given registry, renders the
physical action, merges the publish report into a plan, and runs `steps/physical.sh`
inside the riscv64 image (`--network host`, workspace mounted at `/w`), then prints
the verdict and the checks. The image's npm 9.2.0 exercises the array-shaped
`npm pack --json`; npm 11/12 exercise the object shape.

What the rehearsal cannot show is what a first real run reports as `environment`:
node >= 20 availability, `file` and `flock` presence, the cpu model and ISA string,
and whether that box can reach the registry. `PHYSICAL_ALLOW_FOREIGN_ARCH=true` is
the only way to run the wrapper outside a riscv64 environment; it exists so the
rehearsal is possible, and a workflow never sets it.
