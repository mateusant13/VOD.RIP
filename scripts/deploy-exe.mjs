/**
 * Copy the PyInstaller output to the project root so vod-rip.exe lives
 * alongside _internal/ (required one-folder layout).
 */
import { cpSync, existsSync, rmSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const src = join(root, 'dist', 'vod-rip');
const exeSrc = join(src, 'vod-rip.exe');
const internalSrc = join(src, '_internal');

if (!existsSync(exeSrc)) {
  console.error('Missing build output:', exeSrc);
  console.error('Run: npm run build-dist');
  process.exit(1);
}

cpSync(exeSrc, join(root, 'vod-rip.exe'));
if (existsSync(internalSrc)) {
  rmSync(join(root, '_internal'), { recursive: true, force: true });
  cpSync(internalSrc, join(root, '_internal'), { recursive: true });
}
if (existsSync(join(src, 'icon.ico'))) {
  cpSync(join(src, 'icon.ico'), join(root, 'icon.ico'));
}

console.log('Deployed vod-rip.exe + _internal to project root');
