"""A boot is quiet at INFO: the framework's startup chatter is DEBUG.

``sandloom admin create``, the wizard's steps and ``sandloom dev run`` print
what the framework logs at INFO. Module loading, built drivers, the planner's
choices, a first boot's missing schema file and the pool's dispose/recreate
are DEBUG (``LOG_LEVEL=DEBUG`` or ``sandloom --debug`` shows them); warnings
and errors stay.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest

from sandloom.core import app_factory
from sandloom.core.config.settings import settings
from sandloom.core.descriptors import HealthResult
from sandloom.core.drivers.linger_pool import LingeringQueuePool
from sandloom.core.tests._root_support import create_root_app

#: What a first run printed before the password prompt and under the spinner.
CHATTER = re.compile(
    r"\[plugin_loader\]|\[infra\] built|module registered|"
    r"Schema registry persistence file not found|declared operation|"
    r"Pool disposed|Pool recreating"
)


@pytest.mark.asyncio
async def test_a_boot_logs_no_startup_chatter_at_info(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    with caplog.at_level(logging.INFO):
        app = create_root_app()
        async with app.router.lifespan_context(app):
            pass
    loud = [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelno >= logging.INFO and CHATTER.search(r.getMessage())
    ]
    assert loud == []
    # LOG_LEVEL=DEBUG (what `sandloom --debug` sets) brings them back.
    monkeypatch.setattr(settings, "log_level", "DEBUG")
    with caplog.at_level(logging.DEBUG):
        caplog.clear()
        create_root_app()
    assert any("[plugin_loader]" in r.getMessage() for r in caplog.records)


def test_the_pool_logs_where_sqlalchemys_pools_do() -> None:
    """SQLAlchemy keeps its own loggers at WARNING; the pool subclass logs
    under them, so its dispose/recreate lines are as quiet as the stock pool's."""
    assert LingeringQueuePool.__module__.startswith("sandloom.")
    assert getattr(LingeringQueuePool, "_sqla_logger_namespace", "").startswith(
        "sqlalchemy.pool"
    )


class _Database:
    def __init__(self, status: str) -> None:
        self.status = status

    async def health(self) -> HealthResult:
        return HealthResult(status=self.status, details={"error": "refused"})


class _Core:
    def __init__(self, status: str) -> None:
        self.database = _Database(status)

    def capability(self, capability: str, name: str = "default") -> Any:
        assert (capability, name) == ("database", "default")
        return self.database


@pytest.mark.asyncio
async def test_dev_reports_an_unreachable_database_in_one_line(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopped dev services: the app says so once, with the fix, instead of
    booting half-alive with tracebacks."""
    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://u:secret@127.0.0.1:23470/shop"
    )
    with caplog.at_level(logging.WARNING):
        await app_factory.report_unreachable_database(_Core("fail"))  # type: ignore[arg-type]
    (record,) = caplog.records
    message = record.getMessage()
    assert "PostgreSQL at 127.0.0.1:23470 fails its health check" in message
    assert "uv run sandloom dev up" in message and "SANDLOOM-R014" in message
    assert "DATABASE_URL" in message  # it may be a server dev does not run
    assert "secret" not in message and record.exc_info is None
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await app_factory.report_unreachable_database(_Core("ok"))  # type: ignore[arg-type]
    assert caplog.records == []
