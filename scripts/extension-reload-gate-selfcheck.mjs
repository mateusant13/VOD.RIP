#!/usr/bin/env node
/**
 * Self-check for the stable reload-directive gate in
 * vendor/cookie-extension/src/background.js (Status code 10 hardening).
 *
 * Imports the REAL service worker module with stubbed chrome/fetch and
 * drives `checkReloadDirective` through the decision matrix. Run from the
 * repo root:
 *
 *   node --experimental-default-type=module scripts/extension-reload-gate-selfcheck.mjs
 *
 * The gate: a reload directive is honored only after TWO consecutive polls
 * report the same target (persisted in chrome.storage.local so SW idle
 * restarts don't reset the count). One sighting, a changed target, or no
 * directive must never call chrome.runtime.reload().
 */
import assert from 'node:assert/strict';

const MANIFEST_VERSION = '0.8.33';
const storage = new Map();
let reloadCalls = 0;
let statusBody = null; // what /api/extension/status returns
const posted = []; // POST bodies (reload-done confirmations)

globalThis.self = {
  addEventListener() {},
};
globalThis.chrome = {
  cookies: { onChanged: { addListener() {} } },
  tabs: {
    onUpdated: { addListener() {} },
    onActivated: { addListener() {} },
    query: async () => [],
  },
  windows: { onFocusChanged: { addListener() {} } },
  alarms: { create() {}, onAlarm: { addListener() {} } },
  runtime: {
    onInstalled: { addListener() {} },
    onStartup: { addListener() {} },
    onMessage: { addListener() {} },
    getManifest: () => ({ version: MANIFEST_VERSION }),
    reload() {
      reloadCalls += 1;
    },
  },
  storage: {
    local: {
      async get(key) {
        return storage.has(key) ? { [key]: storage.get(key) } : {};
      },
      async set(obj) {
        for (const [k, v] of Object.entries(obj)) storage.set(k, v);
      },
      async remove(key) {
        storage.delete(key);
      },
    },
  },
};
globalThis.fetch = async (url, opts) => {
  if (String(url).endsWith('/api/extension/status')) {
    return { ok: true, json: async () => statusBody };
  }
  if (String(url).endsWith('/api/extension/reload-done')) {
    posted.push(JSON.parse(opts.body));
    return { ok: true, json: async () => ({ ok: true }) };
  }
  throw new Error(`unexpected fetch: ${url}`);
};

const { checkReloadDirective } = await import(
  new URL('../vendor/cookie-extension/src/background.js', import.meta.url)
);

const fresh = () => {
  storage.clear();
  reloadCalls = 0;
  posted.length = 0;
};

// 1. No directive -> no reload, seen cleared.
fresh();
statusBody = { ok: true, version: MANIFEST_VERSION, reloadTo: null };
await checkReloadDirective();
assert.equal(reloadCalls, 0, 'no directive must not reload');
assert.equal(storage.size, 0, 'no directive must clear the seen marker');

// 2. First sighting of a newer target -> record, DO NOT reload.
fresh();
statusBody = { ok: true, version: MANIFEST_VERSION, reloadTo: '0.8.34' };
await checkReloadDirective();
assert.equal(reloadCalls, 0, 'single sighting must not reload');
assert.equal(storage.get('vodrip_reload_target_seen'), '0.8.34', 'target must be recorded');

// 3. Same target on the next poll -> reload exactly once, marker cleared.
await checkReloadDirective();
assert.equal(reloadCalls, 1, 'second consecutive sighting must reload');
assert.equal(storage.size, 0, 'marker must clear after reload');

// 4. Target changed between polls -> re-arm on the new target, no reload.
fresh();
statusBody = { ok: true, version: MANIFEST_VERSION, reloadTo: '0.8.34' };
await checkReloadDirective();
statusBody = { ok: true, version: MANIFEST_VERSION, reloadTo: '0.8.35' };
await checkReloadDirective();
assert.equal(reloadCalls, 0, 'changed target must not reload');
assert.equal(storage.get('vodrip_reload_target_seen'), '0.8.35', 'must re-arm on new target');

// 5. Target equal to the running manifest version -> confirm (reload-done
//    POST), no chrome.runtime.reload().
fresh();
statusBody = { ok: true, version: MANIFEST_VERSION, reloadTo: MANIFEST_VERSION };
await checkReloadDirective();
await new Promise((resolve) => setTimeout(resolve, 10)); // confirmReloadDone is fire-and-forget
assert.equal(reloadCalls, 0, 'matching version must not reload');
assert.equal(posted.length, 1, 'matching version must POST reload-done');
assert.equal(posted[0].version, MANIFEST_VERSION);

// 6. Persistence across SW idle restarts: a sighting survives a fresh SW
//    (the storage stub persists), so the next poll reloads.
fresh();
statusBody = { ok: true, version: MANIFEST_VERSION, reloadTo: '0.8.34' };
await checkReloadDirective(); // SW instance A records the target
statusBody = { ok: true, version: MANIFEST_VERSION, reloadTo: '0.8.34' };
await checkReloadDirective(); // SW instance B (storage persisted) reloads
assert.equal(reloadCalls, 1, 'persisted marker must survive SW restart');

// 7. Fetch failure (backend offline) -> silent no-op, marker untouched.
fresh();
statusBody = { ok: true, version: MANIFEST_VERSION, reloadTo: '0.8.34' };
await checkReloadDirective();
globalThis.fetch = async () => {
  throw new Error('backend offline');
};
await checkReloadDirective();
assert.equal(reloadCalls, 0, 'offline check must not reload');
globalThis.fetch = async (url, opts) => {
  if (String(url).endsWith('/api/extension/status')) {
    return { ok: true, json: async () => statusBody };
  }
  throw new Error(`unexpected fetch: ${url}`);
};

console.log('extension-reload-gate-selfcheck: OK (7 scenarios)');
