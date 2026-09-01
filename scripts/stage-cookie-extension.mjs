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
  console.log(`[kick-overlay] staged ${path.relative(root, overlaySrc)} -> ${path.relative(root, overlayOut)}`);
}
console.log(`[cookie-extension] staged ${path.relative(root, src)} -> ${path.relative(root, out)}`);
