#!/usr/bin/env node
// Merges the publish reports a build left behind into the plan a hardware run reads,
// and turns the reports of that run into a summary.
//
// Two jobs, one file, because both are about the same question: what did this build
// actually publish, and what happened when the published package was installed on real
// riscv64 silicon (.dsh/plan.md D41)?
//
// Modes:
//   --merge <dir> --out <file>   merge publish-report-*.json into {name: report}
//   --summary <dir>              print the markdown table for $GITHUB_STEP_SUMMARY
//   --verdict <dir>              exit 1 if any report is a failure (environment excepted)
//
// Nothing here talks to a registry: these files were written by the publish step, which
// already established what the registry serves.
import { existsSync, readdirSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

const argv = process.argv.slice(2);
const mode = argv[0];

function fail(message) {
  console.error("[physical-plan] " + message);
  process.exit(2);
}

function readReports(dir) {
  const path = resolve(dir);
  if (!existsSync(path)) return [];
  const reports = [];
  for (const name of readdirSync(path).sort()) {
    if (!name.endsWith(".json")) continue;
    let doc;
    try {
      doc = JSON.parse(readFileSync(join(path, name), "utf8"));
    } catch (error) {
      fail("cannot read " + join(path, name) + ": " + error.message);
    }
    // Two kinds of file land here: publish reports (`name`/`version`) and hardware
    // reports (`pkg`/`ver`). Both are read with the same normalisation so a caller does
    // not have to remember which directory it handed over.
    if (!doc || typeof doc !== "object") {
      fail(join(path, name) + " is not a report (an object is required)");
    }
    const who = doc.name || doc.pkg || "";
    const which = doc.version || doc.ver || "";
    if (!who || !which) {
      fail(
        join(path, name) +
          " is not a report (name/version (publish) or pkg/ver (hardware) are required)"
      );
    }
    doc.label = who + "@" + which;
    if (!doc.integrity && !doc.expected_integrity && !doc.verdict) {
      // A publish report without the digest is still useful (the hardware run can
      // install and exercise the package), but it cannot prove the bytes are the
      // published ones, and that has to be visible rather than silently downgraded.
      doc.digest_missing = true;
    }
    reports.push(doc);
  }
  return reports;
}

function merge(dir, out) {
  const reports = readReports(dir);
  const plan = {};
  for (const doc of reports) {
    const key = doc.name || doc.pkg;
    if (plan[key]) continue;
    plan[key] = doc;
  }
  mkdirSync(dirname(resolve(out)), { recursive: true });
  writeFileSync(resolve(out), JSON.stringify(plan, null, 2) + "\n");
  if (reports.length === 0) {
    console.log("[physical-plan] no publish report found under " + resolve(dir));
    process.exit(0);
  }
  for (const doc of reports) {
    console.log(
      "[physical-plan] " + doc.label +
        " served by " + doc.registry + ": " + (doc.served || "?") +
        (doc.digest_missing ? " (no digest)" : "")
    );
  }
  console.log("[physical-plan] plan: " + resolve(out) + " (" + reports.length + " package(s))");
}

function summary(dir) {
  const reports = readReports(dir);
  if (reports.length === 0) {
    console.log("_no hardware report was produced by this run_");
    return;
  }
  console.log("");
  console.log("| package | verdict | installed | binding | hardware |");
  console.log("| --- | --- | --- | --- | --- |");
  for (const doc of reports) {
    const host = doc.host || {};
    // The ISA string is hundreds of characters of extensions: it belongs in the JSON,
    // where a reader can diff it, not in a table cell.
    const hardware = [host.uname_m, host.model, host.glibc].filter(Boolean).join(" · ");
    const installed = doc.installed_integrity
      ? doc.installed_integrity === doc.expected_integrity
        ? "digest matches"
        : "digest differs"
      : "no digest";
    const verdict = doc.verdict || "?";
    const marker = verdict === "ok" ? "ok" : "**" + verdict + "**";
    console.log(
      "| `" + doc.pkg + "@" + doc.ver + "` | " + marker + " | " + installed + " | " +
        (doc.binding ? "`" + shortPath(doc.binding) + "`" : "-") + " | " + (hardware || "-") + " |"
    );
  }
  const failures = reports.filter((doc) => doc.verdict !== "ok");
  if (failures.length) {
    console.log("");
    console.log("Failures:");
    for (const doc of failures) {
      const detail = (doc.checks || [])
        .filter((check) => check.result !== "ok")
        .map((check) => check.name + ": " + check.detail)
        .join("; ");
      console.log("- `" + doc.pkg + "@" + doc.ver + "`: " + (doc.verdict || "?") + " - " + (detail || "no detail"));
    }
  }
  const degraded = reports.filter(
    (doc) => (doc.elf_check || "").includes("degraded") || (doc.checks || []).some((c) => c.result === "unknown")
  );
  if (degraded.length) {
    console.log("");
    console.log(
      "Environment notes (`file` missing or no digest to compare): " +
        degraded.map((doc) => "`" + doc.pkg + "`").join(", ")
    );
  }
}

function shortPath(value) {
  const parts = String(value).split("/");
  return parts.slice(-3).join("/");
}

function verdict(dir) {
  const reports = readReports(dir);
  if (reports.length === 0) return;
  const failed = reports.filter(
    // "environment" is the machine's problem, not the artifact's: it still fails the
    // workflow (the verification did not happen), but the summary says which it was.
    (doc) => doc.verdict && doc.verdict !== "ok" && doc.verdict !== "skipped"
  );
  if (!failed.length) {
    console.log("[physical-plan] " + reports.length + " package(s) verified");
    return;
  }
  console.error(
    "[physical-plan] " +
      failed.length + " of " + reports.length + " verification(s) failed: " +
      failed.map((doc) => doc.pkg + "@" + doc.ver + "=" + doc.verdict).join(", ")
  );
  process.exit(1);
}

switch (mode) {
  case "--merge": {
    const dir = argv[1];
    const outIndex = argv.indexOf("--out");
    const out = outIndex >= 0 ? argv[outIndex + 1] : "";
    if (!dir || !out) fail("--merge needs a directory and --out <file>");
    merge(dir, out);
    break;
  }
  case "--summary": {
    const dir = argv[1];
    if (!dir) fail("--summary needs a directory");
    summary(dir);
    break;
  }
  case "--verdict": {
    const dir = argv[1];
    if (!dir) fail("--verdict needs a directory");
    verdict(dir);
    break;
  }
  default:
    fail("unknown mode: " + String(mode));
}
