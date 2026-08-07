#!/usr/bin/env node
/**
 * Stage the vendored VOD.RIP Cookie Bridge extension into the packaged app.
 *
 * The bridge is a modified fork of Get cookies.txt LOCALLY (MIT, kairi003),
 * vendored at vendor/cookie-extension/src. Manual install = "Load unpacked"
 * that folder in Chrome/Edge — the only silent-install path on unmanaged
 * machines is the Chrome Web Store, so the release ships the source next to
 * the exe and the README walks users through the 30-second manual step.
 *
 * Output: <dist>/VOD-RIP/cookie-extension/src  (the PyInstaller onedir that
 * the installer and the zip both package from), or the directory given as
 * argv[2]. Graceful: warns and exits 0 when the vendor tree is missing, so
 * a dev build without the fork still packages.
 *
 * Usage: node scripts/stage-cookie-extension.mjs [outDir]
 */
import { cpSync, existsSync, readFileSync, rmSync } from 'node:fs';
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

rmSync(out, { recursive: true, force: true });
cpSync(src, out, { recursive: true });
console.log(`[cookie-extension] staged ${path.relative(root, src)} -> ${path.relative(root, out)}`);
