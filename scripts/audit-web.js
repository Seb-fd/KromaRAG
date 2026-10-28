// npm audit has no native ignore-file, so this wrapper encodes known-unfixable
// dev-only findings and fails on anything new. Currently no allowlisted
// advisories — raw `npm audit` is clean (2026-10-28: vitest 5 migration fixed
// the @vitest/mocker + tinypool chain; the braces chain fixed upstream).
// Re-add here only with a GHSA URL + justification, never silently.
const { execSync } = require("node:child_process");

const ALLOWED_URLS = new Set([]);
// Transitive parents have `via: ["micromatch"]` string refs without their own
// URL, so allow the whole chain only when every advisory URL in the report
// is allowlisted.
const ALLOWED_CHAIN = new Set([]);

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
    names.length === 0
      ? "audit-web: OK (0 findings). Prod audit (--omit=dev) is clean."
      : `audit-web: OK (${names.length} finding(s), all allowlisted). Prod audit (--omit=dev) is clean.`,
  );
  process.exit(0);
}

console.error(`audit-web: FAIL — ${blocking.length} non-allowlisted finding(s): ${blocking.join(", ")}`);
for (const name of blocking) {
  const v = vulns[name];
  console.error(`- ${name} [${v.severity}] via=${JSON.stringify(v.via?.map((x) => (typeof x === "string" ? x : x.url)))}`);
}
console.error("If this is a dev-only chain with no upstream fix, add its GHSA URL(s) to ALLOWED_URLS in scripts/audit-web.js with justification — never silently.");
process.exit(1);
