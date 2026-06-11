/**
 * Copy the PyInstaller output to the project root so VOD-RIP.EXE lives
 * alongside _internal/ (required one-folder layout).
 */
import { cpSync, existsSync, rmSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const distName = 'VOD-RIP';
const exeName = 'VOD-RIP.exe';
const rootExeName = 'VOD-RIP.EXE';
const src = join(root, 'dist', distName);
const exeSrc = join(src, exeName);
const internalSrc = join(src, '_internal');

if (!existsSync(exeSrc)) {
  console.error('Missing build output:', exeSrc);
  console.error('Run: npm run build-dist');
  process.exit(1);
}

cpSync(exeSrc, join(root, rootExeName));
if (existsSync(internalSrc)) {
  rmSync(join(root, '_internal'), { recursive: true, force: true });
  cpSync(internalSrc, join(root, '_internal'), { recursive: true });
}
const iconSrc = existsSync(join(src, 'icon.ico'))
  ? join(src, 'icon.ico')
  : join(root, 'assets', 'icon.ico');
if (existsSync(iconSrc)) {
  cpSync(iconSrc, join(root, 'icon.ico'));
}

console.log(`Deployed ${rootExeName} + _internal to project root`);
