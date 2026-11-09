"""The application manifest (Public API 1.0, spec §5.7): interface and the
construction from today's loader.

Moved from ``sandloom/core/tests/test_app_manifest.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

from sandloom_standard.main import app

from sandloom.core.loader.manifest import (
    AppManifest,
)


def test_create_app_publishes_the_manifest() -> None:
    manifest: AppManifest = app.state.app_manifest
    summary = app.state.module_summary
    assert [m.name for m in manifest.modules] == [d.name for d in summary.loaded]
    identity = manifest.module("sandloom_identity")
    assert any(r.path.startswith("/api/v1/") for r in identity.routes)
    assert "refresh_tokens" in {m.table for m in identity.models}
