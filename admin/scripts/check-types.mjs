import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { execFileSync } from 'node:child_process';
const dir = mkdtempSync(join(tmpdir(), 'sandloom-admin-contract-'));
try {
  const target = join(dir, 'generated.ts');
  execFileSync(
    resolve('node_modules/.bin/openapi-typescript'),
    ['contracts/openapi.json', '-o', target],
    { stdio: 'inherit' },
  );
  if (readFileSync(target, 'utf8') !== readFileSync('src/api/generated.ts', 'utf8')) {
    throw new Error('TypeScript contracts are stale: run npm run contracts');
  }
} finally {
  rmSync(dir, { recursive: true, force: true });
}
