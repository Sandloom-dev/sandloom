---
title: Checks
description: What sandloom check and sandloom test verify, the four families of error codes and when each fires, and how a failure leads you or your agent to the fix.
type: concept
job: When my agent says a change is done, I want a check that fails on a broken rule and names the fix, so I can trust the result without reading every line.
purpose: The reader finishes every change on sandloom test, follows a failure to its code and runs sandloom explain on it, and knows which family of codes fires where.
audience: [developer, agent]
level: first-app
---

# Checks

Sandloom holds its rules with checks, not with instructions. A check reads the code or runs it, and fails the change that breaks a rule whether or not the agent remembered the rule.

A broken framework rule carries a code such as `SANDLOOM-C001`, and `uv run sandloom explain <code>` gives its reason and its fix. The family of the code says when the check ran. `uv run sandloom test` runs the tests with every static check, and a change is done when its last line reads `sandloom test: OK`.

## The four families of codes

| Code | Fires | Example |
| --- | --- | --- |
| `SANDLOOM-C…` | `sandloom check` reads the code, without running it | `C001`: a module imports another module |
| `SANDLOOM-B…` | The application refuses to start; `sandloom check --boot` starts it to find these | `B008`: a route is neither authenticated nor declared public |
| `SANDLOOM-R…` | A request or a job fails at runtime | `R002`: database work outside a [unit](../modules/database/units.md), the block that owns the transaction |
| `SANDLOOM-T…` | `sandloom test` or `sandloom generate` | `T005`: an event's fields changed in a way its readers cannot follow |

A code is never reused. [Error codes](../reference/error-codes.md) lists every one with its cause and fix.

## A failure, from the check to the fix

1. An agent imports one module from another, `from demo.modules.example import api` (rejected: `SANDLOOM-C001`).
2. `uv run sandloom check` lists every violation, then one "How to fix" entry per rule (shortened here):

    ```text
    sandloom check: 1 architecture violation(s)

      demo/modules/notes/services.py:37: error[SANDLOOM-C001 module-boundaries]: from demo.modules.example import ...

    How to fix:
      SANDLOOM-C001 module-boundaries: …
          docs: https://sandloom.dev/docs/reference/error-codes/#sandloom-c001
          help: sandloom explain SANDLOOM-C001
    ```

3. The agent runs the `help` command; `uv run sandloom explain C001` works too. It starts with the reason for the rule:

    ```text
    SANDLOOM-C001 [module-boundaries] (check)

    a business module imports a sibling business module

    Modules are atomic and isolated: each one can be deployed as its own service.
    ```

    Its `Fix:` lines name the replacement: an [event](../modules/between/events.md) or a [contract](../modules/between/contracts.md), the two ways one module reaches another.
4. The agent makes that change and runs the check again until it prints `sandloom check: OK`.

## What sandloom test runs

`uv run sandloom test` runs every step, even after one fails, and ends with a summary. Here the import above is still in place:

```text
sandloom test summary
----------------
  FAIL (exit 1)    ruff check               0.6s
  PASS             generated files          0.4s
  PASS             schema & deploy          0.0s
  PASS             notes                    4.7s
  PASS             suite (not slow)         6.7s
  FAIL (exit 1)    ruff format              0.0s
  PASS             mypy                     6.5s
  FAIL (exit 1)    sandloom check           0.1s  -> sandloom explain SANDLOOM-C0xx (per violation)
  PASS             contract snapshots       0.1s
  PASS             route snapshots          0.0s
  PASS             migration drift          2.6s

sandloom test: FAILED (ruff check, ruff format, sandloom check)
```

<!-- prose-ok: P6 the option list and the failure kinds are real lists, not rhythm -->
`ruff check` runs first: what it fixes is in the tree the generated-files check and the tests see. Then the module you name, the fast suite and the other static checks. A failing framework step points at `sandloom explain`; a ruff, mypy or test failure prints its own message above the summary. `sandloom test` never writes a generated file: when one is stale, it fails and says to run `uv run sandloom generate`. [Run the suites](../modules/testing.md#run-the-suites) covers `--full`, `--no-gates` and `--plan`.

## Run sandloom check alone

`sandloom check` is static: it imports no application code and needs no environment.

```bash
uv run sandloom check --list-rules
```

<!-- prose-ok: P6 three exit codes and two options are real lists -->
`--only <rule>,<rule>` runs a subset, and `--boot` also starts the application through `create_app` to report every `SANDLOOM-B…` refusal and stale route snapshots. It exits 0 when clean, 1 on a violation and 2 on a bad `architecture_rules.yml`.

A `sandloom check` rule that must not apply to one line is waived on that line, with its reason: `# sandloom: allow SANDLOOM-C001 <reason>` (the rule name `module-boundaries` works too). A waiver without a reason does not apply, and the other families cannot be waived this way.

The checks prove the structure, not the behaviour: whether a feature does what you meant is for your tests. `sandloom check: OK` covers only what the code shows. It cannot see an import built from a string at runtime, a name reached through `getattr`, or behaviour set by configuration. `--boot` and the tests in `sandloom test` cover those. Some rules have no check yet; the framework's [module-author contract](https://github.com/Sandloom-dev/sandloom/blob/main/src/sandloom/AGENTS.md#rules-no-check-enforces-yet-get-these-right-yourself) lists them.

## Go deeper

- [Error codes](../reference/error-codes.md): every code with its cause and fix.
- [Testing modules](../modules/testing.md): the fixtures and helpers for your module's tests.
- [The toolchain](index.md): where the checks sit in the loop of a change.
