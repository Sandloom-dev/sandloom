"""Migration tooling (``sandloom-migrate``) and the module-chain runner.

A business module's ``migrations/env.py`` imports exactly one name from here::

    from sandloom.core.migrations import run_module_migrations

    run_module_migrations(__file__)

The runner lives in :mod:`sandloom.core.migrations.alembic_support`, which needs an
active Alembic context; it is imported lazily so importing this package (the
CLI does) never pulls Alembic's runtime context in.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence


def run_module_migrations(
    env_file: str | os.PathLike[str],
    *,
    package: str | None = None,
    model_modules: Sequence[str] | None = None,
    table_prefix: str | None = None,
    table_names: Iterable[str] = (),
) -> None:
    """Run a module's own migration chain by convention (from its env.py).

    See :func:`sandloom.core.migrations.alembic_support.run_module_migrations`.
    """
    from sandloom.core.migrations.alembic_support import (  # noqa: PLC0415 - cold: package boundary - importing any sandloom.core.migrations submodule (urls is stdlib-only) must not load Alembic; a module env.py enters here
        run_module_migrations as _run,
    )

    _run(
        env_file,
        package=package,
        model_modules=model_modules,
        table_prefix=table_prefix,
        table_names=table_names,
    )


__all__ = ["run_module_migrations"]
