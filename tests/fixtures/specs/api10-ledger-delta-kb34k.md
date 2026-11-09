# Ledger delta: KB-34k, an access fact is identified by owner and name

KB-34k (2026-11-09, owner decision on the W5+W6 security review M2) makes
`(owner, fact_name)` the identity of an access fact for authorization.
`Entitled(name)` without an owner accepted any module's local fact of that
name and walked every bound `<owner>.facts.v1`; now the owner is stated and
nothing else answers. A breaking change before the release, with upgrade
steps and no alias.

Changed public signatures (`sandloom.sdk.authz` / `sandloom.shared.authz`,
`sandloom.sdk.testing` / `sandloom.core.testing`):

| Name | Before | After |
| --- | --- | --- |
| `Entitled` | `Entitled(fact_name, *, category=None, code=None, mode=None, owner=None)` | `Entitled(fact_name, *, owner, category=None, code=None, mode=None)` |
| `fake_access_fact` | `fake_access_fact(name, value=True, *, deny_category=None, core=None, owner=None)` | `fake_access_fact(name, value=True, *, owner, deny_category=None, core=None)` |
| `lookup_access_fact` | `lookup_access_fact(ctx, name, *, owner=None)` | `lookup_access_fact(ctx, name, *, owner)` |

Behaviour:

- `owner=` missing: Python's `TypeError` when the policy (or fake) is built.
  `None`, a non-string, an empty string or a non-identifier:
  `InvalidFactOwner` (both `TypeError` and `ValueError`), new code
  `SANDLOOM-B038` (`fact-owner-missing`). Not a new public name: it is raised,
  not imported.
- `lookup_access_fact` accepts the local registration only when its
  `owner_module` is the owner, else asks only `<owner>.facts.v1` when it is
  bound; a descriptor naming another `owner_module` stays ignored
  (SANDLOOM-B034). No fallback: owner absent 501 `fact_unregistered`, owner
  unreachable 503 `fact_owner_unavailable`, both indeterminate.
- `Entitled.contracts()` is `{<owner>.facts.v1}`; the fact's first dotted
  segment is never read as its owner (`fact_owner_contract(owner)`).
- The request-local fact cache (`AuthzContext.fact_cache`) is keyed by
  `(owner, name, mode)` and the remote descriptor cache by `(owner, name)`,
  so one request never serves one owner's answer to another's policy.
- `fake_access_fact` registers the fake under the stated owner; the default
  owner `sandloom.core.testing` is gone.

Kept: SANDLOOM-B034 (a module registers a fact only under its own name), the
`FactsContract` owner filtering, the refusal of a second registration of one
fact name in one process (a monolith never hosts two owners of one name), and
`ClaimFactCheck`, which already passed the owner from its declaration literal.

| Id | Disposition |
| --- | --- |
| G-AUTHZ-14 | New: fact identity is `(owner, fact_name)`, mono and split; the five rows of the owner's table, a fake of another owner, and `Not` / `Or` over the missing-owner rows. Enforced by `tests/integration/test_fact_owner_identity.py`. |
| G-AUTHZ-04 | Unchanged: the same decision in a monolith and in a process that binds the owner's `<owner>.facts.v1`; its tests now state `owner="billing"`. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
