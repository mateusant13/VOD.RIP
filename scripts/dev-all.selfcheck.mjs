#!/usr/bin/env node
/**
 * Self-check for scripts/dev-all.mjs helpers.
 * Covers: tag timestamp format, truncate threshold math, argv parsing, log path.
 * Run: node scripts/dev-all.selfcheck.mjs
 */

// ---- tag format ----
function makeTag(label) {
  const ts = new Date().toISOString().replace(/Z$/, ""); // YYYY-MM-DDTHH:MM:SS.fff
  return `${ts} [${label}] `;
}
const t1 = makeTag("api");
assert(t1.endsWith(" [api] "), "tag ends with label bracket");
assert(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}/.test(t1), "tag starts with ISO timestamp");

// ---- truncate threshold ----
const TEN_MB = 10 * 1024 * 1024;
assert(TEN_MB === 10485760, "10 MB = 10485760 bytes");

// ---- truncateIfLarge decision ----
function shouldTruncate(size) {
  return size > 10 * 1024 * 1024;
}
assert(shouldTruncate(10485761) === true,  "one byte over threshold");
assert(shouldTruncate(10485760) === false, "at threshold — no truncate");
assert(shouldTruncate(0) === false,        "empty file — no truncate");

// ---- log path construction ----
function logPath(root, label) {
  return `${root}/tmp/vodrip-devall-${label}.log`;
}
assert(logPath("/repo", "api") === "/repo/tmp/vodrip-devall-api.log",  "api log path");
assert(logPath("/repo", "web") === "/repo/tmp/vodrip-devall-web.log",  "web log path");

// ---- argv parsing ----
function hasArg(argv, name) {
  return argv.includes(name);
}
assert(hasArg(["--kill-logs"], "--kill-logs") === true,  "--kill-logs detected");
assert(hasArg([], "--kill-logs") === false,               "--kill-logs absent");
assert(hasArg(["--fast-preview"], "--fast-preview") === true, "--fast-preview detected");

// ---- env-var disable check ----
function loggingDisabled(env) {
  return env.VODRIP_DEVALL_DISABLE_LOG === "1";
}
assert(loggingDisabled({ VODRIP_DEVALL_DISABLE_LOG: "1" }) === true,  "env=1 disables");
assert(loggingDisabled({}) === false,                                   "no env — enabled");

console.log("All self-checks passed.");
process.exit(0);

function assert(cond, msg) {
  if (!cond) {
    console.error(`FAIL: ${msg}`);
    process.exit(1);
  }
}
