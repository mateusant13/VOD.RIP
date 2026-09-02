#!/usr/bin/env node
/**
 * Stage the vendored VOD.RIP Cookie Bridge extension into the packaged app.
 *
 * The bridge is a modified fork of Get cookies.txt LOCALLY (MIT, kairi003),
 * vendored at vendor/cookie-extension/src. The same build stages vendor/kick-overlay
 * beside it so the silent installer can load both extensions in one browser
 * profile. Manual install remains available as "Load unpacked".
 *
 * Output: <dist>/VOD-RIP/cookie-extension/src and <dist>/VOD-RIP/kick-overlay
 * (the PyInstaller onedir that the installer and the zip package), or the
 * cookie source directory given as argv[2]. Graceful: warns and exits 0 when
 * the cookie vendor tree is missing, so a dev build without the fork still
 * packages.
 *
 * Usage: node scripts/stage-cookie-extension.mjs [outDir]
 */
import { cpSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const src = path.join(root, 'vendor', 'cookie-extension', 'src');
const out = path.resolve(
  process.argv[2] ?? path.join(root, 'dist', 'VOD-RIP', 'cookie-extension', 'src'),
);

/**
 * Post-copy integrity check: every file a staged extension tree actually
 * references must be present on disk. Catches a partial cpSync (Windows
 * sharing violation retried to success mid-tree, or a vendored asset that
 * was never checked in) that would otherwise ship an extension whose
 * manifest loads but whose runtime files are missing — the kick-overlay's
 * player.html loads its engine (`ivs/index.js`, `hls/hls.min.js`,
 * `player-bridge.js`) via <script src> outside the manifest, so those are
 * checked here, not by Chrome.
 * Throws so the build fails loudly instead of packaging a black player.
 */
function assertTreeComplete(treeRoot, { extraRefs = [] } = {}) {
  const missing = [];
  const manifestPath = path.join(treeRoot, 'manifest.json');
  let manifest = null;
  try {
    manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  } catch {
    missing.push('manifest.json');
    manifest = null;
  }
  const refs = [];
  if (manifest) {
    if (manifest.background) {
      const sw = manifest.background.service_worker || manifest.background.scripts;
      (Array.isArray(sw) ? sw : [sw]).filter(Boolean).forEach((r) => refs.push(r));
    }
    (manifest.content_scripts || []).forEach((cs) => (cs.js || []).forEach((r) => refs.push(r)));
    if (manifest.action) {
      if (manifest.action.default_popup) refs.push(manifest.action.default_popup);
      if (manifest.action.default_icon) Object.values(manifest.action.default_icon).forEach((r) => refs.push(r));
    }
    if (manifest.icons) Object.values(manifest.icons).forEach((r) => refs.push(r));
  }
  refs.push(...extraRefs);
  for (const rel of refs) {
    if (rel.startsWith('/')) { // absolute within the extension package
      if (!existsSync(path.join(treeRoot, rel.slice(1)))) missing.push(rel);
      continue;
    }
    if (!existsSync(path.join(treeRoot, rel))) missing.push(rel);
  }
  if (missing.length) {
    throw new Error(
      `[stage] incomplete extension tree at ${treeRoot} — missing: ${missing.join(', ')}`,
    );
  }
}

// --- guard: this must be OUR fork, not upstream drift --------------------
const manifest = path.join(src, 'manifest.json');
const bridgeModule = path.join(src, 'modules', 'cookie_bridge.mjs');
const background = path.join(src, 'background.js');
if (!existsSync(manifest) || !existsSync(bridgeModule) || !existsSync(background)) {
  console.warn('[cookie-extension] vendor tree missing/incomplete — skipping stage');
  process.exit(0);
}
const bg = readFileSync(background, 'utf8');
if (!bg.includes('cookie_bridge')) {
  console.warn('[cookie-extension] background.js does not import the bridge — skipping stage');
  process.exit(0);
}

// Overwrite in place, NEVER delete: the user may have this exact folder
// loaded unpacked in Chrome, and a delete-then-copy window makes a
// concurrent SW registration re-fetch fail with "Service worker
// registration failed. Status code: 10" (kErrorNetwork) — the SW dies
// until a manual reload (Chrome gives up after ~3 backoff retries).
// cpSync(force) overwrites each file; files removed from src linger but
// are never referenced by the manifest, so they are inert.
// ponytail: if the folder ever needs pruning, stage into out.tmp and
// swap directories instead of deleting in place.
let lastErr;
for (let attempt = 0; attempt < 3; attempt += 1) {
  try {
    cpSync(src, out, { recursive: true, force: true });
    lastErr = null;
    break;
  } catch (err) {
    // Transient Windows sharing violation if a browser holds a file open
    // mid-read; retry before failing the build.
    lastErr = err;
    await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)));
  }
}
if (lastErr) throw lastErr;
assertTreeComplete(out);

// The silent installer also loads the Kick Overlay, so ship both unpacked
// sources beside the frozen executable.
const isPackagedLayout = path.basename(out) === 'src' && path.basename(path.dirname(out)) === 'cookie-extension';
const appOut = isPackagedLayout ? path.resolve(out, '..', '..') : out;
const installScript = path.join(root, 'backend', 'scripts', 'cookie_extension_auto_install.ps1');
const installOut = path.join(appOut, 'scripts', 'cookie_extension_auto_install.ps1');
if (existsSync(installScript)) {
  mkdirSync(path.dirname(installOut), { recursive: true });
  cpSync(installScript, installOut, { force: true });
}
const overlaySrc = path.join(root, 'vendor', 'kick-overlay');
const overlayOut = path.join(appOut, 'kick-overlay');
if (existsSync(path.join(overlaySrc, 'manifest.json')) && existsSync(path.join(overlaySrc, 'content.js'))) {
  cpSync(overlaySrc, overlayOut, { recursive: true, force: true });
  // player.html loads its engine via <script src> OUTSIDE the manifest —
  // verify those too so a partial copy can never ship a black player.
  assertTreeComplete(overlayOut, {
    extraRefs: [
      'player.html',
      'player-bridge.js',
      'ivs/index.js',
      'hls/hls.min.js',
      'background.js',
      'popup.html',
    ],
  });
  console.log(`[kick-overlay] staged ${path.relative(root, overlaySrc)} -> ${path.relative(root, overlayOut)}`);
}
assertTreeComplete(src);
console.log(`[cookie-extension] staged ${path.relative(root, src)} -> ${path.relative(root, out)}`);
