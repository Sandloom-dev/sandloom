// Preserve Sandloom's license and dependency notices in the static bundle.
import { copyFileSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
const lock = JSON.parse(readFileSync('package-lock.json', 'utf8'));
const notices = [];
for (const [path, entry] of Object.entries(lock.packages)) {
  if (!path.startsWith('node_modules/') || entry.dev) continue;
  const pkg = JSON.parse(readFileSync(join(path, 'package.json'), 'utf8'));
  const files = readdirSync(path).filter((name) =>
    /^(licen[cs]e|copying|notice)(\.|$)/i.test(name),
  );
  for (const name of files) {
    notices.push(`${pkg.name}@${pkg.version} — ${name}\n${readFileSync(join(path, name), 'utf8')}`);
  }
}
writeFileSync('dist/third-party-licenses.txt', notices.join('\n\n--------------------\n\n'));
copyFileSync(new URL('../LICENSE', import.meta.url), 'dist/LICENSE');
copyFileSync(new URL('../NOTICE', import.meta.url), 'dist/NOTICE');
