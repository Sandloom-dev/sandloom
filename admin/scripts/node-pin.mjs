// The one Node that builds the console. The precompressed .gz and .br files are bytes of the
// zlib and brotli inside Node, and the packaged copy is committed, so the same source gives the
// same bytes only on the same Node. admin/.nvmrc names it, package.json `engines.node` repeats
// it and the CI setup-node steps read it (a test keeps the first two equal).
//
// Run as a script (`node scripts/node-pin.mjs`) it refuses any other Node: `npm run package`
// does that first, so the build fails before tsc and vite run.
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const FULL_VERSION = /^\d+\.\d+\.\d+$/;

// The first line of .nvmrc, with the `v` nvm accepts in front; only a full x.y.z is a pin.
export const parsePin = (text) => {
  const pin = (text.split('\n')[0] ?? '').trim().replace(/^v/, '');
  if (!FULL_VERSION.test(pin)) {
    throw new Error(`admin/.nvmrc must name one full x.y.z Node version, found "${pin}"`);
  }
  return pin;
};

export const pinnedNode = () =>
  parsePin(readFileSync(new URL('../.nvmrc', import.meta.url), 'utf8'));

// The refusal text of a running version that is not the pin, or null.
export const nodeProblem = (running, pinned) =>
  running === pinned
    ? null
    : `Node ${running} is not the pinned ${pinned} (admin/.nvmrc): the .gz and .br bytes differ between Node builds. Run \`nvm use\` (or install ${pinned}) first.`;

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const problem = nodeProblem(process.versions.node, pinnedNode());
  if (problem) {
    console.error(problem);
    process.exit(1);
  }
}
