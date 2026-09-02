import { createHash } from 'crypto';
import { existsSync, readFileSync, readdirSync, writeFileSync } from 'fs';
import { join, relative, resolve } from 'path';
import { fileURLToPath } from 'url';

const root = resolve(fileURLToPath(new URL('..', import.meta.url)));
const version = process.argv[2] || process.env.GITHUB_REF_NAME?.replace(/^v/, '');
if (!version) throw new Error('usage: node scripts/write-asr-runtime-manifest.mjs VERSION');
const runtimeDir = join(root, 'dist', 'runtimes', 'asr', version);
const archivePath = join(root, 'release', `VOD-RIP-ASR-${version}-win-x64.zip`);
const outputPath = join(root, 'release', 'VOD-RIP-ASR-manifest.json');
if (!existsSync(runtimeDir) || !existsSync(archivePath)) {
  throw new Error(`missing ASR runtime or archive for ${version}`);
}

function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}
function filesUnder(dir) {
  const files = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) files.push(...filesUnder(path));
    else if (entry.isFile()) files.push(path);
  }
  return files;
}

const files = Object.fromEntries(
  filesUnder(runtimeDir)
    .map((path) => [relative(runtimeDir, path).replaceAll('\\', '/'), sha256(path)])
    .sort(([a], [b]) => a.localeCompare(b)),
);
const repository = process.env.GITHUB_REPOSITORY || 'mateusant13/VOD.RIP';
const tag = process.env.GITHUB_REF_NAME || `v${version}`;
const archiveName = `VOD-RIP-ASR-${version}-win-x64.zip`;
const manifest = {
  version,
  executable: 'VOD-RIP-ASR.exe',
  archive_url: `https://github.com/${repository}/releases/download/${tag}/${archiveName}`,
  archive_sha256: sha256(archivePath),
  files,
};
writeFileSync(outputPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
console.log(`Wrote ${outputPath} (${Object.keys(files).length} files)`);
