import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const sha256 = (text) => createHash('sha256').update(text).digest('hex');

export function patchedSource(source, spec) {
  const current = sha256(source);
  if (current === spec.after_sha256) return source;
  if (current !== spec.before_sha256) {
    throw new Error(`Unreviewed source for ${spec.package}/${spec.file}`);
  }
  let output = source;
  for (const [before, after] of spec.replacements) {
    if (output.split(before).length !== 2) {
      throw new Error(`Patch anchor is not unique: ${spec.package}/${spec.file}`);
    }
    output = output.replace(before, after);
  }
  if (sha256(output) !== spec.after_sha256) throw new Error('Patched checksum differs');
  return output;
}

export function applyBuildToolPatches() {
  const patches = JSON.parse(
    readFileSync(new URL('../patches/build-tools.json', import.meta.url), 'utf8'),
  );
  for (const spec of patches) {
    const metadata = require.resolve(`${spec.package}/package.json`);
    if (JSON.parse(readFileSync(metadata, 'utf8')).version !== spec.version) {
      throw new Error(`Review patches before changing ${spec.package} ${spec.version}`);
    }
    const root = dirname(metadata);
    const target = resolve(root, spec.file);
    if (relative(root, target).startsWith('..')) throw new Error('Patch escapes package');
    const before = readFileSync(target, 'utf8');
    const after = patchedSource(before, spec);
    if (after !== before) writeFileSync(target, after);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  applyBuildToolPatches();
}
