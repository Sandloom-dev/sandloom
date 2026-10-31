import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);

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
