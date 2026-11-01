# Generated code included by the how-to guides

These files are real output of the `sandloom add` generators. The how-to guides
include them with pymdownx.snippets
(`--8<-- "docs/snippets/generated/<generator>/..."`). Do not edit them by
hand: regenerate them instead. The directory is excluded from the site.

Each `<generator>/` directory is the root of one scratch application, so a
file keeps its path relative to that application (`demo/modules/...`,
`demo/shared/...`). Only the files a guide includes were copied.

## How they are produced

`tools/docs/docs_generated.py` regenerates them (its `SNIPPET_APPS` is the one
source of the commands below) and also writes the generated files of the
finished tutorial application (`examples/tutorial`). From the repository root:

```bash
uv run python tools/docs/docs_generated.py          # write
uv run python tools/docs/docs_generated.py --check  # what CI runs
```

`tests/architecture/test_docs_generated.py` runs `--check`, so a
framework change that alters the generator output fails until the files are
rewritten. Each scratch application is
`sandloom new demo --no-batteries --module notes`, then:

| Directory | Commands (all `sandloom add --root <copy>/demo ...`) |
| --- | --- |
| `module/` | `module board` |
| `event/` | `event notes NoteArchived` |
| `contract/` | `contract stats quota`, in an app made with `--module stats` instead of `--module notes` (so the names match the tutorial app) |
| `consumer/` | `module board`; `consumer board notes.item.created.v1`; `consumer board billing.invoice.paid.v1 --field invoice_id:UUID --field tenant_id:UUID --field amount:int` |
| `saga/` | `saga notes onboarding` |
| `projection/` | `projection notes board` |

If a guide includes a line range (`file.py:13:15`), check that the range
still covers the same lines after a rewrite.
