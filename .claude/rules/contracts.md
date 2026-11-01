---
paths:
  - "**/contracts*.py"
  - "**/services/contract*.py"
  - "**/shared/contracts/**"
  - "**/module.snapshot.json"
---
# Sync service contracts

- **Provider.** `@contract("<m>.<name>.v1")` on a stateless class (no
  `__init__`, no instance attributes; `ClassVar` constants are fine). Its
  public `async def` methods are reads; `@command(idempotency_key="<arg>")`
  marks a command. Helpers are `_private`. SANDLOOM-B018 refuses anything else.
  `sandloom add contract <m> <name>` writes the provider, its DTO in
  `schemas.py`, then runs `sandloom generate` (stub and DTO copy).
- **Session.** Inside a method, `db()` is the provider unit's session. A
  method never takes a session parameter.
- **Signatures.** Every parameter and the return use wire types: JSON
  scalars, `UUID`, `datetime`, `Decimal`, `Enum`, `Literal`, pydantic models,
  and containers or unions of those (a fully typed signature uses the faster
  codec, C026). No `*args`, no `**kwargs`.
- **DTOs** (models and Enums in a signature) live in the provider's own
  `schemas` module, inside the closed set of event field shapes (finite
  floats, Field constraints, no parametrised generic). `sandloom generate`
  copies each into `<shared>/contracts/<module>_dto.py`; callers import and
  receive the copy (C012). Validators are not copied: they run in the provider.
- **Reads** run in a unit that is always rolled back, and never write
  (`ReadContractWriteError`).
- **Commands** are deduped durably in the provider's database by their
  idempotency key. A command is NOT atomic with the caller: design it as a
  saga step. `risk="money"` declares a money source.
- **Callers** declare `x = Contract(REF)` at module level, with `REF` from the
  generated stub `sandloom.shared.contracts.<name>`; `define_module` derives
  `consumes` from it. A required contract nobody provides fails startup
  (SANDLOOM-B005). `Contract(REF, optional=True).client()` is `None` only when the
  provider is not deployed: use it only where the caller has a correct answer
  without it.
- **Generated files.** `sandloom generate` writes the stub (SANDLOOM-T007), the
  DTO copies (C012) and `contracts.snapshot.json` (SANDLOOM-T004); `sandloom test`
  only checks them. Never hand-edit them; a breaking change fails before
  anything is written.
- **Breaking changes** need a new `.v2` contract, not an edit of `.v1`
  (`sandloom contracts check --against <snapshot|url>`).
- **Testing.** Fake a contract you call with `fake_contract(REF, impl)`.
  Prove local and remote calls match with `assert_contract_conformance`
  (`sandloom.sdk.testing`).
- Concept: `docs/modules/between/contracts.md`.
