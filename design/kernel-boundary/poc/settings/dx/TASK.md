# DX task (KB-49 P12): give the `catalog` module its settings

You add settings to a Sandloom business module `catalog` (package `catalog/`
in your working directory, already importable; it has `__init__.py` with
`register = define_module(__name__, version="0.1.0", setup=_setup)` and an
`api.py` with one public route).

Declare, using ONLY the settings form described in `REFERENCE.md`:

1. `CATALOG_API_KEY` — a secret (optional, default unset).
2. `CATALOG_PAGE_SIZE` — an int, default 50, allowed 1..500 (an operations setting).
3. `CATALOG_SHOW_UNPUBLISHED` — a bool, default false; a *semantic* setting
   (it weakens a guarantee: drafts become visible). True is the weaker value.
4. An accepted risk `show_unpublished_in_prod` for it.
5. A production check: with ENVIRONMENT=prod, `CATALOG_SHOW_UNPUBLISHED=true`
   is refused unless that risk is accepted.
6. A removed setting `CATALOG_LEGACY_INDEX` (removed in 0.2; hint: "the index
   is always rebuilt from events").
7. The route in `api.py` returns `{"page_size": <current page size>}` read
   from the settings (not from the environment); `_setup` reads the API key.
8. Tests in `tests/test_catalog.py` (use `harness.py`): default env boots; page
   size 0 is refused naming `CATALOG_PAGE_SIZE`; the prod check refuses and
   the accepted risk `catalog.show_unpublished_in_prod` lets it boot; the
   removed setting set to `true` is refused; the route returns an overridden
   page size via the testing override from `REFERENCE.md`.
9. `mypy --strict catalog` passes (readers typed).

Run: `$PY -m pytest tests -q` and `$PY -m mypy --strict catalog` ($PY is given
in your brief). Do not read or edit anything outside your working directory
except the framework sources if you really need them (count it).

When done, report exactly:
- files you created or edited (list);
- errors you hit (every failing test run, mypy run or traceback, one line each with the cause);
- documentation lookups (times you re-read REFERENCE.md or any doc to resolve something);
- framework (kernel) source files you opened (list);
- the final pytest and mypy summary lines.
