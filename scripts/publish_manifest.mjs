#!/usr/bin/env node
// Prepares the manifest that gets published, and nothing else.
//
// This lives in its own file instead of being embedded in a `node -e '...'` string:
// quoting a JavaScript literal inside a shell script had become a recurring source
// of bugs (one apostrophe in a message broke the whole step). The publish script
// still owns downloading, unpacking, overlaying and calling npm.
//
// Every mode takes the publish spec the renderer generated, as JSON on argv:
//
//   --shell <spec>                        P_* shell assignments for the caller
//   --verify-integrity <spec> <tarball>   compare sha512 with base.integrity
//   --prepare <spec> <dir>                write <dir>/package.json (+ README.md)
//   --assert-served <spec> <dir>          fail unless the registry serves exactly these bytes
//   --plan <spec> <dir>                   P_PACKED_BYTES / P_FETCH_TIMEOUT for the upload
//
// The spec mirrors one npm_publish entry after the renderer resolved it:
//   { name, version, registry, dir, kind, manifest, base?, overlay?, allow_overwrite?,
//     optional?, os?, cpu?, libc?, main?, engines?, manifest_json?, readme_text? }
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const argv = process.argv.slice(2);
const mode = argv[0];

function fail(message) {
  console.error("[publish] " + message);
  process.exit(1);
}

function readSpec(value) {
  // The publish script feeds the spec inline (a quoted heredoc); a path is read from
  // disk, which keeps longer specs reviewable in tests.
  const raw = String(value ?? "").trim();
  const text = raw.startsWith("{") ? raw : readFileSync(raw, "utf8");
  try {
    return JSON.parse(text);
  } catch (error) {
    fail("bad publish spec: " + error.message);
  }
}

function shellQuote(value) {
  return "'" + String(value ?? "").replace(/'/g, "'\\''") + "'";
}

function sameRegistry(left, right) {
  const trim = (value) => String(value ?? "").replace(/\/+$/, "");
  return trim(left) === trim(right);
}

const spec =
  mode === "--shell" || mode === "--verify-integrity" || mode === "--prepare" ||
  mode === "--assert-served" || mode === "--plan"
    ? readSpec(argv[1])
    : fail("unknown mode: " + mode);

if (mode === "--shell") {
  // Only what the shell itself needs; the manifest work stays here.
  const scalars = {
    NAME: spec.name,
    VERSION: spec.version,
    REGISTRY: spec.registry,
    KIND: spec.kind || "source-tree",
    DIR: spec.dir || ".",
    MANIFEST: spec.manifest || "generated",
    BASE_URL: (spec.base || {}).url || "",
    BASE_INTEGRITY: (spec.base || {}).integrity || "",
    // Space separated: overlay paths are validated to contain no whitespace.
    OVERLAY: (spec.overlay || []).join(" "),
    ALLOW_OVERWRITE: spec.allow_overwrite ? "true" : "",
  };
  for (const [key, value] of Object.entries(scalars)) {
    console.log("P_" + key + "=" + shellQuote(value));
  }
  process.exit(0);
}

if (mode === "--verify-integrity") {
  const tarball = argv[2];
  const expected = (spec.base || {}).integrity || "";
  if (!expected) {
    fail("the publish spec has no base.integrity; refusing to publish an unverified tarball");
  }
  if (!existsSync(tarball)) fail("base tarball is missing: " + tarball);
  const digest =
    "sha512-" + createHash("sha512").update(readFileSync(tarball)).digest("base64");
  if (digest !== expected) {
    fail("base tarball integrity mismatch: expected " + expected + " but got " + digest);
  }
  console.log("[publish] base tarball verified (" + expected.slice(0, 19) + "...)");
  process.exit(0);
}

if (mode === "--prepare") {
  const dir = argv[2];
  const manifestMode = spec.manifest || "generated";
  const file = join(dir, "package.json");

  let pkg;
  if (manifestMode === "template") {
    if (!spec.manifest_json) fail("manifest=template without manifest_json");
    pkg = { ...spec.manifest_json };
  } else if (existsSync(file)) {
    pkg = JSON.parse(readFileSync(file, "utf8"));
  } else if (manifestMode === "upstream") {
    fail("missing manifest " + file + "; manifest=upstream publishes the base package as it is");
  } else {
    pkg = {};
  }

  // Name and version are asserted in every mode; only the generated and template
  // shapes are allowed to write them.
  if (manifestMode !== "upstream") {
    if (pkg.name !== spec.name) {
      console.log("[publish] name " + (pkg.name || "(none)") + " -> " + spec.name);
    }
    pkg.name = spec.name;
    pkg.version = spec.version;
  } else {
    if (pkg.name !== spec.name) {
      fail("the base package is " + pkg.name + ", the recipe publishes " + spec.name);
    }
    if (String(pkg.version) !== String(spec.version)) {
      fail("the base package is " + pkg.version + ", the recipe publishes " + spec.version);
    }
  }

  const changes = [];
  if (manifestMode !== "upstream") {
    if (spec.main && pkg.main !== spec.main) {
      changes.push("main " + spec.main);
    }
    if (spec.main) pkg.main = spec.main;
    if (spec.os) pkg.os = [spec.os];
    if (spec.cpu) pkg.cpu = [spec.cpu];
    if (spec.libc) pkg.libc = [spec.libc];
    if (spec.engines) pkg.engines = spec.engines;
  }

  const hasOptional = Object.keys(spec.optional || {}).length > 0;
  if (hasOptional) {
    const upstreamDeps = { ...(pkg.optionalDependencies || {}) };
    const upstreamNames = Object.keys(upstreamDeps);
    pkg.optionalDependencies = { ...upstreamDeps };
    let added = 0;
    for (const [depName, depVersion] of Object.entries(spec.optional)) {
      const previous = pkg.optionalDependencies[depName];
      if (previous === depVersion) continue;   // already merged by an earlier run
      pkg.optionalDependencies[depName] = depVersion;
      changes.push("optionalDependencies " + depName + "@" + depVersion);
      if (previous === undefined) added += 1;
    }
    // The merge is additive and idempotent: no platform upstream lists may disappear,
    // and nothing but the packages this recipe publishes may appear. "Count must grow"
    // would have refused the second run of a recipe that publishes out of the build
    // tree, because the first run already wrote the entry into that manifest.
    for (const name of upstreamNames) {
      if (pkg.optionalDependencies[name] === undefined) {
        fail("optionalDependencies lost " + name + ", a platform the base package ships");
      }
    }
    const extra = Object.keys(pkg.optionalDependencies).filter(
      (name) => !upstreamNames.includes(name) && !(name in spec.optional)
    );
    if (extra.length) {
      fail("optionalDependencies gained " + extra.join(", ") + ", which the recipe does not publish");
    }
    console.log(
      added
        ? "[publish] added " + added + " platform package(s) to optionalDependencies"
        : "[publish] optionalDependencies already list " + Object.keys(spec.optional).join(", ")
    );
  }

  // npm gives publishConfig.registry precedence over --registry, so a base package
  // that pins its own registry would send the tarball somewhere else (D13).
  if (spec.registry) {
    pkg.publishConfig = { ...(pkg.publishConfig || {}) };
    if (!pkg.publishConfig.access) pkg.publishConfig.access = "public";
    if (!sameRegistry(pkg.publishConfig.registry, spec.registry)) {
      changes.push(
        "publishConfig.registry " +
          (pkg.publishConfig.registry || "(none)") +
          " -> " +
          spec.registry
      );
    }
    pkg.publishConfig.registry = spec.registry;
  }

  if (spec.readme_text) {
    const readme = join(dir, "README.md");
    if (!existsSync(readme)) {
      writeFileSync(readme, spec.readme_text);
      changes.push("README.md written");
    }
  }

  writeFileSync(file, JSON.stringify(pkg, null, 2) + "\n");
  console.log(
    "[publish] " + file + " (" + manifestMode + ") " +
      (changes.length ? "changed: " + changes.join("; ") : "unchanged")
  );
}

// npm itself is used here, not a registry client: the tarball npm would upload and the
// credential the step configured are both npm's, so a second implementation could
// disagree with them and report a false conflict.
function runNpm(args, cwd) {
  // npm runs as a node script when npm_execpath is set (the usual case inside a step).
  const command = process.env.npm_execpath
    ? [process.execPath, process.env.npm_execpath]
    : ["npm"];
  const [cmd, ...prefix] = command;
  const result = spawnSync(cmd, [...prefix, ...args], {
    cwd,
    encoding: "utf8",
    env: process.env,
  });
  return { status: result.status, stdout: result.stdout || "", stderr: result.stderr || "" };
}

// dry-run packs without writing the tarball, and reports the digest of the bytes npm
// would upload: the same code path `npm publish` uses for the directory.
function packedIntegrity(dir) {
  const result = runNpm(
    ["pack", "--dry-run", "--json", "--ignore-scripts", dir],
    dir,
  );
  if (result.status !== 0) {
    return { error: result.stderr.trim() || "npm pack failed" };
  }
  let parsed;
  try {
    parsed = JSON.parse(result.stdout);
  } catch (error) {
    return { error: "cannot read npm pack output: " + error.message };
  }
  // npm <=10 prints an array, npm >=11 an object keyed by package name.
  const entry = Array.isArray(parsed) ? parsed[0] : Object.values(parsed)[0];
  if (!entry || !entry.integrity) {
    return { error: "npm pack reported no integrity" };
  }
  return {
    integrity: entry.integrity,
    shasum: entry.shasum || "",
    size: Number(entry.size) || 0,
    files: entry.files || [],
  };
}

function servedManifest() {
  const result = runNpm(
    ["view", spec.name + "@" + spec.version, "--json", "--registry", spec.registry],
    null,
  );
  if (result.status !== 0) {
    return { error: result.stderr.trim() || "npm view failed" };
  }
  let parsed;
  try {
    parsed = JSON.parse(result.stdout);
  } catch (error) {
    return { error: "cannot read npm view output: " + error.message };
  }
  return { manifest: versionDocument(parsed) };
}

// npm <= 11 prints the version manifest; npm >= 12 prints a one-element list (its
// "queryable" output). Reading the list as a manifest is what once made a matching
// digest look like a missing one, so both shapes are normalised here.
// npm's `fetch-timeout` is a wall clock budget for the whole request - it starts when
// the socket is assigned and covers the upload as well as the answer, in every npm
// version from 9 to 12 (minipass-fetch keeps the same timer). The default 300 s
// therefore caps a publish by the time it takes *to upload*, which for a package
// carrying a native binding is minutes: 9.3 MB of tarball becomes 12.2 MB of base64
// JSON, and a route slower than ~40 kB/s cannot fit it in 300 s. npm then aborts
// mid-body, retries (each attempt re-uploading everything), and the registry never
// sees a complete request - "the request never arrived" from its point of view.
//
// So the budget is derived from the payload instead of assumed: a floor of 300 s
// (npm's default, correct for ordinary packages) plus what the bytes need at a
// deliberately pessimistic 8 kB/s, capped at an hour. PUBLISH_FETCH_TIMEOUT overrides
// it outright, for an operator who knows the route.
const MIN_FETCH_TIMEOUT_MS = 300_000;
// The route that carried the 9.3 MB binding in 148 s ran at about 84 kB/s. Assuming
// 16 kB/s leaves room for a route five times slower than that and still finishes.
const ASSUMED_BYTES_PER_SECOND = 16_000;
const MAX_FETCH_TIMEOUT_MS = 3_600_000;
// Base64 wraps the tarball: the request body is about a third larger than the file.
const PAYLOAD_INFLATION = 1.4;
// Above this, a retry re-uploads megabytes for nothing, so npm retries once instead
// of twice. Below it the default is kept: a retry is cheap and usually a blip.
const RETRY_ONCE_ABOVE_BYTES = 4 * 1024 * 1024;

function fetchTimeoutFor(bytes, override) {
  if (override) {
    const value = Number(override);
    if (!Number.isFinite(value) || value <= 0) {
      fail("PUBLISH_FETCH_TIMEOUT must be a positive number of milliseconds");
    }
    return Math.round(value);
  }
  const transfer = Math.ceil((bytes * PAYLOAD_INFLATION) / ASSUMED_BYTES_PER_SECOND) * 1000;
  return Math.min(MAX_FETCH_TIMEOUT_MS, MIN_FETCH_TIMEOUT_MS + transfer);
}

function versionDocument(parsed) {
  if (Array.isArray(parsed)) {
    return parsed.find((entry) => entry && typeof entry === "object") || {};
  }
  return parsed && typeof parsed === "object" ? parsed : {};
}

// npm keys credentials by a URL's "nerf dart" (scheme dropped, last segment dropped)
// and matches the longest configured key, so the same rules are used here for the
// tarball download - a registry that exposes no digest still has to be checked.
function nerfDart(url) {
  const parsed = new URL(url);
  const pathname = parsed.pathname.endsWith("/")
    ? parsed.pathname
    : parsed.pathname.replace(/\/[^/]*$/, "/");
  return "//" + parsed.host + pathname;
}

function credentialFor(url) {
  const configPath = process.env.NPM_CONFIG_USERCONFIG;
  if (!configPath || !existsSync(configPath)) return null;
  const dart = nerfDart(url);
  let best = null;
  for (const line of readFileSync(configPath, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#") || trimmed.startsWith(";")) continue;
    // Split at the first "=" only: base64 values carry their own padding.
    const separator = trimmed.indexOf("=");
    if (separator < 0) continue;
    const key = trimmed.slice(0, separator).trim();
    const value = trimmed.slice(separator + 1).trim();
    // npm's keys are "<nerf dart>:_authToken" / "<nerf dart>:_auth", and a port sits
    // inside the dart, so the key is cut at the credential suffix rather than at a
    // colon.
    const at = key.lastIndexOf(":_auth");
    if (at < 0 || !value) continue;
    const keyDart = key.slice(0, at);
    if (!dart.startsWith(keyDart)) continue;
    if (!best || keyDart.length > best.keyDart.length) {
      best = { keyDart, kind: key.slice(at + 1), value };
    }
  }
  if (!best) return null;
  return best.kind === "_auth" ? "Basic " + best.value : "Bearer " + best.value;
}

async function fetchTarball(url, capBytes) {
  const headers = { "user-agent": "npm-riscv64 publish check" };
  const credential = credentialFor(url);
  if (credential) headers.authorization = credential;
  let response;
  try {
    response = await fetch(url, {
      headers,
      redirect: "follow",
      signal: AbortSignal.timeout(180_000),
    });
  } catch (error) {
    return { error: "cannot download " + url + ": " + error.message };
  }
  if (response.status === 401 || response.status === 403) {
    return { error: "the registry refused the tarball download (" + response.status + ")" };
  }
  if (!response.ok) {
    return { error: "tarball download answered " + response.status };
  }
  const length = Number(response.headers.get("content-length") || 0);
  if (length && length > capBytes) {
    return { error: "the tarball is " + length + " bytes, above the verification cap" };
  }
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length > capBytes) {
    return { error: "the tarball is " + bytes.length + " bytes, above the verification cap" };
  }
  return { bytes };
}

function sha1Hex(bytes) {
  return createHash("sha1").update(bytes).digest("hex");
}

function sha512Sri(bytes) {
  return "sha512-" + createHash("sha512").update(bytes).digest("base64");
}

if (mode === "--plan") {
  // What the upload will cost, decided before it starts: the packed size drives the
  // fetch timeout, and both are logged so a slow route is visible in the step output.
  const dir = resolve(argv[2]);
  const ours = packedIntegrity(dir);
  if (ours.error) fail("cannot size up this publish: " + ours.error);
  const timeout = fetchTimeoutFor(ours.size, process.env.PUBLISH_FETCH_TIMEOUT);
  console.log("P_PACKED_BYTES=" + shellQuote(String(ours.size)));
  console.log("P_FETCH_TIMEOUT=" + shellQuote(String(timeout)));
  console.log(
    "P_FETCH_RETRIES=" +
      shellQuote(ours.size >= RETRY_ONCE_ABOVE_BYTES ? "1" : "")
  );
}

if (mode === "--assert-served") {
  // A published package is a claim about what consumers install. The registry answer to
  // `npm publish` says the upload was accepted; this says the registry now serves
  // *these* bytes. They are not the same statement, so the step is only green when the
  // second is established - and when the registry cannot be checked at all, the step
  // says so instead of pretending either way.
  //
  // The check has to survive three registry behaviours, all observed:
  //   * `dist.integrity` present (npmjs, verdaccio, GitHub Packages) - compare it;
  //   * only `dist.shasum` (older hosted repositories) - compare the sha1;
  //   * neither, only `dist.tarball` (the private registry this repository rehearses
  //     against) - download the tarball and compare the bytes.
  //
  // `--accepted` says `npm publish` returned success. That changes what an
  // unverifiable answer means: after an accepted upload it is a warning (the registry
  // simply does not publish digests), while after a refusal it is a failure, because
  // then there is no evidence at all that our bytes are the ones being served.
  // Resolved: `npm pack` runs with this directory as its cwd, so a relative path would
  // resolve against itself.
  const dir = resolve(argv[2]);
  const accepted = argv.includes("--accepted");
  const capBytes = Number(process.env.PUBLISH_VERIFY_MAX_BYTES || 64 * 1024 * 1024);
  const strategy = (process.env.PUBLISH_VERIFY || "auto").toLowerCase();

  const ours = packedIntegrity(dir);
  if (ours.error) fail("cannot compute what this publish would upload: " + ours.error);

  const theirs = servedManifest();
  if (theirs.error) {
    fail(
      "cannot read what " + spec.name + "@" + spec.version + " serves at " +
        spec.registry + ": " + theirs.error
    );
  }
  const served = theirs.manifest;
  const dist = served.dist || {};

  // The served manifest has to reference the platform packages this recipe publishes,
  // otherwise an install would not pull the riscv64 build. Absence is fine for a
  // recipe that publishes no platform package.
  const expected = spec.optional || {};
  for (const [depName, depVersion] of Object.entries(expected)) {
    const actual = (served.optionalDependencies || {})[depName];
    if (actual !== depVersion) {
      fail(
        "the served manifest does not reference " + depName + "@" + depVersion +
          " (" + (actual ? "it lists " + actual : "it has no such entry") + "); " +
          "installing this package would not pull the riscv64 build"
      );
    }
  }

  // Nothing to compare against, so there is no verdict - and what that means depends
  // on what the upload did: after an accepted 201 the package is there and only the
  // check was impossible (a warning), while after a refusal nothing at all shows our
  // bytes are what a consumer would get (a failure).
  const unverifiable = (reason) => {
    if (accepted) {
      console.log(
        "[publish] WARNING: " + reason + "; " + spec.name + "@" + spec.version +
          " was accepted but not verified"
      );
      process.exit(0);
    }
    fail(
      "cannot verify what " + spec.name + "@" + spec.version + " serves: " + reason + "\n" +
        "the publish was refused and the registry gave nothing to check, so there is no " +
        "evidence our artifact is the one on offer. Enable redeploy (or delete that " +
        "version) on the registry, or give the recipe a version it can own."
    );
  };

  const describe = (digest) => (digest ? digest : "(none)");
  const mismatch = (what, theirsValue) =>
    fail(
      "the registry does not serve this artifact: " + what + "\n" +
        "  this publish:  " + ours.integrity + "\n" +
        "  it serves:     " + describe(theirsValue) + "\n" +
        "that version holds something else - upstream's copy, or an earlier publish of " +
        "ours - and the registry does not let a publish replace it. Nothing was " +
        "delivered: enable redeploy (or delete that version) on the registry, or give " +
        "the recipe a version it can own."
    );

  if (strategy !== "tarball" && dist.integrity) {
    if (dist.integrity !== ours.integrity) mismatch("the digests differ", dist.integrity);
    console.log(
      "[publish] the registry serves this artifact (dist.integrity " +
        ours.integrity.slice(0, 19) + "...)"
    );
    process.exit(0);
  }

  if (strategy === "digest") {
    // An operator asked for digests only: no download, no invented verdict.
    unverifiable(
      "the registry publishes no dist.integrity (PUBLISH_VERIFY=digest skips the " +
        "tarball check)"
    );
  }

  if (dist.shasum && strategy !== "tarball") {
    const oursSha1 = ours.shasum;
    if (!oursSha1) {
      unverifiable(
        "npm reported no shasum for this publish, so the registry's dist.shasum has " +
          "nothing to compare with"
      );
    }
    if (dist.shasum !== oursSha1) mismatch("the shasums differ", "sha1 " + dist.shasum);
    console.log(
      "[publish] the registry serves this artifact (dist.shasum " + oursSha1 + ")"
    );
    process.exit(0);
  }

  if (!dist.tarball) {
    unverifiable("the registry exposes no digest and no tarball URL");
  }

  const downloaded = await fetchTarball(dist.tarball, capBytes);
  if (downloaded.error) unverifiable(downloaded.error);

  const servedIntegrity = sha512Sri(downloaded.bytes);
  if (servedIntegrity !== ours.integrity) {
    const servedSha1 = sha1Hex(downloaded.bytes);
    mismatch(
      "the downloaded tarball differs (" + downloaded.bytes.length + " bytes, sha1 " +
        servedSha1 + ")", servedIntegrity
    );
  }
  console.log(
    "[publish] the registry serves this artifact (tarball sha512 " +
      servedIntegrity.slice(0, 19) + "..., " + downloaded.bytes.length + " bytes)"
  );
}
