import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { withConsolePaths } from './console-manifest.mjs';

const PREFIX = 'src/sandloom_admin/console/';

test('only the console block is replaced, sorted, at the place it holds', () => {
  const paths = ['z.py', `${PREFIX}b`, `${PREFIX}a`, 'a.py', 'm.py'];
  assert.deepEqual(withConsolePaths(paths, PREFIX, [`${PREFIX}y`, `${PREFIX}x`]), [
    'z.py',
    `${PREFIX}x`,
    `${PREFIX}y`,
    'a.py',
    'm.py',
  ]);
});

test('a group with no console file gets the block at its end', () => {
  assert.deepEqual(withConsolePaths(['z.py', 'a.py'], PREFIX, [`${PREFIX}b`, `${PREFIX}a`]), [
    'z.py',
    'a.py',
    `${PREFIX}a`,
    `${PREFIX}b`,
  ]);
});

test('a run on the committed console leaves tools/packaging/public-source.json as it is', () => {
  const repository = fileURLToPath(new URL('../..', import.meta.url));
  const consoleDir = join(repository, PREFIX);
  const walk = (dir) =>
    readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      return statSync(path).isDirectory() ? walk(path) : [path];
    });
  const files = walk(consoleDir)
    .filter((path) => !path.endsWith('.DS_Store'))
    .map((path) => relative(repository, path).split(sep).join('/'));
  const policy = JSON.parse(
    readFileSync(join(repository, 'tools/packaging/public-source.json'), 'utf8'),
  );
  const groups = policy.include.filter((group) =>
    group.paths.some((path) => path.startsWith('src/sandloom_admin/')),
  );
  assert.equal(groups.length, 1);
  assert.deepEqual(withConsolePaths(groups[0].paths, PREFIX, files), groups[0].paths);
});
