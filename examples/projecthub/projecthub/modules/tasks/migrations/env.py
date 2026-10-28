"""The module's own migration chain (tables of ``models/``)."""

from sandloom.sdk.migrations import run_module_migrations  # sandloom: advanced migrations

run_module_migrations(__file__)
