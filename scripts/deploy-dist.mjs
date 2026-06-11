/**
 * Copy PyInstaller output beside the project root for local runs.
 * Windows: VOD-RIP.EXE + _internal/
 * macOS:   VOD.RIP.app
 * Linux:   VOD-RIP + _internal/
 */
import { chmodSync, cpSync, existsSync, rmSync } from 'fs';
import { dirname, join } from 'path';
import { platform } from 'os';
import { fileURLToPath } from 'url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const os = platform();
const distDir = join(root, 'dist');
const folderDist = join(distDir, 'VOD-RIP');
const macApp = join(distDir, 'VOD.RIP.app');

function copyIcon() {
  // Windows / Linux: copy .ico to root
  const icoCandidates = [
    join(folderDist, 'icon.ico'),
    join(root, 'assets', 'icon.ico'),
  ];
  for (const src of icoCandidates) {
    if (existsSync(src)) {
      cpSync(src, join(root, 'icon.ico'));
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
    cpSync(winExe, join(root, 'VOD-RIP.EXE'));
    console.log('Deployed VOD-RIP.EXE to project root');
  } else if (existsSync(unixExe)) {
    const dest = join(root, 'VOD-RIP');
    cpSync(unixExe, dest);
    chmodSync(dest, 0o755);
    console.log('Deployed VOD-RIP binary to project root');
  }

  if (existsSync(internalSrc)) {
    const internalDest = join(root, '_internal');
    rmSync(internalDest, { recursive: true, force: true });
    cpSync(internalSrc, internalDest, { recursive: true });
    console.log('Deployed _internal/ to project root');
  }

  copyIcon();
}
