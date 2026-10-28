---
title: Lint policy
---

# Lint policy and the suppression ratchet

`ruff check src/` is a quality gate (pre-commit and CI). The configuration lives
in `pyproject.toml` (`[tool.ruff.lint]`); every ignored rule there
carries a comment explaining why.

## Enabled rule families

| Family | What it catches |
| --- | --- |
| `E`, `F`, `I`, `B`, `C4`, `UP` | pycodestyle / pyflakes errors, import order, bugbear, comprehensions, pyupgrade |
| `PLC0415` | `import` inside a function (import-outside-toplevel) — kernel only (`sandloom/core`, `sandloom/shared`) |
| `BLE` | blind `except Exception` that swallows the error |
| `S` | bandit security checks |
| `ASYNC` | blocking calls and anti-patterns inside coroutines |
| `SIM` | needless complexity that has a simpler equivalent |
| `PERF` | performance anti-patterns (manual list building, …) |
| `C901` | McCabe complexity above 10 |
| `RUF` | ruff-specific rules, including **RUF100**: a `# noqa` that suppresses nothing fails |
| `PGH004` | blanket `# noqa` (without codes) |

Tests (`**/tests/**`) relax the rules that only fire on test idioms — pytest
`assert`, fixture tokens, temp paths, seeded randomness, function-level imports,
sleep-polling helpers, complexity of scenario tests. The full list is in
`[tool.ruff.lint.per-file-ignores]`.

`PLC0415` is a kernel rule (ADR-0007): business modules, the `sandloom` CLI
dispatcher and the test bootstrap are exempt (`"!src/sandloom/{core,shared}/**"`),
so a lazy import in a module's `setup()` needs no marker and never touches the
framework-wide suppression baseline.

The generated module migration revisions (`src/sandloom/*/migrations/versions`) and
the core chain (`migrations/core`) are excluded; everything else in `src/`,
including the kernel's `sandloom/core/migrations` package, is linted.

## Writing a suppression

Fix the finding when you can. When the code is deliberately the way it is,
suppress the one code on the one line and say why:

```python
except Exception:  # noqa: BLE001 - metrics must never break startup
    pass
```

- **`BLE001` and every bandit code (`S…`) require a reason** after ` - `: why the
  code may fail open (or closed) there, or why the flagged construct is safe
  (`# noqa: S608 - dialect-quoted identifiers, values bound`). The ratchet test
  fails a reason-less marker.
- Other codes need a reason only when it is not obvious. A function-level import
  is assumed to be a deliberate lazy import (cycle avoidance or import-time cost —
  framework import time matters); add the reason if it is something else.
- Never write a blanket `# noqa`, and do not put `# noqa` on a line that does not
  need it: PGH004 and RUF100 reject both.
- Prefer an explicit `try` / `except` over `contextlib.suppress(Exception)`:
  `suppress` would hide the blind except from BLE001 (SIM105 is ignored for this
  reason).

## The ratchet

The families above were enabled on 2026-10-24 with their existing violations
baselined as per-line markers. `tests/architecture/test_lint_suppression_ratchet.py`
(an architecture test, so it runs in pre-commit and CI) pins, per code, how many
`# noqa` markers `src/` may carry:

- **count above the baseline** — the test fails. Satisfy the rule instead; if
  the exception is genuinely right, argue it in review and raise the number in
  `NOQA_BASELINE` in the same change.
- **count below the baseline** — the test also fails and tells you the new
  number. Lower `NOQA_BASELINE` to it, so the paydown is locked in.
- **config-level escapes are pinned too**: adding a ratcheted rule to `ignore`
  or to a `per-file-ignores` entry fails until `CONFIG_IGNORES` / `TEST_IGNORES`
  in the test are updated deliberately.

Markers are counted from comment tokens only, so a docstring that quotes a
marker never counts.

### Paying suppressions down

1. Pick a code (`C901` and `BLE001` are the most valuable) and fix one site:
   split the function, narrow the except, move the import to module level when
   it causes no cycle and no import-time cost.
2. Remove the marker; `uv run ruff check src/` must stay green.
3. Run `uv run pytest tests/architecture/test_lint_suppression_ratchet.py`
   and lower the number it reports in `NOQA_BASELINE`.

### Enabling another rule

Turn it on in `select`, run `uv run ruff check src/ --fix` for its safe fixes
and review the diff, then baseline the rest with
`uv run ruff check src/ --add-noqa="- <reason>"` (write real reasons where the
policy asks for them). Add the family's prefix to `RATCHETED_PREFIXES` and its
counts to `NOQA_BASELINE`. Keep one rule family per commit so concurrent branches
only conflict on marker lines.

`ruff --add-noqa` merges new codes into an existing marker and replaces its
reason with the new one: check the diff for lost reasons before committing.
