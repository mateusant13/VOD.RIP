#!/usr/bin/env node
/**
 * check-node-install.mjs — fail loudly when node_modules is INCOMPLETE.
 *
 * Why this exists
 * ---------------
 * On 2026-10-05 the shared tree at the repo root was left with:
 *   - node_modules/.bin          deleted entirely  (~63 shims, incl. tsc + vitest)
 *   - node_modules/.package-lock.json deleted    (npm's own install record)
 *   - 32 real non-optional packages absent       (the whole @babel/* toolchain
 *                                                + the Tailwind v4 CSS deps)
 * A lane then reported "tsc exit 0" against that tree. It was a false green:
 * `npx tsc` had resolved a TypeScript from somewhere other than this install.
 *
 * `npx <tool>` is NOT a valid check. When the local shim is missing, npx is
 * free to fetch from the network or fall back to a global install, so it can
 * exit 0 while the project tree is broken. Always run the local binary:
 *     node node_modules/typescript/bin/tsc --noEmit
 *
 * This script has ZERO dependencies on purpose — it must be able to diagnose a
 * tree whose dependencies are the thing that is broken. Run it with bare node.
 *
 * Usage
 *   node scripts/check-node-install.mjs            # quiet, exit 1 if incomplete
 *   node scripts/check-node-install.mjs --json     # machine-readable
 *   node scripts/check-node-install.mjs --min-files 8000   # raise the floor
 *
 * Exit codes
 *   0  install looks complete
 *   1  install INCOMPLETE (missing .bin, missing packages, or count too low)
 *   2  could not evaluate (no lockfile / no node_modules at all)
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const NM = path.join(ROOT, "node_modules");
const LOCK = path.join(ROOT, "package-lock.json");

const argv = process.argv.slice(2);
const AS_JSON = argv.includes("--json");
const minIdx = argv.indexOf("--min-files");
const MIN_FILES = minIdx !== -1 ? Number(argv[minIdx + 1]) : 8000;

/** Shims that must exist for the frontend gates to run against THIS tree. */
const REQUIRED_BINS = ["tsc", "vitest", "vite"];

const problems = [];
const notes = [];

/** npm in-flight staging leftovers from an interrupted install. Inert. */
const STAGING = new Set();

function out(...a) {
  if (!AS_JSON) console.log(...a);
}

/* ------------------------------------------------------------------ *
 * Which lockfile entries are supposed to exist on THIS machine?
 * Optional deps pinned to another platform (fsevents on macOS,
 * lightningcss-linux-*, @rollup/rollup-darwin-*) are correctly absent on
 * Windows and must NOT be reported as missing.
 * ------------------------------------------------------------------ */
function expectedPackages(lock) {
  const pkgs = lock.packages ?? {};
  const want = new Set();
  for (const [key, entry] of Object.entries(pkgs)) {
    if (key === "") continue; // the root project entry
    const m = /^node_modules\/(?:@[^/]+\/)?[^/]+$/.exec(key);
    if (!m) continue; // nested / transitive-path entries checked separately
    const name = key.slice("node_modules/".length);

    const os = entry.os;
    const cpu = entry.cpu;
    if (os && !os.some((o) => o === process.platform || o === "any")) continue;
    if (cpu && !cpu.some((c) => c === process.arch || c === "any")) continue;

    want.add(name);
  }
  return want;
}

function actualPackages() {
  const have = new Set();
  const staging = STAGING;
  for (const name of fs.readdirSync(NM)) {
    if (name.startsWith(".")) continue;
    const p = path.join(NM, name);
    if (!fs.statSync(p).isDirectory()) continue;
    if (name.startsWith("@")) {
      for (const sub of fs.readdirSync(p)) {
        // A dot-prefixed sibling is npm's in-flight staging dir
        // (e.g. @rollup/.rollup-win32-x64-msvc-PMEzLQM2), left behind when an
        // install was interrupted. It is a temp artifact, NOT a package.
        if (sub.startsWith(".")) { staging.add(`${name}/${sub}`); continue; }
        if (fs.statSync(path.join(p, sub)).isDirectory()) have.add(`${name}/${sub}`);
      }
    } else {
      have.add(name);
    }
  }
  return have;
}

function countFiles(dir) {
  let n = 0;
  const stack = [dir];
  while (stack.length) {
    const d = stack.pop();
    let entries;
    try {
      entries = fs.readdirSync(d, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const e of entries) {
      if (e.isDirectory()) stack.push(path.join(d, e.name));
      else n++;
    }
  }
  return n;
}

/* ----------------------------- checks ----------------------------- */

if (!fs.existsSync(LOCK)) {
  console.error(`FAIL: no lockfile at ${LOCK} — cannot judge the install.`);
  process.exit(2);
}
if (!fs.existsSync(NM)) {
  console.error(`FAIL: no node_modules at ${NM}. Run: npm ci`);
  process.exit(2);
}

const lock = JSON.parse(fs.readFileSync(LOCK, "utf8"));

// 1. .bin must exist and carry the gate shims.
const binDir = path.join(NM, ".bin");
if (!fs.existsSync(binDir)) {
  problems.push("node_modules/.bin is MISSING — npx will silently fall back to a network/global tsc.");
} else {
  for (const b of REQUIRED_BINS) {
    if (!fs.existsSync(path.join(binDir, `${b}.cmd`))) {
      problems.push(`node_modules/.bin/${b}.cmd is missing.`);
    }
  }
  notes.push(`.bin present (${fs.readdirSync(binDir).length} entries)`);
}

// 2. npm's own install record. Without it we cannot tell "pruned" from
//    "half-finished", which is exactly how this went unnoticed.
if (!fs.existsSync(path.join(NM, ".package-lock.json"))) {
  problems.push(
    "node_modules/.package-lock.json is MISSING — npm's record of what it installed. " +
      "The tree cannot be validated; re-run `npm ci`."
  );
}

// 3. The real signal: every package the lockfile expects for this platform.
const want = expectedPackages(lock);
const have = actualPackages();
const missing = [...want].filter((n) => !have.has(n)).sort();
const extra = [...have].filter((n) => !want.has(n)).sort();
if (missing.length) {
  problems.push(`${missing.length} package(s) the lockfile expects are MISSING: ${missing.slice(0, 12).join(", ")}${missing.length > 12 ? " …" : ""}`);
} else {
  notes.push(`all ${want.size} expected packages present`);
}
if (extra.length) {
  // Not fatal — but it is how a half-pruned tree usually announces itself.
  notes.push(`${extra.length} package dir(s) not in the lockfile (leftover from another install): ${extra.slice(0, 8).join(", ")}${extra.length > 8 ? " …" : ""}`);
}
if (STAGING.size) {
  // Inert residue of an interrupted `npm install`. Harmless, and `npm ci`
  // clears it. Deliberately NOT a failure — it is not a broken package.
  notes.push(
    `${STAGING.size} npm staging leftover(s) from an interrupted install (inert, cleared by npm ci): ` +
      `${[...STAGING].slice(0, 4).join(", ")}${STAGING.size > 4 ? " …" : ""}`
  );
}

// 4. Hollow packages: directory survived, contents did not.
const hollow = [...have].filter(
  (n) => !fs.existsSync(path.join(NM, ...n.split("/"), "package.json"))
);
if (hollow.length) {
  problems.push(`${hollow.length} package dir(s) have no package.json (hollow): ${hollow.slice(0, 10).join(", ")}`);
}

// 5. Crude floor on total size — catches a prune that took whole subtrees.
const files = countFiles(NM);
if (files < MIN_FILES) {
  problems.push(`file count is SHORT: ${files} < floor ${MIN_FILES}. A prune removed real packages.`);
} else {
  notes.push(`file count ${files} (floor ${MIN_FILES})`);
}

/* ------------------------------ report ---------------------------- */

if (AS_JSON) {
  console.log(
    JSON.stringify(
      { ok: problems.length === 0, files, expectedPackages: want.size, actualPackages: have.size, missing, extra, hollow, staging: [...STAGING], problems, notes },
      null,
      2
    )
  );
} else {
  out(`node_modules: ${files} files, ${have.size} top-level packages (lock expects ${want.size})`);
  for (const n of notes) out(`  ok   ${n}`);
  for (const p of problems) out(`  FAIL ${p}`);
  if (problems.length) {
    out("");
    out("Install is INCOMPLETE. Repair with a full, lockfile-exact install:");
    out("    npm ci");
    out("Do NOT trust `npx tsc` / `npx vitest` here — they will pass anyway.");
  }
}

process.exit(problems.length ? 1 : 0);
