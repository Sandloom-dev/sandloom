// Flat config. Token discipline is enforced here (no inline styles, no arbitrary
// Tailwind values) and in scripts/check-tokens.mjs (no raw colors/px in CSS).
import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import storybook from 'eslint-plugin-storybook';

// Arbitrary VALUES (w-[13px], bg-[#fff], h-[calc(...)]); selector variants like
// data-[state=open]: stay allowed because they carry no design value.
const arbitraryValue = String.raw`\b[a-z-]+-\[[^\]]*[\d#(][^\]]*\](?!:)`;

export default tseslint.config(
  {
    ignores: [
      'dist',
      'storybook-static',
      'node_modules',
      'coverage',
      'test-results',
      'playwright-report',
      'src/api/generated.ts',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  ...tseslint.configs.stylisticTypeChecked,
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
      globals: { ...globals.browser, ...globals.node },
    },
  },
  {
    files: ['**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks, 'jsx-a11y': jsxA11y },
    rules: {
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.strict.rules,
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/consistent-type-imports': 'error',
      '@typescript-eslint/restrict-template-expressions': ['error', { allowNumber: true }],
      'no-restricted-syntax': [
        'error',
        {
          selector: 'JSXAttribute[name.name="style"]',
          message: 'Use design-token utility classes; inline styles bypass the token system.',
        },
        {
          selector: `Literal[value=/${arbitraryValue}/]`,
          message:
            'Arbitrary Tailwind values bypass the tokens; add a token in src/styles/tokens.css.',
        },
        {
          selector: `TemplateElement[value.raw=/${arbitraryValue}/]`,
          message:
            'Arbitrary Tailwind values bypass the tokens; add a token in src/styles/tokens.css.',
        },
        {
          selector: 'Literal[value=/#[0-9a-fA-F]{3,8}\\b/]',
          message: 'Raw hex colors are forbidden; use a color token.',
        },
      ],
      'no-restricted-globals': [
        'error',
        { name: 'confirm', message: 'Use <ConfirmDialog>; window.confirm is not accessible.' },
        { name: 'alert', message: 'Use toasts or inline states.' },
      ],
      'no-restricted-properties': [
        'error',
        { object: 'window', property: 'confirm', message: 'Use <ConfirmDialog>.' },
        { object: 'window', property: 'alert', message: 'Use toasts or inline states.' },
      ],
    },
  },
  {
    files: ['src/features/**/*.{ts,tsx}', 'src/app/**/*.{ts,tsx}'],
    rules: {
      // Pages talk to the server only through TanStack Query hooks in src/api.
      'no-restricted-globals': [
        'error',
        { name: 'fetch', message: 'Use the api client through a query/mutation hook.' },
        { name: 'confirm', message: 'Use <ConfirmDialog>.' },
        { name: 'alert', message: 'Use toasts or inline states.' },
      ],
    },
  },
  {
    files: ['tests/**/*.{ts,tsx}', '**/*.test.{ts,tsx}', '**/*.stories.tsx'],
    rules: {
      '@typescript-eslint/no-non-null-assertion': 'off',
      '@typescript-eslint/require-await': 'off',
    },
  },
  ...storybook.configs['flat/recommended'],
  {
    files: ['**/*.{js,mjs}'],
    ...tseslint.configs.disableTypeChecked,
  },
);
