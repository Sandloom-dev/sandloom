import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import { patchedSource } from './apply-build-tool-patches.mjs';

const require = createRequire(import.meta.url);

test('vendor patches reject drift and remain idempotent', () => {
  const hash = (text) => createHash('sha256').update(text).digest('hex');
  const spec = {
    package: 'fixture',
    file: 'source.js',
    before_sha256: hash('original'),
    after_sha256: hash('patched'),
    replacements: [['original', 'patched']],
  };
  assert.equal(patchedSource('original', spec), 'patched');
  assert.equal(patchedSource('patched', spec), 'patched');
  assert.throws(() => patchedSource('unexpected', spec), /Unreviewed source/);
  assert.throws(
    () => patchedSource('original', { ...spec, after_sha256: hash('different') }),
    /Patched checksum differs/,
  );
});

test('URI parser handles advisory inputs and Ajv still resolves schema references', () => {
  const uri = createRequire(require.resolve('ajv/package.json'))('uri-js');
  assert.doesNotThrow(() => uri.parse('mailto:?to'));
  const parsed = uri.parse('https://example.test/%E0%80%AE%E0%80%AE/path');
  assert.ok(!parsed.path.includes('../'));
  const child = spawnSync(
    process.execPath,
    [
      '-e',
      `const { createRequire } = require('node:module');
       const uri = createRequire(require.resolve('ajv/package.json'))('uri-js');
       for (const separator of ['\\u2028', '\\u2029']) {
         uri.resolve('https://example.test/', separator + 'path', { iri: true });
       }`,
    ],
    { cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 2000 },
  );
  assert.equal(child.error, undefined, child.error?.message);
  assert.equal(child.status, 0, child.stderr);
  const Ajv = require('ajv');
  const ajv = new Ajv();
  ajv.addSchema({ $id: 'https://example.test/types', definitions: { count: { type: 'integer' } } });
  const validate = ajv.compile({ $ref: 'https://example.test/types#/definitions/count' });
  assert.equal(validate(3), true);
  assert.equal(validate('three'), false);
});

test('repeated extglob wildcard inputs cannot hang the build-tool matcher', () => {
  for (const pattern of ['+(*)', '+(*a)', '*(a*b)']) {
    const child = spawnSync(
      process.execPath,
      [
        '-e',
        `const pico = require('picomatch');
         try {
           const match = pico(process.argv[1])('a'.repeat(60) + '!');
           process.stdout.write(JSON.stringify({ match }));
         } catch (error) {
           if (!(error instanceof SyntaxError) || !/repeated extglob/i.test(error.message)) throw error;
           process.stdout.write(JSON.stringify({ rejected: true }));
         }`,
        pattern,
      ],
      { cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 2000 },
    );
    assert.equal(child.error, undefined, `${pattern}: ${child.error?.message}`);
    assert.equal(child.status, 0, child.stderr);
    assert.ok(child.stdout.includes('match') || child.stdout.includes('rejected'));
  }
  const pico = require('picomatch');
  assert.equal(pico('**/*.{ts,tsx}')('src/page.tsx'), true);
  assert.equal(pico('+(one|two).ts')('onetwo.ts'), true);
  assert.equal(pico('+([a*])')('a**'), true);
  assert.equal(pico('+(\\*)')('***'), true);
  assert.equal(pico('!(*.d).ts')('page.ts'), true);
});

test('patched minimatch keeps legacy CommonJS and modern named imports usable', async () => {
  const legacy = require('minimatch');
  const modern = await import('minimatch');
  assert.equal(typeof legacy, 'function');
  assert.equal(legacy('src/page.tsx', '**/*.tsx'), true);
  assert.equal(legacy('src/page.ts', '**/*.tsx'), false);
  assert.equal(modern.default, modern.minimatch);
  assert.equal(new legacy.Minimatch('src/**').match('src/page.tsx'), true);
  assert.equal(new modern.Minimatch('src/**').match('src/page.tsx'), true);
  assert.deepEqual(legacy.match(['a.ts', 'b.tsx'], '**/*.tsx'), ['b.tsx']);
  assert.equal(legacy.defaults({ nocase: true })('PAGE.TSX', '*.tsx'), true);
});

test('jsx-a11y still matches custom label and control component patterns', () => {
  const child = (name) => ({
    type: 'JSXElement',
    openingElement: {
      type: 'JSXOpeningElement',
      name: { type: 'JSXIdentifier', name },
      attributes: [],
    },
    children: [],
  });
  const contains = require('eslint-plugin-jsx-a11y/lib/util/mayContainChildComponent');
  const labelled = require('eslint-plugin-jsx-a11y/lib/util/mayHaveAccessibleLabel');
  assert.equal(contains({ children: [child('AppLabel')] }, '*Label'), true);
  assert.equal(contains({ children: [child('AppLabel')] }, '*Button'), false);
  assert.equal(labelled(child('AppControl'), 1, [], undefined, ['App*']), false);
  assert.equal(labelled(child('AppControl'), 1, [], undefined, ['Other*']), true);
});

test('Redocly still applies URL-matched headers without a network request', async () => {
  const { readFileFromUrl } = require('@redocly/openapi-core/lib/utils');
  const headers = [{ matches: 'schema.example.test/**', name: 'X-Example', value: 'matched' }];
  const customFetch = async (url, options) => {
    assert.equal(url, 'https://schema.example.test/openapi.yaml');
    assert.equal(options.headers['X-Example'], 'matched');
    return {
      ok: true,
      text: async () => 'openapi: 3.1.0',
      headers: new Headers({ 'content-type': 'application/yaml' }),
    };
  };
  const result = await readFileFromUrl('https://schema.example.test/openapi.yaml', {
    headers,
    customFetch,
  });
  assert.equal(result.body, 'openapi: 3.1.0');
});
