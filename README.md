# npm-riscv64

Recipes for building riscv64 binaries of npm packages, plus the CI that turns them
into published packages.

Published packages keep the upstream API and follow npm's `optionalDependencies`
convention, so `npm install <package>` on riscv64 picks up a prebuilt binary
without a compiler and without the user noticing that anything special happened.

## How it works

```
packages/<pkg>/<version>/.stat      recipe: source, patches, build/test commands, publish targets
        │
        ├──> scripts/render.py      renders one recipe into a composite action
        │       .rendered/action/action.yml + steps/*.sh
        │
        ├──> .github/workflows/build.yml
        │       pull_request -> fetch, patch, build, test
        │       push to main -> the same, plus publish
        │
        └──> docs/recipe-spec.md    the recipe format, in full
```

* A recipe is data (`.stat`) plus shell snippets (`commands/`), with an optional
  `common/` layer shared by all versions of a package.
* Every version directory is a buildable recipe; version directories are the
  complete set, and each one must carry `.stat`.
* Building and testing run inside a riscv64 container (`docker run --platform
  linux/riscv64`), so the result is a real RISC-V object, not a cross-compiled
  guess.
* Publishing is either a native package plus a small JS package that references it
  through `optionalDependencies`, or a single package that ships the prebuild.

## Quick start

```bash
# validate every recipe and render it (no build)
python3 scripts/render.py --check-all

# render one recipe into an action plus its step scripts
python3 scripts/render.py --pkg bufferutil --ver 4.2.0 --out-dir .rendered/action

# what would CI build for the current change?
EVENT_NAME=pull_request BASE_REF=main python3 scripts/detect_targets.py
```

## Adding a package

1. `mkdir -p packages/<pkg>/<version>` (scoped packages keep the `@scope` segment:
   `packages/@scope/name/1.2.3`).
2. Write `.stat`: where the source comes from, the artifact glob, and the publish
   targets.
3. Write `commands/build` and `commands/test`. The test must prove the artifact is
   really riscv64 (`file`), not just that a build command exited zero.
4. `python3 scripts/render.py --check-all`, then
   `python3 tests/run_action_locally.py --pkg <pkg> --ver <version>`.

`docs/recipe-spec.md` documents the fields, the merge rules, the generated
fetch/patch/publish snippets, and the validation rules.

Rendered output for the reference recipe is snapshotted under
`tests/golden/bufferutil-4.2.0/`. If you change the template or the renderer and the
test fails, read the diff - when the change is intended, refresh the snapshot with
`python3 tests/regen_golden.py` and include it in the same commit.

## Verifying

| level | command | covers |
| --- | --- | --- |
| L0 | `python3 -m unittest discover -s tests` | renderer, layering, change detection, rendered YAML validity, golden snapshot of the reference recipe |
| L1 | `python3 tests/run_action_locally.py --pkg bufferutil --ver 4.2.0` | the rendered action actually fetches, builds and tests under riscv64 emulation |
| L2 | `python3 tests/l2_publish.py` | publish order A→B, idempotency, manifest merge, name identity, partial-failure re-entry (verdaccio) |
| L2 | `python3 tests/verify_published.py bufferutil 4.2.0` | what the registry serves, compared file by file with upstream's own tarball (`--shape prebuild\|wrapper\|platform`) |
| patches | `python3 scripts/check_patches.py [--network]` | `upstream/*.patch` are real, declared, repository-free diffs that still apply |
| L3 | GitHub Actions in a fork | emulated qemu, artifact reuse, real registry publishing |

L1 and L2 need docker. For L1, build the toolchain image once:

```bash
docker build -f docker/riscv64-toolchain.Dockerfile -t npm-riscv64/toolchain:riscv64 .
```

## Caching

A finished workspace tree is uploaded as an artifact keyed by a hash of the *rendered*
build commands (the fetch/patch/build steps, the patch files, the image id). A later run
with the same key restores the tree and skips fetch, patch and build - the test step
still runs, and publish works off the restored tree. A miss just builds.

Because the key describes what actually runs, it also decides what a change touches: a
template or renderer edit that adds an input, an `if:` guard or a comment changes no
command, so no recipe is rebuilt, while recipe changes always rebuild. See
`docs/recipe-spec.md` for the exact rules.

## Container images

There are two, matching the `toolchain:` value of a recipe:

| image | used by | contents |
| --- | --- | --- |
| `npm-riscv64-toolchain:riscv64` | `native` recipes (default) | riscv64 base, node, node-gyp, native build tools |
| `npm-riscv64-toolchain:cross` | `cross` recipes | amd64 base, pinned rust toolchain + `riscv64-linux-gnu` gcc, cross linker env |

A cross recipe builds in the first image's amd64 counterpart and *tests* in the
riscv64 one, so the artifact is still executed on the target CPU. Neither image is
built on every run: build them once, publish them, and let CI pull them.

```bash
# local builds (the riscv64 one needs qemu binfmt, or docker buildx --platform)
docker build --platform linux/riscv64 \
  -f docker/riscv64-toolchain.Dockerfile \
  -t ghcr.io/<owner>/npm-riscv64-toolchain:riscv64 .
docker build --platform linux/amd64 \
  -f docker/riscv64-cross.Dockerfile \
  -t ghcr.io/<owner>/npm-riscv64-toolchain:cross .

# publish them (needs a token with write:packages)
echo "$GH_TOKEN" | docker login ghcr.io -u <owner> --password-stdin
docker push ghcr.io/<owner>/npm-riscv64-toolchain:riscv64
docker push ghcr.io/<owner>/npm-riscv64-toolchain:cross
```

`ghcr.io/<owner>/...` must be lowercase, so `RuyiRepo` becomes `ruyirepo`. The
`build toolchain images` workflow does the same on a runner
(`Actions -> build toolchain images -> Run workflow`); run it again whenever a
`docker/*.Dockerfile` changes.

CI resolves the build image as `vars.RISCV_IMAGE` when set, otherwise
`ghcr.io/${{ github.repository_owner }}/npm-riscv64-toolchain:riscv64`, and the
cross image as `vars.RISCV_CROSS_IMAGE` or (from a custom `RISCV_IMAGE`) the same
repository with the `:cross` tag. Both are pulled before the build; the cross image
only by the jobs that use it. It only authenticates when a `GHCR_TOKEN` secret is set: a public
package pulls anonymously, and logging in with the wrong token turns a public image
into `403 Forbidden` (docker never falls back to anonymous once credentials exist).
For a private package, either connect the package to the repository or add a token
with `read:packages` as `GHCR_TOKEN`. Locally, L1 works the same way: it uses the
local tag if present and pulls `RISCV_IMAGE` when it is missing.

Each tag holds one platform only, so a pull has to name it:
`docker pull --platform linux/riscv64 <image>:riscv64`. Without `--platform` the
daemon asks for the platform of the host and the registry answers
`no matching manifest for linux/amd64 in the manifest list entries`. The action and
`build.yml` do this for you; it only matters when you pull by hand.

Pushing the images is a permission question about the **package**, not about the
workflow: `packages: write` is not enough when the package is not linked to the
repository. A package that was first pushed by hand with a personal token stays
unlinked, and the workflow token then keeps read-only access - the push fails with
`denied: permission_denied: read_package`. Fix it either way:

* link the package: `Package settings -> Manage Actions access -> add <owner>/<repo>`
  with the Write role (the image workflow also labels images with
  `org.opencontainers.image.source`, so a later push links them automatically), or
* set a `GHCR_TOKEN` secret holding a classic PAT with `write:packages`; the image
  workflow prefers that secret when it is present.

## Running it in a fork

Development happens in a fork; the repository itself lives at `RuyiRepo/ruyirepo`,
and nothing here is pushed there directly. A fork is also the only place the whole
pipeline can be exercised: L0-L2 run locally, but a real GitHub Actions run and a real
registry publish exist nowhere else (that is "L3" in the tables above).

Setup, in order:

1. **Fork** `RuyiRepo/ruyirepo` into the account that will run the tests, and work on a
   branch there.
2. **Toolchain images.** Run `Actions -> build toolchain images -> Run workflow` once,
   so `ghcr.io/<fork-owner>/npm-riscv64-toolchain:riscv64` (and `:cross`) exist. Public
   packages pull anonymously; if a package is private, connect it to the fork or add a
   `GHCR_TOKEN` secret (see the section above).
3. **Registry credential.** `gh secret set NPM_PUBLISH_TOKEN --repo <fork-owner>/npm-riscv64`
   with the value the recipe's registry expects (`auth: basic` -> base64 of
   `user:password`, `auth: token` -> the token itself). Without this secret everything
   still builds and tests; only the publish step stops.
4. **Optional variables**, for pointing the run somewhere else without editing recipes:

   | variable | why |
   | --- | --- |
   | `PUBLISH_REGISTRY` | publish to another registry than the one `.stat` names |
   | `PUBLISH_SCOPE` | rewrite every publish target to `@<scope>/...` (GitHub Packages needs it to equal the owner) |
   | `SKIP_PUBLISH` | `true` keeps the build running and skips publishing |
   | `RISCV_IMAGE`, `RISCV_CROSS_IMAGE` | use your own toolchain images |
   | `RISCV_RUNNER` | send the jobs to a self-hosted runner (see the next section) |

What a run does:

* **pull request** - detect, fetch, patch, build, test. No publishing: the merge is the
  approval gate (D5).
* **push to `main`** - the same, plus publishing. A recipe whose artifact did not change
  but whose publish step did is delivered again from the cached tree, without rebuilding.
* **`workflow_dispatch` with `republish`** - publish every recipe from the cached trees
  and build nothing. This is the lever for a delivery fix: publishing is verified by
  reading back what the registry serves, so a registry that rejected an earlier push
  fails loudly here instead of staying quietly wrong.

Things that surprise people the first time:

* the first run of a recipe is slow (qemu, plus `npm i` inside the container); later
  runs reuse the cached tree, keyed by the recipe, the template, the renderer and the
  image id;
* a red publish step does not mean the build is broken - the four build steps finished
  before it, and their artifacts are uploaded regardless;
* the fork's `main` is what publishes. A run on a feature branch never publishes, so a
  branch has to be merged into the fork's `main` to see the delivery end to end;
* the registry credential belongs to whoever administers the target registry. A fork
  cannot publish to `RuyiRepo`'s registry just by existing - it needs a credential that
  repository accepts, or `PUBLISH_REGISTRY` pointing at its own.

## Self-hosted runners

The workflow runs on `ubuntu-latest` by default, which costs minutes of emulation: qemu
runs the riscv64 container everywhere, and a Rust workspace (rolldown) spends most of
its time in the emulated compiler. A self-hosted runner moves the work onto a machine
you control - and, if that machine is a `riscv64` docker host, off the emulator
entirely - which is the intended setup for real use.

### Which host can actually run it

GitHub publishes runner binaries for linux **x64 and arm64** only (no `riscv64`
artifact in `actions/runner` releases), so plan for one of these:

| setup | what runs where | trade-off | which install below |
| --- | --- | --- | --- |
| runner on an x64/arm64 host, `binfmt_misc` installed | every container still emulates riscv64 | simplest and fully supported; better than hosted only because the box is yours and the images stay pulled | the archive, as printed |
| runner on x64/arm64, docker daemon on a `riscv64` host (`DOCKER_HOST=ssh://riscv64-host`) | containers run natively | the step mounts `$GITHUB_WORKSPACE` with `-v`, and that path is resolved on the *daemon's* host, so the same path (and the checkout) has to exist there - a shared filesystem at an identical path, or nothing works | the archive, plus the `DOCKER_HOST` settings after it |
| runner on a `riscv64` host | containers run natively | needs a runner built for riscv64 yourself (.NET 8 on riscv64) - unsupported upstream, so only if you maintain it | **not** the archive: there is no riscv64 download, so replace the download line with your own build |

### Install it

The commands below are the ones `Settings -> Actions -> Runners -> New runner` prints
for your repository - use that page for the current download URL, since the version
changes often. `svc.sh` is **not** something you write: it is shipped inside the runner
archive (a systemd wrapper generated from `systemd.svc.sh.template`, which writes
`/etc/systemd/system/actions.runner.<repo>.<name>.service` from
`bin/actions.runner.service.template`). Only `config.sh`, `run.sh` and `svc.sh` from
that archive are involved - no other scaffolding.

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
# URL, version and architecture are printed by
# Settings -> Actions -> Runners -> New runner; <arch> is x64 or arm64, and the page
# picks it for the host you are on.
curl -fsSL -o runner.tar.gz \
  https://github.com/actions/runner/releases/download/v<version>/actions-runner-linux-<arch>-<version>.tar.gz
tar xzf runner.tar.gz                      # brings config.sh, run.sh, svc.sh

./config.sh --url https://github.com/RuyiRepo/ruyirepo \
  --token <registration-token> \
  --name riscv64-builder --labels riscv64-builder

sudo ./svc.sh install "$USER"              # systemd unit, runs as you
sudo ./svc.sh start
./svc.sh status                            # stop/uninstall exist too
```

These commands are the whole installation for the first two setups - both keep the
runner on a Linux x64/arm64 host, so the archive is the right one and nothing else is
needed to register it. The third setup (a native riscv64 host) has no archive to
download: it needs a runner you build and maintain yourself, and the commands above
still apply once your build produces the same layout (`config.sh`, `run.sh`, `svc.sh`);
if it only produces `run.sh`, supervise that instead.

A host without systemd (containers, Alpine, WSL1) cannot use `svc.sh`; run
`./run.sh` under your own supervisor instead (`tmux`, `s6`, `supervisord`, or a
container that stays up) and keep the same label. `svc.sh` itself insists on root
(`sudo`) because `systemctl` does, and honours `GITHUB_ACTIONS_RUNNER_SERVICE_TEMPLATE`
if you need a custom unit.

For the second setup, the install is the same and the difference is configuration:
the runner talks to a riscv64 daemon instead of its own, so

* `DOCKER_HOST=ssh://riscv64-host` goes in `~/actions-runner/.env`, which the runner
  reads from its own root directory at startup and passes to every job (the systemd
  unit needs no edit for this - the listener sets the variable itself);
* the SSH key of the *runner* user must be able to reach the daemon host, and that user
  must be in `docker` on it;
* `$GITHUB_WORKSPACE` is bind-mounted as `-v "$GITHUB_WORKSPACE:/w"`, resolved on the
  daemon's host: the same absolute path has to exist there with the checked-out tree,
  which in practice means a shared filesystem (NFS/cephfs/9p) mounted at the identical
  path on both machines;
* `gh` is needed on the *runner* host (the cache restore runs there), not on the daemon
  host.

Requirements on the runner host:

| what | why |
| --- | --- |
| Docker, and the runner user in the `docker` group | every build and test step is a container |
| `riscv64` binfmt when the host is not a riscv64 docker host | otherwise `docker run --platform linux/riscv64` fails immediately; on a native daemon it costs nothing |
| `gh` CLI on `PATH`, authenticated or with `GH_TOKEN` | the cache restore asks the artifacts API which run holds the tree |
| ~50 GB disk, 8 cores, 16 GB RAM | rolldown's cargo build and the toolchain images are the heavy part |
| network access to the registry and to GHCR | toolchain images and upstream tarballs come from outside |

Then set the repository variable `RISCV_RUNNER=riscv64-builder`; `build.yml` uses it
for both jobs (`runs-on: ${{ vars.RISCV_RUNNER || 'ubuntu-latest' }}`), so a self-hosted
setup needs no workflow edit. The variable is one label, and the runner must carry
exactly that label. Leaving it unset keeps the hosted runners.

Two things a self-hosted runner changes about the build:

* **images are reused.** A hosted runner starts empty and pulls the toolchain image
  every time; a self-hosted one keeps it, so pre-pull once per host
  (`docker pull --platform linux/riscv64 ghcr.io/<owner>/npm-riscv64-toolchain:riscv64`)
  and point `RISCV_IMAGE` / `RISCV_CROSS_IMAGE` at that tag. The build cache key
  includes the image id, so a re-pulled (different) image invalidates the cached
  trees - correct, and worth knowing when a run unexpectedly rebuilds;
* **`--user` mapping.** Containers run as the calling user (the runner user), and the
  workspace belongs to it too; a runner started as a system service with a different
  uid than the checkout can leave unreadable files. `./svc.sh install "$USER"` keeps
  them the same - and the same rule applies to the daemon host in the `DOCKER_HOST` case.

## Publishing

Every recipe names its registry in `.stat`; the examples below use
`https://npm.example.com/repository/npm-local` as a stand-in for it - read that as
"the registry you publish to", not as an address this repository ships. The address in
`.stat` is deployment configuration: read it from there, not from this document, and
do not expect it to outlive the deployment behind it.

Packages are published under the **upstream names**, unscoped (`bufferutil`,
`utf-8-validate`, `rolldown`). That naming is deliberate: a consumer whose npm resolves
those names from a registry carrying these recipes gets riscv64 support without
renaming a single import.

The registry answers `401` with `WWW-Authenticate: Basic`, so the credential is HTTP
Basic, not an npm bearer token. Each `.stat` says how to send it:

```yaml
    registry: https://npm.example.com/repository/npm-local
    token_env: NODE_AUTH_TOKEN
    auth: basic        # token -> "_authToken", basic -> "_auth" (base64 user:password)
```

`token_env` names the **environment variable**, not the secret: the publish step reads
`$NODE_AUTH_TOKEN`, normalizes the registry URL to end with `/` (npm derives its
credential key from `new URL(".", registryUrl)`, so a missing slash makes npm look one
path segment higher and fail with `ENEEDAUTH` before sending anything), and writes the
matching line into a throwaway `.npmrc`
(`//npm.example.com/repository/npm-rv-test-local/:_auth=...`, keyed by
the full registry URL, path included). To get a credential, log in to the registry UI
with your account and take the base64 form:

```bash
printf '%s' 'user:password' | base64        # macOS: base64 | tr -d '\n'
gh secret set NPM_PUBLISH_TOKEN --repo <owner>/npm-riscv64   # paste the base64 value
```

The workflow maps `secrets.NPM_PUBLISH_TOKEN` to `NODE_AUTH_TOKEN` for the action, so
adding that secret is the only setup a fork needs. The publish step streams npm's
output with `--loglevel http`: a 9 MB upload takes a second from a machine next to the
registry but minutes over a slow route, and a captured npm would show nothing until it
finished.

How long the upload is allowed to take is derived from the package, not from npm's
default. `fetch-timeout` in npm is one wall-clock budget for the whole request - the
upload included, in every version from 9 to 12 - so 300 s is really a ceiling on the
route's speed: a binding-sized tarball (9.3 MB, 12.2 MB once base64-wrapped) cannot
finish below ~40 kB/s. When that ceiling bites, npm aborts mid-body, retries, and the
registry logs nothing at all, because it never got a complete request:

```
npm http fetch PUT ... attempt 1 failed with FETCH_ERROR
npm error network timeout at: https://<registry>/@rolldown%2fbinding-linux-riscv64-gnu
```

So the step sizes the package first and passes its own budget
(`upload 9128369 bytes, fetch timeout 1099000ms`), with one retry instead of two above
4 MB because a retry re-uploads the whole body. `PUBLISH_FETCH_TIMEOUT` (milliseconds)
overrides that for a route you know. A recipe pointed at a bearer-token registry
(GitHub Packages, Nexus npm tokens) uses `auth: token` instead and the secret holds the
token verbatim.

Repository variables override the recipes at render time, so a fork can publish
somewhere else without editing them:

| variable | value | effect |
| --- | --- | --- |
| `PUBLISH_SCOPE` | `ruyirepo` | rewrites every publish target to `@ruyirepo/...` (references included) |
| `PUBLISH_REGISTRY` | `https://npm.pkg.github.com` | points every publish target at that registry |
| `SKIP_PUBLISH` | `true` | keeps the build running but skips publishing |

A scope is only needed when the target registry demands one - GitHub Packages
requires it to equal the repository owner. Otherwise the unscoped upstream name is the
better default: it keeps `npm i rolldown` meaning what it always meant.

Two consequences of publishing under the upstream name, both worth knowing before a
consumer tries it:

* the registry must be the one the consumer resolves that name from. `npm-rv-test-local`
  is a hosted repository, so it carries only the packages published into it; a consumer
  pointing npm straight at it gets `404` for every ordinary dependency. Either put it in
  a group/virtual repository together with a proxy to npmjs, or map the scopes of the
  dependencies in the consumer's `.npmrc` - and set `replace-registry-host=never`, or
  npm rewrites the npmjs tarball URL to the default registry and the install 404s on
  the tarball instead of the metadata:

  ```ini
  registry=https://npm.example.com/repository/npm-local/
  //npm.example.com/repository/npm-rv-test-local/:_auth=<base64>
  @rolldown:registry=https://registry.npmjs.org/
  @oxc-project:registry=https://registry.npmjs.org/
  replace-registry-host=never
  ```

  Pointing the group repository at npm instead of mapping scopes is the simpler
  arrangement: `npm i rolldown --registry=https://…/repository/npm-rv-test-group`
  resolves our build first and every other package from its npmjs proxy, with no
  `.npmrc` changes and no `replace-registry-host` trap.
* a **globally** installed CLI is a separate copy. `npm i -g rolldown` fetches upstream
  unless it is pointed at this registry, and upstream carries none of the riscv64
  bindings, so the CLI on `PATH` keeps failing with "Cannot find native binding" even
  while the project install is correct. Reinstall it from the registry - or run the
  project copy with `npx rolldown` - and check what actually landed:

  ```
  npm i -g rolldown@1.3.3 --registry=https://npm.example.com/repository/npm-group
  ls -l /usr/local/lib/node_modules/@rolldown/binding-linux-riscv64-gnu/*.node   # the binding
  rolldown src/main.js --file bundle.js
  ```

  (A group repository with an npmjs proxy also makes `npm i -g` behave: the
  alternative is mapping `@rolldown` and `@oxc-project` to npmjs and setting
  `replace-registry-host=never`, as above.)

* a version that also exists upstream looks identical to a proxying registry. Hosted
  members win in a group repository, so consumers get the riscv64 build - but a
  `latest` that upstream has already moved past resolves to the binding-less upstream
  package, and publishing the same version again is refused by the registry as
  "already exists" (the publish step logs that as a skip).

### Non-riscv64 consumers

A published package is the upstream code with our riscv64 artifact added, so it also
installs and runs on other architectures - through upstream's own paths:

| consumer | `rolldown` | `bufferutil` / `utf-8-validate` |
| --- | --- | --- |
| linux riscv64 | our platform package `@rolldown/binding-linux-riscv64-gnu`, installed through the wrapper's `optionalDependencies` | our prebuild (`prebuilds/linux-riscv64/`) |
| linux x64 / arm64, macOS, Windows | upstream's optional binding package, resolved through the group repository's npmjs proxy | compiled from source by `node-gyp-build`, so a C/C++ toolchain is needed |

No manifest restricts the platform (`os`/`cpu` are unset), so `npm install` on x64
succeeds and our riscv64 artifact is simply unused there: the package is not
riscv64-only, and x64 users do not silently get a wrong binary. Verified on an x64 host
against the group repository - `npm i rolldown` plus a bundle runs on upstream's
`@rolldown/binding-linux-x64-gnu`, and `npm i bufferutil utf-8-validate` compiles
`build/Release/*.node` from source and runs. The hosted-only repository is not enough
for either: it lacks the ordinary dependencies (`@oxc-project/types`,
`node-gyp-build`, ...), so `npm i` fails with 404 before the platform question arises.

If a package must refuse to install elsewhere, say so in the recipe - the renderer
writes `os` and `cpu` into the published manifest:

```yaml
npm_publish:
  - role: main
    name: rolldown
    dir: out/rolldown
    os: linux
    cpu: riscv64
```

npm then rejects those installs with `EBADPLATFORM` instead of falling back to upstream
code, which is the honest signal when only our binary is meant to run.

Pull requests only build and test; merging to `main` is the approval gate that also
publishes. If publishing fails, the artifact is still there - the four build steps run
before it.

Publishing is not "did the upload return 200". A mirror that proxies npmjs already
carries upstream's copy of the version being republished, so the step publishes with
`--force` (letting the registry decide instead of npm's client-side refusal), then reads
back what the registry serves for that version and compares it with the digest of the
tarball it just built, and checks that the served manifest still references the platform
package. Green therefore means *the registry serves our bytes*; a version holding
someone else's artifact fails loudly instead of passing as "already published" - which is
exactly how a fork run once stayed green while the mirror kept serving `rolldown@1.3.3`
without the riscv64 binding.

Registries answer in three shapes, and the check reads all of them: a `dist.integrity`
(npmjs, verdaccio, GitHub Packages), only a `dist.shasum` (older hosted repositories),
or neither and just a `dist.tarball` - then the step downloads the tarball and compares
the bytes. The last shape costs a download of what was just uploaded, which on a slow
route is worth knowing: `PUBLISH_VERIFY=digest` skips it (the log then says the artifact
was accepted but not verified instead of claiming a check it did not do), and
`PUBLISH_VERIFY_MAX_BYTES` (64 MB by default) refuses anything above that size.

## Upstream increments

Every recipe that adds riscv64 support also carries the same increment written as a
pull request for upstream, under `upstream/`. Those patches are **not applied here** -
the recipes publish upstream's own artifacts plus our riscv64 addition - and they exist
so the work can be offered back instead of living only in this repository. Anyone can
read them and send them upstream as they are:

| recipe | increment | upstream files |
| --- | --- | --- |
| `bufferutil` 4.2.0 | `build a linux-riscv64 prebuild and ship it in the release tarballs` | `.github/workflows/ci.yml` |
| `utf-8-validate` 6.0.6 | same increment for its own CI | `.github/workflows/ci.yml` |
| `rolldown` 1.3.3 | `build and publish a riscv64 binding` | `packages/rolldown/package.json`, `.github/workflows/reusable-release-build.yml` |

`packages/<pkg>/<version>/upstream/*.patch` starts with a header naming the upstream
repository, the base ref and the files it touches, then a real unified diff generated
against a checkout of that ref. `scripts/check_patches.py` keeps them honest and runs
as part of `--check-all`:

* the patch is a genuine diff - no `/dev/null` sides, no invented headers, `a/` and
  `b/` prefixes intact;
* every file it changes is declared in `.stat: upstream_increments[].touches`, and
  nothing else is;
* it mentions nothing of this repository (registry hosts, mirrors, container files),
  because a patch that does cannot be submitted;
* the platform package name matches what napi-rs derives from the target triple
  (`-riscv64-gnu` for `riscv64gc-unknown-linux-gnu`), and the increment actually adds
  that target;
* with `--check-patches-network`, `git apply --check` against the pinned upstream ref,
  so a patch no longer applies is caught before someone opens the pull request.

The distinction matters and is enforced rather than documented: `patches/` holds
build-time patches this repository applies to make a build work, `upstream/` holds
increments for upstream. The generated `patch` step never touches `upstream/`, and a
test asserts that.

## Layout

```
packages/<pkg>/<version>/   recipes (.stat, commands/, patches/, upstream/, templates/,
                            tests/); <pkg> is the npm name as published upstream
                            (unscoped when upstream is)
docker/                     riscv64 toolchain image
scripts/                    renderer, change detector, recipe helpers
.github/templates/          the composite action template
.github/workflows/build.yml the single workflow for pull requests and main
docs/recipe-spec.md         recipe format specification
tests/                      L0 unit tests, L1 runner, L2 publish checks
```
