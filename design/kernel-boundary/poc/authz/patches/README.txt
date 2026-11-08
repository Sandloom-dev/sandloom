kb-poc-authz.patch: the PoC candidate against ceb3d66 (branch arch/kernel-boundary).

Apply in a throwaway worktree, never in a shared checkout:
    git worktree add --detach <dir> ceb3d66 && cd <dir> && git apply <this dir>/kb-poc-authz.patch
    uv sync && uv run sandloom generate --check

Per file, which change it carries. The separate changes sit behind module
constants (contract_check._K5, contract_check._WITNESS, deps._K6,
readiness._K6_SEED), all False: the candidate as applied is PA-2 alone. The PoC
stand sets them from KB_POC_K5 / KB_POC_K6 / KB_POC_WITNESS before it builds the
app (stand.py _poc_switches); the kernel reads no environment for them.

PA-2 (the alternative)
  src/sandloom/shared/authz/contract_check.py   access_check, From, ContractCheck,
                                                ClaimFactCheck, membership witness (K1);
                                                K5 and WITNESS branches behind switches
  src/sandloom/core/module_api/_schema.py       access_check in the describe document
  src/sandloom/core/contracts/compatibility.py  removed / re-bound / lowered-risk check = incompatible
  src/sandloom/core/cli/generated.py            atoms and claim classes in render_stub
  src/sandloom/sdk/authz.py, public_api_baseline.json   four new advanced names
  src/sandloom/shared/authz/witness.py          K1: the declared membership witness registry
  src/sandloom/shared/authz/account.py          K1: tenant kind via the declared witness
  src/sandloom/shared/authz/facts.py            lookup_access_fact(owner=), AccessFact(atom=...)
  src/sandloom_tenancy/contracts.py             @access_check on has_role_at_least (OrgRole)
  src/sandloom_billing/contracts.py             @access_check on check_subscription_active,
                                                new has_credits_at_least (Credits)
  src/sandloom_billing/access.py                AccessFact(atom="Subscribed")
  src/sandloom/shared/contracts/*_v1.py, src/sandloom_*/*.snapshot.json   generated

Static checks from metadata (Z-8)
  src/sandloom/core/archcheck/atom_metadata.py, module_rules.py, api10_rules.py,
  runner.py, error_codes.py (SANDLOOM-C032 handwritten-access-check),
  cli/check.py (rule list), tests/architecture/test_api10_checks.py (code pin),
  tools/packaging/public-source.json (the three new files classified)

K6 (switch KB_POC_K6=1)
  src/sandloom/shared/authz/algebra.py          Requirement.needs_request_session()
  src/sandloom/shared/authz/deps.py             require() asks the policy
  src/sandloom/core/rpc/readiness.py            boot probe of every binding seeds the describe cache

PA-3 comparison only
  src/sandloom/shared/authz/facts_contract.py   facts.v1.evaluate(..., params)
  src/sandloom/shared/authz/facts.py            _remote_live_resolver sends params
  src/sandloom/shared/authz/atoms_fact.py       Entitled(params=)
