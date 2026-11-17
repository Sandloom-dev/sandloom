// Precompress the hashed text assets (.br and .gz beside each file), so the Python package and
// nginx (gzip_static) serve them compressed with no work per request and no runtime dependency.
// A file is skipped when compression saves too little to be worth the extra bytes in the wheel.
//
// The bytes of a .gz / .br depend on the zlib and brotli build inside Node, and the packaged copy
// is committed: `npm run package` (SANDLOOM_REQUIRE_PINNED_NODE=1) refuses any Node but the one in
// .nvmrc, so the same source gives the same bytes on every machine. A plain `npm run build` only
// warns, its output is not committed.
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { brotliCompressSync, constants, gzipSync } from 'node:zlib';

const pinned = readFileSync(new URL('../.nvmrc', import.meta.url), 'utf8').trim();
if (process.versions.node !== pinned) {
  const message = `Node ${process.versions.node} is not the pinned ${pinned} (admin/.nvmrc): the .gz and .br bytes differ between Node builds.`;
  if (process.env.SANDLOOM_REQUIRE_PINNED_NODE === '1') {
    throw new Error(
      `${message} Run \`nvm use\` (or install ${pinned}) before \`npm run package\`.`,
    );
  }
  console.warn(`warning: ${message}`);
}

const dir = 'dist/assets';
const TEXT = /\.(js|css|svg)$/;
const MIN_BYTES = 1024;
let before = 0;
let after = 0;
for (const name of readdirSync(dir).filter((name) => TEXT.test(name))) {
  const path = join(dir, name);
  const source = readFileSync(path);
  if (source.length < MIN_BYTES) continue;
  const br = brotliCompressSync(source, {
    params: {
      [constants.BROTLI_PARAM_QUALITY]: constants.BROTLI_MAX_QUALITY,
      [constants.BROTLI_PARAM_SIZE_HINT]: source.length,
    },
  });
  const gz = gzipSync(source, { level: 9 });
  if (gz.length > source.length * 0.9) continue;
  writeFileSync(`${path}.br`, br);
  writeFileSync(`${path}.gz`, gz);
  before += source.length;
  after += br.length;
}
const kb = (n) => `${(n / 1024).toFixed(1)} KiB`;
console.log(`precompressed ${kb(before)} of assets to ${kb(after)} (brotli)`);
