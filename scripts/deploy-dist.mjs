/**
 * Copy PyInstaller output for local runs.
 * Windows: VOD-RIP.EXE + _internal/
 * macOS:   VOD.RIP.app
 * Linux:   VOD-RIP + _internal/
 *
 * WINDOWS TARGET IS H:, NOT THE REPO (C:). The repo copy of this layout is
 * ~1.2 GB (VOD-RIP.EXE + _internal/), so on Windows the root-level deploy is
 * staged under the H: build root instead:
 *
 *   %VODRIP_BUILD_ROOT%\_deploy\VOD-RIP.EXE
 *   %VODRIP_BUILD_ROOT%\_deploy\_internal\...
 *   %VODRIP_BUILD_ROOT%\_deploy\kick-overlay\
 *   %VODRIP_BUILD_ROOT%\_deploy\scripts\cookie_extension_auto_install.ps1
 *   %VODRIP_BUILD_ROOT%\_deploy\icon.ico
 *
 * VODRIP_BUILD_ROOT is set by scripts/build-install.ps1 (default
 * H:\VOD.RIP-build) so the frozen app is launched from H: with the exact same
 * onedir layout the installer produces. The frozen backend still resolves its
 * assets beside the EXE, so the layout - not the absolute root - is what
 * matters. macOS/Linux keep the historical in-repo layout.
 */
import { chmodSync, cpSync, existsSync, mkdirSync, rmSync } from 'fs';
import { dirname, join } from 'path';
import { platform } from 'os';
import { fileURLToPath } from 'url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const os = platform();
const isWindows = os === 'win32';
const distDir = join(root, 'dist');
const folderDist = join(distDir, 'VOD-RIP');
const macApp = join(distDir, 'VOD.RIP.app');

// Where the root-level deploy lands. Windows -> H: build root (never C:);
// macOS/Linux -> the repo root, as before. Env override wins in every case.
const buildRoot = process.env.VODRIP_BUILD_ROOT || (isWindows ? 'H:\\VOD.RIP-build' : root);
const deployRoot = isWindows ? join(buildRoot, '_deploy') : root;

function copyIcon() {
  // Windows / Linux: copy .ico to the deploy root
  const icoCandidates = [
    join(folderDist, 'icon.ico'),
    join(root, 'assets', 'icon.ico'),
  ];
  for (const src of icoCandidates) {
    if (existsSync(src)) {
      cpSync(src, join(deployRoot, 'icon.ico'));
      break;
    }
  }
  // macOS: copy .icns into .app bundle Resources
  if (os === 'darwin') {
    const icnsCandidates = [
      join(root, 'assets', 'icon.icns'),
      join(folderDist, 'icon.icns'),
    ];
    const appResources = join(root, 'VOD.RIP.app', 'Contents', 'Resources');
    for (const src of icnsCandidates) {
      if (existsSync(src)) {
        if (existsSync(appResources)) {
          cpSync(src, join(appResources, 'icon.icns'));
        }
        break;
      }
    }
    // Also copy .ico in case PyWebView on macOS needs it
    for (const src of icoCandidates) {
      if (existsSync(src)) {
        if (existsSync(appResources)) {
          cpSync(src, join(appResources, 'icon.ico'));
        }
        break;
      }
    }
  }
}

if (os === 'darwin' && existsSync(macApp)) {
  const dest = join(root, 'VOD.RIP.app');
  rmSync(dest, { recursive: true, force: true });
  cpSync(macApp, dest, { recursive: true });
  console.log('Deployed VOD.RIP.app to project root');
} else {
  const winExe = join(folderDist, 'VOD-RIP.exe');
  const unixExe = join(folderDist, 'VOD-RIP');
  const internalSrc = join(folderDist, '_internal');

  if (!existsSync(winExe) && !existsSync(unixExe)) {
    console.error('Missing build output in', folderDist);
    console.error('Run: npm run build-dist');
    process.exit(1);
  }

  if (os === 'win32' && existsSync(winExe)) {
    mkdirSync(deployRoot, { recursive: true });
    cpSync(winExe, join(deployRoot, 'VOD-RIP.EXE'));
    console.log('Deployed VOD-RIP.EXE to', deployRoot);
  } else if (existsSync(unixExe)) {
    const dest = join(deployRoot, 'VOD-RIP');
    cpSync(unixExe, dest);
    chmodSync(dest, 0o755);
    console.log('Deployed VOD-RIP binary to', deployRoot);
  }

  if (existsSync(internalSrc)) {
    const internalDest = join(deployRoot, '_internal');
    mkdirSync(deployRoot, { recursive: true });
    rmSync(internalDest, { recursive: true, force: true });
    cpSync(internalSrc, internalDest, { recursive: true });
    console.log('Deployed _internal/ to', deployRoot);
  }
  // The frozen backend resolves these assets beside the executable. Keep the
  // deployed layout equivalent to the onedir install layout - only the root it
  // sits under moved (repo -> H: build root on Windows).
  const autoInstallSrc = join(root, 'backend', 'scripts', 'cookie_extension_auto_install.ps1');
  if (existsSync(autoInstallSrc)) {
    const autoInstallDest = join(deployRoot, 'scripts', 'cookie_extension_auto_install.ps1');
    mkdirSync(dirname(autoInstallDest), { recursive: true });
    cpSync(autoInstallSrc, autoInstallDest, { force: true });
    console.log('Deployed cookie auto-install driver');
  }
  const overlaySrc = join(root, 'vendor', 'kick-overlay');
  const overlayDest = join(deployRoot, 'kick-overlay');
  if (existsSync(join(overlaySrc, 'manifest.json'))) {
    rmSync(overlayDest, { recursive: true, force: true });
    cpSync(overlaySrc, overlayDest, { recursive: true, force: true });
    console.log('Deployed Kick Overlay package');
  }

  copyIcon();
}
