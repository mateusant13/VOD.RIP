/**
 * Stage the ASR runtime (VOD-RIP-ASR.exe + _internal/) under a versioned
 * runtime directory so the base app can discover it in a stable, version-keyed
 * location and the release workflow can zip it as a standalone artifact.
 *
 * Layout produced:
 *   dist/runtimes/asr/<version>/VOD-RIP-ASR.exe
 *   dist/runtimes/asr/<version>/_internal/...
 *
 * The version is read from backend/services/_version.py (single source of
 * truth) so the directory name tracks exactly what the base app reports.
 *
 * Run after `pyinstaller vod-rip-asr.spec --clean --noconfirm`.
 */
import { cpSync, existsSync, mkdirSync, rmSync, readFileSync, chmodSync } from 'fs';
import { dirname, join } from 'path';
import { platform } from 'os';
import { fileURLToPath } from 'url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const os = platform();
const distDir = join(root, 'dist');
const asrBuild = join(distDir, 'VOD-RIP-ASR');

if (os !== 'win32') {
  throw new Error('ASR runtime is only staged for Windows in this workflow.');
}

const exe = join(asrBuild, 'VOD-RIP-ASR.exe');
const internalSrc = join(asrBuild, '_internal');
if (!existsSync(exe)) {
  console.error('Missing ASR build output in', asrBuild);
  console.error('Run: pyinstaller vod-rip-asr.spec --clean --noconfirm');
  process.exit(1);
}

// Read the canonical version from backend/services/_version.py.
const versionPy = join(root, 'backend', 'services', '_version.py');
let version = '1.0.0';
if (existsSync(versionPy)) {
  const m = readFileSync(versionPy, 'utf-8').match(/__version__\s*=\s*["']([^"']+)["']/);
  if (m) version = m[1];
}

const runtimeDir = join(distDir, 'runtimes', 'asr', version);
rmSync(runtimeDir, { recursive: true, force: true });
mkdirSync(runtimeDir, { recursive: true });

cpSync(exe, join(runtimeDir, 'VOD-RIP-ASR.exe'));
if (existsSync(internalSrc)) {
  cpSync(internalSrc, join(runtimeDir, '_internal'), { recursive: true });
}

// Keep the icon beside the exe, mirroring the base deploy layout.
const iconSrc = join(asrBuild, 'icon.ico');
if (existsSync(iconSrc)) {
  cpSync(iconSrc, join(runtimeDir, 'icon.ico'));
}

console.log(`Staged ASR runtime v${version} -> ${runtimeDir}`);
