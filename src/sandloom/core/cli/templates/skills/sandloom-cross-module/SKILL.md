---
name: sandloom-cross-module
description: Use when one module of this Sandloom app must tell another something, ask it a question, react to what it did, read its data in a list, or run a process across several modules with undo. It picks between an event, a consumer, a contract call, a projection and a saga, and names the command and recipe of each.
---

# Talk to another module from <app>

Modules never import each other. Pick the way by what the work needs, then run
its command. Each command ends with `sandloom generate`; review that diff and
never edit what it wrote.

## Which one

| The work is | Use | Command | Recipe or doc |
|---|---|---|---|
| tell others that something happened, and not wait | an event (producer) | `uv run sandloom add event <module> <CamelName>` | `uv run sandloom docs events` |
| do something when another module's event arrives | a consumer | `uv run sandloom add consumer <module> <channel>` | `uv run sandloom docs <channel>` |
| ask a question or give an order and need the answer now | a contract call | `uv run sandloom add contract-use <module> <contract>` | recipe `call-contract` |
| expose a question or an order of your own | a contract (provider) | `uv run sandloom add contract <module> <name>` | `uv run sandloom docs contracts` |
| list or filter data other modules own, one table to read | a projection | `uv run sandloom add projection <module> <name>` | recipes `projection-from-platform-events`, `projection-rebuild` |
| a process over several modules where a later failure undoes an earlier step | a saga | `uv run sandloom add saga <module> <name>` | `uv run sandloom docs sandloom.sdk.sagas` |
| change an event's fields in a breaking way | a `.v2` event | | skill `sandloom-event-version-roll` |

A user's email or name is in no event: ask the contract that owns it at the
moment you send, and never copy it into your tables.

## Decide in this order

1. **Nothing has to be undone and nobody waits:** an event. Publish it inside
   the unit, `await NoteCreated(...).publish()`; it leaves with the commit.
2. **You need an answer or a refusal now:** a contract call. A projection lags
   its sources, so it never checks a rule that must hold.
3. **You need to read many rows that others own:** a projection, rebuildable
   from the owner's read contract.
4. **A later step can fail and an earlier module's effect must be undone:** a
   saga. A contract command is not atomic with your unit, so it is a saga
   step.

## Examples

An event and its consumer (the view lists only the fields you read):

```python
class NoteCreated(Event, channel="notes.note.created.v1"): ...


@on(NoteCreatedView)
async def on_note_created(event: NoteCreatedView) -> None: ...
```

A contract call, declared at module level from the generated stub:

```python
from <app>.shared.contracts.stats_quota_v1 import STATS_QUOTA_V1
from sandloom.sdk import Contract

quota = Contract(STATS_QUOTA_V1)

left = await quota.remaining(org_id)  # a method of the provider's contract
```

The provider's refusal reaches you as `HTTPException` with its status; let it
propagate in a route. `Contract(REF, optional=True).client()` is `None` only
when the provider is not deployed: use it only where you have a correct
answer without it.

## Rules

1. **Events are at-least-once and unordered.** Order by a per-aggregate
   `version` field. A consumer takes exactly its event:
   `async def f(event: E) -> None`; it runs in its own unit, once per envelope
   id.
2. **A new field on a published event must be optional** (the same `.v1`
   accepts only that); anything else is a new `.v2`. One producer per channel.
3. **A contract is a stateless class** with public `async def` methods,
   wire-typed parameters, and its DTOs in its own `schemas.py`. A command
   declares `@command(idempotency_key="<arg>")`.
4. **A required contract nobody provides fails startup** (SANDLOOM-B005). Declare
   a consumed contract with `add contract-use` before importing its stub.
5. **Reference other modules' rows by plain UUID.** No foreign key, no import.
6. **A projection source needs a version** per aggregate; the row always
   carries `tenant_id`. `Projection` and `SourceGroup`, and the saga names
   `Saga`, `step`, `local_step`, `SagaContext`, `start_saga`, are free in the
   module's own `projections.py` and `sagas.py` and need
   `# sandloom: advanced <reason>` in any other file (SANDLOOM-T009).

Test a consumer with `await deliver(X(...))`, an event with `published(X)`, a
contract call with `fake_contract(REF, impl)`.
