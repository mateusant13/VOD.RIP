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
// A MISSING RUNTIME IS NOT A FAILED RELEASE.
//
// This step used to throw, and on 2026-10-15 that killed the whole v1.0.54
// release: `build-asr-runtime` ran, produced nothing at
// dist/runtimes/asr/<ver>, the packaging step above correctly WARNED and
// skipped ("No ASR runtime at ... - skipping ASR zip (base still built)"),
// and then this file threw on the artifact the previous step had just
// announced it would not create. The release job failed and the
// `release` job - which needs it - was skipped, so no GitHub Release was
// published for a version that was otherwise complete.
//
// The workflow already states the intent ("base still built"). So the
// absent runtime is reported the same way the step above reports it, and
// the release ships the base plus its installer. What must NOT happen is
// a manifest describing an archive that does not exist: that is the
// corrupt state an installer would trust.
const missing = [!existsSync(runtimeDir) && runtimeDir, !existsSync(archivePath) && archivePath]
  .filter(Boolean);
if (missing.length) {
  console.warn(
    `WARNING: no ASR runtime for ${version} - skipping the manifest, the base still ships.`
  );
  for (const m of missing) console.warn(`  absent: ${m}`);
  console.warn(
    "The opt-in ASR runtime must be built before tagging if this release is meant to offer it."
  );
  process.exit(0);
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
