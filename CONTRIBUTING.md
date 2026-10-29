# Contributing to Sandloom

Development takes place in [Sandloom-dev/sandloom](https://github.com/Sandloom-dev/sandloom).
Use GitHub issues for ordinary bugs and feature requests; use
[private security reporting](SECURITY.md) for vulnerabilities.

Read [AGENTS.md](AGENTS.md) and the area-specific contract before changing code.
Business modules and their tests use the public SDK and their own package.

## Branches and owner review

All changes to `main` go through a PR. Final review and manual merge belong to
[@Maryarty](https://github.com/Maryarty). Agents and contributors leave PRs open
for the owner; do not merge or enable auto-merge without an explicit instruction
for that PR. See [branches and owner review](docs/contributing/branches.md) for
the protection configuration and verification procedure.

## Licensing contributions

Sandloom uses [Apache-2.0](LICENSE). Unless explicitly stated otherwise,
contributions intentionally submitted for inclusion are provided under that
license, as described in its section 5. Contributors retain copyright in their
work and must have the right to submit it. Preserve third-party notices and
identify any included third-party material and its license.

## Development environment

Run from the repository root:

```bash
uv sync --locked --all-extras
docker compose -f deploy/compose/test.yml up -d --wait
uv run sandloom test <module>
uv run sandloom test --full
```

Use your own `SANDLOOM_TEST_DATABASE_URL`, `SANDLOOM_TEST_CACHE_URL`,
`SANDLOOM_TEST_BROKER_URL` and `SANDLOOM_TEST_OWNER` on shared machines.

The root `sandloom-dev` project is the contributor environment. Release
distributions are built from `packages/sandloom-*` using the shared packaging hook. Preserve one
`src/sandloom` source tree and the module-local tests. The public testing kit is
runtime product code; repository test suites do not belong in runtime wheels.

## Documentation and website

Edit prose in `docs/`, the homepage in `website/`, and tutorial code in
`examples/tutorial/`. Docs include the real tutorial files instead of a second
copy. Generated references and snapshots are refreshed by their owning tools,
not hand-edited. The combined site build places documentation at `/docs/`.

## CI profiles

PRs run checks for their changed areas and the fast backend suite on Python
3.12. Before release, run **Full validation (manual)** on the selected commit
for the complete matrix and heavy scenarios. See
[CI profiles and minute budget](docs/contributing/ci.md). The local runner
retains full backend validation by default.

## Review and validation

Explain the behavior, the reason and validation for a change. Preserve existing
architecture checks, guarantee IDs, migration revisions and public API baselines.
Agent-context, generators and API changes require a DX evaluation before release.
See the [source snapshot policy](docs/contributing/source-export.md) for source
selection and the limits of its checks. Package releases and website deployments
are separate operator actions.
