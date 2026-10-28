# Public repository layout: ledger disposition

The Sandloom rename and source-layout migration preserve the following promises.
Their fingerprints change because public paths and CLI names replace the legacy
layout; this records the disposition without changing the pre-wave baseline.

| Id | Disposition |
| --- | --- |
| G-OPS-20 | Retain locked, non-root image requirements; Dockerfile moved to `deploy/docker/Dockerfile`, with the same architecture test. |
| C-DEPLOY-01 | Retain monolith behavior; profile moved to `deploy/compose/monolith.yml`. |
| C-DEPLOY-02 | Retain split behavior and exact contract bindings; profile moved to `deploy/compose/microservices.yml`. |
| C-MODULE-05 | Retain stable analyzer IDs and exit codes; CLI/test identifiers use Sandloom and tests live outside the source package. |
| C-TENANT-09 | Retain explicit, reasoned global-table exemptions; checker test identifier uses Sandloom. |
| C-TEST-05 | Retain dry-run-by-default, ownership-safe resource pruning; script moved to `tools/checks/test_resources.py`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
