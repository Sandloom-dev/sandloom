# Build-tool dependency hardening

`npm ci` runs `scripts/apply-build-tool-patches.mjs`, then the real consumer and
security regressions in `scripts/dependency-compat.test.mjs`. The installer uses
only Node built-ins. It checks exact package versions and pristine/patched
SHA-256 hashes, rejects unexpected source, and can run again without changing
an already reviewed result. Docker copies the manifest and scripts before
installation. Lifecycle scripts must be enabled; `npm test` also detects a
missing or ineffective patch.

`build-tools.json` contains two deliberately narrow changes:

- **minimatch 11.0.1:** preserve the callable CommonJS and default ESM imports
  used by pinned ESLint, jsx-a11y and Redocly consumers. The upstream matching
  implementation and security fix are unchanged.
- **picomatch 4.0.7:** reject single-branch repeated extglobs whose parsed tokens
  contain an unbounded wildcard, such as `+(*a)`. They can compile to nested
  unbounded regex quantifiers. Escaped stars, character classes and ordinary
  multi-branch extglobs remain supported. This guard does not depend on the
  opt-out for upstream extglob recursion checks. The regression uses a separate
  process and a timeout; the unpatched `+(*a)` matcher was observed hanging.

The unmaintained `uri-js` dependency of Ajv 6 is replaced through an npm alias
with Fastify's `fast-uri` 4.2.3. Actual Ajv reference resolution and the advisory
inputs are tested. Removing `patch-package` also removes its vulnerable
`micromatch` / `braces` / picomatch 2 dependency chain.

The complete npm audit gate stays enabled, including development dependencies.
There is no Snyk ignore policy. Snyk's package-version analysis still reports
picomatch because its advisory lists every released version; it does not
inspect the local parser patch. Retain that visible finding and the regression
evidence until an upstream fixed release replaces the patch.

Review and regenerate hashes deliberately when changing the pinned versions.
Remove the minimatch compatibility patch once every consumer uses named imports;
remove the picomatch guard once the upstream fixed implementation passes the
same attack regressions. Do not substitute a new version silently.

References:

- [Glob cancellation](https://github.com/advisories/GHSA-3g3m-2fjr-6pw4)
- [Minimatch amplification](https://github.com/advisories/GHSA-g3hv-75wg-m249)
- [Picomatch ReDoS](https://security.snyk.io/vuln/SNYK-JS-PICOMATCH-21248802)
- [uri-js advisory index](https://security.snyk.io/package/npm/uri-js)
- [braces recursion](https://security.snyk.io/vuln/SNYK-JS-BRACES-19963945)
