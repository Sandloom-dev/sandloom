# Build-tool dependency compatibility

The admin uses `glob` 14.0.2 and `minimatch` 11.0.1 to address
[GHSA-3g3m-2fjr-6pw4](https://github.com/advisories/GHSA-3g3m-2fjr-6pw4) and
[GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249).
The npm audit gate remains unchanged, including development dependencies.

Pinned ESLint, jsx-a11y and Redocly consumers still use the older callable
CommonJS/default import of minimatch. Overriding their dependency with the
unmodified version 11 breaks those calls. The version-specific patch restores
only the callable CommonJS export and the ESM default export. Named exports,
the matching implementation and the upstream security fix are unchanged.

`npm ci` applies the patch with `patch-package --error-on-fail`, then runs
`scripts/dependency-compat.test.mjs`. Those tests exercise both import formats,
jsx-a11y's custom component patterns and Redocly's URL-matched headers. They
also run with `npm test`. Docker copies the patch and test before installation.
Do not install this build environment with lifecycle scripts disabled.

Remove the patch and its install hook once every pinned consumer accepts the
modern named import. Remove the overrides once their declared version ranges
select fixed versions on a fresh install. Recheck both conditions when changing
these dependencies; do not silently regenerate the patch for another version.
