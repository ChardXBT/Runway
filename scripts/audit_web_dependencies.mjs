#!/usr/bin/env node
// Full npm audit for apps/web that fails on every finding except a reviewed,
// unexpired, development-only exception recorded in apps/web/audit-exceptions.json.
// The production dependency tree is audited separately with no exceptions.
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "apps", "web");
const lock = JSON.parse(readFileSync(join(webRoot, "package-lock.json"), "utf8"));
const { exceptions } = JSON.parse(readFileSync(join(webRoot, "audit-exceptions.json"), "utf8"));

function runAudit() {
  try {
    return execSync("npm audit --json", {
      cwd: webRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "inherit"],
    });
  } catch (error) {
    // npm audit exits nonzero whenever it reports a finding; the JSON is still on stdout.
    if (typeof error.stdout === "string" && error.stdout.trim()) return error.stdout;
    throw error;
  }
}

const report = JSON.parse(runAudit());
if (report.error) {
  console.error(`npm audit failed: ${report.error.summary ?? JSON.stringify(report.error)}`);
  process.exit(1);
}

const today = new Date().toISOString().slice(0, 10);
const allowed = new Map(exceptions.map((entry) => [entry.advisory, entry]));
const failures = [];
const excepted = new Set();

for (const [name, vulnerability] of Object.entries(report.vulnerabilities ?? {})) {
  for (const node of vulnerability.nodes) {
    if (lock.packages?.[node]?.dev !== true) {
      failures.push(`${name} (${node}) is reachable from production dependencies.`);
    }
  }
  for (const via of vulnerability.via) {
    // String entries mean "depends on another vulnerable package"; that package is checked itself.
    if (typeof via === "string") continue;
    const exception = allowed.get(via.url);
    if (!exception || exception.package !== via.name) {
      failures.push(`${via.name}: ${via.title} (${via.url}) has no reviewed exception.`);
    } else if (exception.expires < today) {
      failures.push(`${via.url} exception expired on ${exception.expires}; review it again.`);
    } else {
      excepted.add(exception);
    }
  }
}

for (const exception of excepted) {
  console.log(
    `Accepted dev-only advisory ${exception.advisory} (${exception.dependencyChain}), ` +
      `reviewed ${exception.reviewed}, expires ${exception.expires}.`,
  );
}
for (const exception of exceptions) {
  if (!excepted.has(exception)) {
    console.log(`Exception ${exception.advisory} no longer matches any finding; remove it.`);
  }
}
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("apps/web audit: no unreviewed findings.");
