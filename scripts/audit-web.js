// npm audit has no native ignore-file, so this wrapper encodes known-unfixable
// dev-only findings and fails on anything new.
// Allowlisted:
// - GHSA-vfj7-8cjw-p6xm (braces <=3.0.3, Patched: None) via eslint-config-next
//   chain (lint-time only).
// - Vitest 3 dev-only chain (test runner only, never ships — prod audit
//   --omit=dev is clean, standalone bundle excludes devDeps):
//   GHSA-82fw-gwwq-j7x9 + GHSA-qxg6-89mg-rqv8 (@vitest/mocker),
//   GHSA-5gmw-xhrv-c9v3 + GHSA-85c8-ppgw-ccpr (tinypool worker pool).
//   Re-check on every vitest bump; drop when upstream ships a fix on the
//   installed major. NOTE 2026-10-28: @vitest/mocker's fix exists only in
//   vitest 5 (major bump, out of scope) — GHSA-qxg6-89mg-rqv8 stays
//   allowlisted until the vitest 5 migration.
const { execSync } = require("node:child_process");

const ALLOWED_URLS = new Set([
  "https://github.com/advisories/GHSA-vfj7-8cjw-p6xm",
  "https://github.com/advisories/GHSA-82fw-gwwq-j7x9",
  "https://github.com/advisories/GHSA-qxg6-89mg-rqv8",
  "https://github.com/advisories/GHSA-5gmw-xhrv-c9v3",
  "https://github.com/advisories/GHSA-85c8-ppgw-ccpr",
]);
// Transitive parents have `via: ["micromatch"]` string refs without their own
// URL, so allow the whole chain only when every advisory URL in the report
// is allowlisted.
const ALLOWED_CHAIN = new Set([
  "braces",
  "micromatch",
  "fast-glob",
  "@next/eslint-plugin-next",
  "eslint-config-next",
  "@vitest/mocker",
  "tinypool",
  "vitest",
]);

function runAudit(args) {
  try {
    const out = execSync(`npm audit --json ${args}`, {
      cwd: "apps/web",
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    return JSON.parse(out);
  } catch (err) {
    // npm exits 1 when vulns found, but stdout still holds JSON.
    const out = err.stdout?.toString() ?? "";
    if (!out) throw err;
    return JSON.parse(out);
  }
}

function advisoryUrls(entry) {
  const urls = new Set();
  for (const via of entry.via ?? []) {
    if (typeof via === "object" && via.url) urls.add(via.url);
  }
  return urls;
}

const report = runAudit("");
const vulns = report.vulnerabilities ?? {};
const names = Object.keys(vulns);
const allUrls = new Set();
for (const name of names) {
  for (const u of advisoryUrls(vulns[name])) allUrls.add(u);
}
const onlyAllowedAdvisories = allUrls.size > 0 && [...allUrls].every((u) => ALLOWED_URLS.has(u));
const blocking = names.filter((name) => {
  if (onlyAllowedAdvisories && ALLOWED_CHAIN.has(name)) return false;
  const urls = advisoryUrls(vulns[name]);
  // Allow only entries whose every advisory is allowlisted.
  return !(urls.size > 0 && [...urls].every((u) => ALLOWED_URLS.has(u)));
});

if (blocking.length === 0) {
  console.log(
    `audit-web: OK (${names.length} finding(s), all allowlisted GHSA-vfj7-8cjw-p6xm). Prod audit (--omit=dev) is clean.`,
  );
  process.exit(0);
}

console.error(`audit-web: FAIL — ${blocking.length} non-allowlisted finding(s): ${blocking.join(", ")}`);
for (const name of blocking) {
  const v = vulns[name];
  console.error(`- ${name} [${v.severity}] via=${JSON.stringify(v.via?.map((x) => (typeof x === "string" ? x : x.url)))}`);
}
console.error("If upstream shipped braces>3.0.3, remove GHSA-vfj7-8cjw-p6xm from ALLOWED_URLS in scripts/audit-web.js.");
process.exit(1);
