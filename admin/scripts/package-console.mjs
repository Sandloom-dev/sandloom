// Copy the built console into the sandloom-admin package and record what it was built from.
// tools/packaging/console_build.py recomputes the same hash, so a stale copy fails the gates.
import { createHash } from 'node:crypto';
import {
  cpSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { join, relative, sep } from 'node:path';

const admin = new URL('..', import.meta.url).pathname;
const repository = join(admin, '..');
const target = join(repository, 'src/sandloom_admin/console');
const manifestFile = join(repository, 'tools/packaging/public-source.json');
// Each deployment supplies these two; the package must not ship the framework's.
const perApplication = new Set(['admin-manifest.json', 'config.json']);

const walk = (dir) =>
  readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
const posix = (path, root) => relative(root, path).split(sep).join('/');

const inputs = [
  ...['src', 'public'].flatMap((dir) => walk(join(admin, dir))),
  ...['index.html', 'package-lock.json', 'vite.config.ts', 'tsconfig.json'].map((f) =>
    join(admin, f),
  ),
]
  .map((path) => posix(path, admin))
  .filter((name) => !/\.(test|stories)\.[a-z]+$/.test(name))
  .filter((name) => !name.endsWith('.DS_Store'))
  .filter((name) => !perApplication.has(name.replace('public/', '')))
  .sort();
const digest = createHash('sha256');
for (const name of inputs) {
  digest.update(`${name}\n`);
  digest.update(readFileSync(join(admin, name)));
}

const widgets = walk(join(admin, 'src/features'))
  .filter((path) => /\/widgets\.tsx$/.test(path))
  .flatMap((path) => [...readFileSync(path, 'utf8').matchAll(/^ {2}'([a-z-]+\.[a-z-]+)': \{/gm)])
  .map((match) => match[1])
  .sort();
if (widgets.length === 0) throw new Error('No widgets found; the pattern above is out of date');

rmSync(target, { recursive: true, force: true });
mkdirSync(target, { recursive: true });
const dist = join(admin, 'dist');
cpSync(dist, target, {
  recursive: true,
  filter: (source) => !perApplication.has(posix(source, dist)),
});
writeFileSync(
  join(target, 'console.json'),
  `${JSON.stringify({ manifest_versions: [1], source_sha256: digest.digest('hex'), widgets }, null, 2)}\n`,
);

// The source export names every file, and hashed asset names change with each build.
const policy = JSON.parse(readFileSync(manifestFile, 'utf8'));
const prefix = 'src/sandloom_admin/console/';
for (const group of policy.include) {
  if (!group.paths.some((path) => path.startsWith('src/sandloom_admin/'))) continue;
  group.paths = [
    ...group.paths.filter((path) => !path.startsWith(prefix)),
    ...walk(target).map((path) => posix(path, repository)),
  ].sort();
}
writeFileSync(manifestFile, `${JSON.stringify(policy, null, 2)}\n`);
console.log(`packaged ${walk(target).length} files into ${posix(target, repository)}`);
