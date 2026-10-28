---
title: Reviewed source snapshots
---

# Reviewed source snapshots

`tools/packaging/public-source.json` is the exact public source allowlist.
Each path belongs to a group with a reason. A directory is never approved in
bulk: new files require an explicit policy change and review.

```sh
# Inspect selection only; no destination or repository is created.
python tools/packaging/export_repository.py --check
python tools/packaging/export_repository.py --audit-report /tmp/sandloom-source-review.json
```

Only files in the Git index are candidates. An untracked file, such as the
temporary workflow `tools/ci/ci_local.py` writes into `.github/workflows/`, is
never reviewed or exported, even when its path matches the policy; an approved
path that is not tracked is refused.
The checker refuses missing paths, unreviewed additions inside public source
roots, duplicate or unsafe paths, unapproved symlinks, backup
files, local environments, private material and credential-shaped content.
`.env.example` and `.env.test` are synthetic configurations, not production
credentials. The explicit symlink exceptions are the four editable-install
links `packages/editable/<distribution>/<package> -> ../../../src/<package>`
(one per distribution: `sandloom`, `sandloom_identity`, `sandloom_tenancy`,
`sandloom_billing`); each exact target must remain inside the reviewed source tree. Symlink
ancestors are refused. The credential scanner is a narrow backstop; review code changes
and the source's provenance separately.

Keep implementation and tests together. In particular, retain architecture
fixtures, Public API 1.0 migration/disposition tables, generated contracts,
module snapshots, all five example applications and the benchmark security
harness. Those files enforce framework guarantees. Raw sessions, measurements,
private plans, experimental native builds and old project material stay outside
the public selection. See the reasoned exclusions in the policy and
[third-party material](https://github.com/Sandloom-dev/sandloom/blob/main/THIRD_PARTY.md).

A later, separately authorized snapshot can be prepared with `--destination`
pointing to a new directory outside the source checkout. Preflight checks run
before that directory is created; a failed attempt removes only its own new
directory. Every output file is hashed and recorded in `SOURCE_EXPORT.json`. The exported
`.gitignore` additionally excludes `private/`; its receipt records both source
and output hashes for that explicit transformation.
The output contains a fresh empty Git history and no remote. Only the reviewed
source paths are staged in its index, so `--check` works immediately; the receipt
and later build artifacts remain untracked. Git-local variables
inherited from a hook cannot redirect initialization into the source repository.
The tool never installs dependencies, builds, commits, pushes or publishes.

After an export, install and validate in the fresh checkout before transferring
it. Source selection checks do not establish that its tests or builds pass.
The generated `SOURCE_EXPORT.json` receipt is explicitly excluded from later
source selections, so a prepared snapshot can check its own policy.
