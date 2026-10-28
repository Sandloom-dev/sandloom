// Token discipline gate: raw colors, px lengths and ms durations may appear
// ONLY in src/styles/tokens.css. ESLint covers TSX (inline styles, arbitrary
// Tailwind values, hex literals); this covers stylesheets and any TS string.
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const ROOT = 'src';
const ALLOWED = new Set(['src/styles/tokens.css', 'src/api/generated.ts']);
const RULES = [
  [/#[0-9a-fA-F]{3,8}\b/, 'raw hex color'],
  [/\b(?:rgba?|hsla?|oklch|oklab)\(/, 'raw color function'],
  [/(?<![\w-])\d*\.?\d+px\b/, 'px length'],
  [/(?<![\w-])\d+ms\b/, 'ms duration'],
  [/\b[a-z-]+-\[[^\]]*[\d#(][^\]]*\](?!:)/, 'arbitrary Tailwind value'],
];
const files = [];
(function walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path);
    else if (/\.(css|ts|tsx)$/.test(name)) files.push(path);
  }
})(ROOT);
const problems = [];
for (const file of files) {
  const rel = relative('.', file);
  if (ALLOWED.has(rel) || /\.(test|stories)\.tsx?$/.test(rel)) continue;
  readFileSync(file, 'utf8')
    .split('\n')
    .forEach((line, index) => {
      if (/^\s*(\/\/|\*|\/\*)/.test(line)) return;
      for (const [pattern, what] of RULES)
        if (pattern.test(line)) problems.push(`${rel}:${index + 1}: ${what}: ${line.trim()}`);
    });
}
if (problems.length) {
  console.error(
    `Design-token violations (define a token in src/styles/tokens.css):\n${problems.join('\n')}`,
  );
  process.exit(1);
}
console.log(`Design tokens: ${files.length} files clean.`);
