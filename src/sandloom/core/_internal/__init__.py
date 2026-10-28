"""Framework-internal core modules — not a public API surface.

Nothing here is importable by business modules (an architecture test enforces
it). These modules exist for the composition root and for tests that override
raw dependencies via ``app.dependency_overrides``.
"""
