// Bundle budget: gzip size of the shipped JS and CSS. Raise a budget only with
// a reason in the commit message; the console must stay light for operators.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';

const BUDGET = { js: 230 * 1024, css: 20 * 1024 };
const dir = 'dist/assets';
const totals = { js: 0, css: 0 };
for (const name of readdirSync(dir)) {
  const kind = name.endsWith('.js') ? 'js' : name.endsWith('.css') ? 'css' : null;
  if (kind) totals[kind] += gzipSync(readFileSync(join(dir, name))).length;
}
const kb = (n) => `${(n / 1024).toFixed(1)} KiB`;
let failed = false;
for (const kind of Object.keys(BUDGET)) {
  const ok = totals[kind] <= BUDGET[kind];
  failed ||= !ok;
  console.log(
    `${ok ? 'ok  ' : 'FAIL'} ${kind}: ${kb(totals[kind])} gzip (budget ${kb(BUDGET[kind])})`,
  );
}
if (failed) process.exit(1);
