#!/usr/bin/env node
/**
 * The API is never left down.
 *
 * Measured 2026-10-12: dev-all.mjs reset restartAttempts ONLY inside
 * reloadApi(), so after three crashes - or three hang-kills, which arrive as
 * an exit CODE on Windows rather than a signal - every branch fell through to
 * shutdown(1) and the whole supervisor exited. The API then stayed down until
 * a human noticed. That is the reported "o backend sempre cai, nunca ta on".
 *
 * This is a SOURCE law, not a behaviour test: it fails if a give-up path is
 * reintroduced. It carries its own red arm (below) so it cannot pass vacuously
 * - a check that matches nothing looks exactly like a check that passes.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "dev-all.mjs"), "utf8");

let failed = 0;
function check(name, ok, detail = "") {
  if (ok) {
    console.log(`  ok   ${name}`);
  } else {
    failed += 1;
    console.error(`  FAIL ${name}${detail ? " - " + detail : ""}`);
  }
}

// The predicates, as functions of the source text, so the red arm can run the
// very same logic against the old version.
const noGiveUp = (s) => !/restartAttempts\s*<\s*3/.test(s);
// Extract the brace-balanced block that follows `if (label === "api")`, so the
// check is about the api BRANCH and not about "shutdown(1) somewhere nearby".
// The first version of this used a 200-character window and flagged the
// legitimate else-branch - a law that fails on correct code proves nothing.
function apiBranchBlocks(s) {
  const blocks = [];
  const needle = 'if (label === "api")';
  let i = s.indexOf(needle);
  while (i !== -1) {
    const open = s.indexOf("{", i + needle.length);
    if (open === -1) break;
    let depth = 0;
    let j = open;
    for (; j < s.length; j += 1) {
      if (s[j] === "{") depth += 1;
      else if (s[j] === "}") {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    blocks.push(s.slice(open, j + 1));
    i = s.indexOf(needle, j);
  }
  return blocks;
}
const apiNeverShutsDown = (s) => {
  const blocks = apiBranchBlocks(s);
  if (blocks.length === 0) return false; // no api branch at all is a failure
  return blocks.every((b) => !/shutdown\(1\)/.test(b));
};
const hasNeverGiveUpRestart = (s) =>
  /function scheduleApiRestart\(/.test(s) &&
  /never giving up/.test(s) &&
  /API_RESTART_MAX_DELAY_S/.test(s);
const resetsAfterHealth = (s) =>
  /API_HEALTHY_RESET_MS/.test(s) && /restartAttempts = 0/.test(s) &&
  /onHealthy/.test(s);
const revivesDeadChild = (s) =>
  /dead and :\$\{apiPort\} unserved/.test(s);
const hasAuthoritativeWatchdog = (s) =>
  /function startAlwaysOnWatchdog\(/.test(s) &&
  /startAlwaysOnWatchdog\(\);/.test(s) &&
  /not answering on :\$\{apiPort\}/.test(s);

console.log("dev-all is always-on:");
check("no restart-attempt cap remains", noGiveUp(src));
check("the api branch never calls shutdown(1)", apiNeverShutsDown(src));
check("scheduleApiRestart exists and never gives up", hasNeverGiveUpRestart(src));
check("a sustained-healthy API resets the budget", resetsAfterHealth(src));
check("a dead child with an unserved port is revived", revivesDeadChild(src));
check("an authoritative watchdog runs regardless of how the API died", hasAuthoritativeWatchdog(src));

// RED ARM: the old version must FAIL these checks, or they prove nothing.
const OLD = [
  "let restartAttempts = 0;",
  "function start(label) {",
  "  child.on(\"exit\", async (code, signal) => {",
  "    if (label === \"api\" && restartAttempts < 3) {",
  "      restartAttempts += 1;",
  "    } else if (label === \"api\") {",
  "      shutdown(1);",
  "    }",
  "  });",
  "}",
].join("\n");
console.log("red arm (the pre-fix source must fail):");
check("old: cap detected", !noGiveUp(OLD));
check("old: api shutdown detected", !apiNeverShutsDown(OLD));
check("old: no never-give-up restart", !hasNeverGiveUpRestart(OLD));
check("old: no health reset", !resetsAfterHealth(OLD));
check("old: no dead-child revive", !revivesDeadChild(OLD));
check("old: no authoritative watchdog", !hasAuthoritativeWatchdog(OLD));

if (failed) {
  console.error(`\n${failed} check(s) failed`);
  process.exit(1);
}
console.log("\nalways-on self-check OK");
