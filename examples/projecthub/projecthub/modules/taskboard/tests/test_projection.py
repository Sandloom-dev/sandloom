"""End-to-end taskboard projection test.

Drives task/project events through the real projection consumers (durable claim
+ version-guarded upsert) and asserts the cross-module read model converges and
is served by one tenant-scoped query — including duplicate and out-of-order
deliveries. The projects read contract is faked (``fake_contract``) so the
test is hermetic.
"""

import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from projecthub.modules.taskboard.consumers import (
    PROJECTION_NAME,
    on_project_updated,
    on_task_upserted,
)
from projecthub.modules.taskboard.services import TaskboardQueryService
from projecthub.shared.contracts.projects_read_v1 import PROJECTS_READ_V1
from projecthub.shared.contracts.tasks_read_v1 import TASKS_READ_V1

# ``db_engine`` (sandloom.sdk.testing plugin) builds the schema including the core
# consumer-inbox table the durable consumers claim into.
from sandloom.sdk.projections import (  # sandloom: advanced drive the L2 projection consumers
    ProjectionConsumerDescriptor,
    make_consumer_callback,
)
from sandloom.sdk.tenant import (  # sandloom: advanced test inspection
    bind_tenant,
    unscoped,
)
from sandloom.sdk.testing import fake_contract


class _StubProjectsRead:
    """Stands in for the ``projects.read.v1`` contract."""

    def __init__(self, projects: dict[str, dict]) -> None:
        self._projects = projects

    async def get(self, project_id: uuid.UUID) -> dict | None:
        return self._projects.get(str(project_id))


def _envelope(event_id: str, channel: str, payload: dict) -> dict:
    return {"id": event_id, "channel": channel, "payload": payload}


@pytest_asyncio.fixture
async def callbacks(db_engine: AsyncEngine):
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def opener(_name: str):
        async with sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    project_id = uuid.uuid4()
    projects = {
        str(project_id): {
            "project_id": str(project_id),
            "tenant_id": "",  # filled by the test
            "name": "Dev Platform",
            "status": "active",
            "version": 1,
        }
    }
    task_cb = make_consumer_callback(
        ProjectionConsumerDescriptor(
            name="test.taskboard@task",
            source_channel="tasks.task.updated.v1",
            projection=PROJECTION_NAME,
            handler=on_task_upserted,
            bind_tenant_from="tenant_id",  # as the module registers it
        ),
        "default",
        opener,
    )
    project_cb = make_consumer_callback(
        ProjectionConsumerDescriptor(
            name="test.taskboard@project",
            source_channel="projects.project.updated.v1",
            projection=PROJECTION_NAME,
            handler=on_project_updated,
            bind_tenant_from="tenant_id",
        ),
        "default",
        opener,
    )
    with fake_contract(PROJECTS_READ_V1, _StubProjectsRead(projects)):
        yield task_cb, project_cb, sessionmaker, project_id


@pytest.mark.asyncio
async def test_projection_converges_and_is_served_by_one_query(callbacks) -> None:
    task_cb, project_cb, sessionmaker, project_id = callbacks
    tenant_id = uuid.uuid4()
    task_id = uuid.uuid4()

    def task_payload(version: int, status: str) -> dict:
        return {
            "task_id": str(task_id),
            "tenant_id": str(tenant_id),
            "project_id": str(project_id),
            "title": "Ship it",
            "status": status,
            "version": version,
        }

    # 1) Task created (v1) — denormalises current project fields via the contract.
    await task_cb(_envelope("t1", "tasks.task.updated.v1", task_payload(1, "active")))
    # 2) Project renamed (v5) — fans out across existing task rows.
    await project_cb(
        _envelope(
            "p5",
            "projects.project.updated.v1",
            {
                "project_id": str(project_id),
                "tenant_id": str(tenant_id),
                "name": "Dev Platform 2",
                "status": "active",
                "version": 5,
            },
        )
    )
    # 3) Task advanced to done (v3).
    await task_cb(_envelope("t3", "tasks.task.updated.v1", task_payload(3, "done")))
    # 4) Duplicate of (3): same event id — skipped by the durable inbox.
    await task_cb(_envelope("t3", "tasks.task.updated.v1", task_payload(3, "done")))
    # 5) Out-of-order older task event (v2) — must NOT roll the row back.
    await task_cb(_envelope("t2", "tasks.task.updated.v1", task_payload(2, "active")))

    # Served by ONE tenant-scoped query — no runtime cross-module join.
    with bind_tenant(tenant_id):
        async with sessionmaker() as session:
            rows = await TaskboardQueryService.find(
                session,
                tenant_id=tenant_id,
                task_status="done",
                project_status="active",
                project_prefix="Dev",
            )
    assert len(rows) == 1
    row = rows[0]
    assert row.task_id == task_id
    assert row.task_status == "done"
    assert row.task_version == 3
    assert row.project_name == "Dev Platform 2"
    assert row.project_version == 5


# --------------------------------------------------------------------------- #
# M2 — concurrent task/project deliveries never strand stale project fields
# --------------------------------------------------------------------------- #
def _project_payload(project_id: uuid.UUID, tenant_id: uuid.UUID, version: int) -> dict:
    return {
        "project_id": str(project_id),
        "tenant_id": str(tenant_id),
        "name": f"Platform v{version}",
        "status": "active" if version < 3 else "archived",
        "version": version,
    }


def _task_payload(task_id, tenant_id, project_id, version: int = 1) -> dict:  # noqa: ANN001
    return {
        "task_id": str(task_id),
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "title": "Ship it",
        "status": "active",
        "version": version,
    }


async def _row(sessionmaker, task_id):  # noqa: ANN001, ANN202
    from projecthub.modules.taskboard.models import ProjectTaskQueryIndex

    async with sessionmaker() as session:
        with unscoped():  # test inspection across tenants
            return await session.get(ProjectTaskQueryIndex, task_id)


@pytest.mark.asyncio
async def test_project_update_racing_a_task_insert_is_not_lost(
    db_engine: AsyncEngine,
) -> None:
    """The review's race: the task handler reads the project over RPC (v1),
    the project v2 event commits meanwhile (its fan-out UPDATE matches no row
    yet), then the task row is inserted with v1 — stuck stale forever."""
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    tenant_id, project_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with bind_tenant(tenant_id):  # what bind_tenant_from does

        @asynccontextmanager
        async def opener(_name: str):
            async with sessionmaker() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        on_project = on_project_updated

        class _RacingProjectsRead:
            async def get(self, _pid: uuid.UUID) -> dict:
                # The project v2 event is applied AND committed while the task
                # handler is between its RPC read and its insert.
                async with opener("default") as session:
                    await on_project(
                        session,
                        _envelope(
                            "p2",
                            "projects.project.updated.v1",
                            _project_payload(project_id, tenant_id, 2),
                        ),
                    )
                return _project_payload(project_id, tenant_id, 1)  # stale read

        with fake_contract(PROJECTS_READ_V1, _RacingProjectsRead()):
            async with opener("default") as session:
                await on_task_upserted(
                    session,
                    _envelope(
                        "t1",
                        "tasks.task.created.v1",
                        _task_payload(task_id, tenant_id, project_id),
                    ),
                )

        row = await _row(sessionmaker, task_id)
        assert row is not None
        assert (row.project_name, row.project_version) == ("Platform v2", 2)


@pytest.mark.asyncio
async def test_project_update_waits_for_an_uncommitted_task_insert(
    db_engine: AsyncEngine,
) -> None:
    """Opposite interleaving: the task row is inserted but NOT committed when
    the project event runs; the project handler must serialise behind it and
    then refresh the (now committed) row."""
    import asyncio

    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    tenant_id, project_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with bind_tenant(tenant_id):  # what bind_tenant_from does
        projects = _StubProjectsRead(
            {str(project_id): _project_payload(project_id, tenant_id, 1)}
        )
        on_project = on_project_updated

        async def _project_v2() -> None:
            async with sessionmaker() as session:
                await on_project(
                    session,
                    _envelope(
                        "p2",
                        "projects.project.updated.v1",
                        _project_payload(project_id, tenant_id, 2),
                    ),
                )
                await session.commit()

        task_session = sessionmaker()
        project_task: asyncio.Task[None] | None = None
        try:
            with fake_contract(PROJECTS_READ_V1, projects):
                await on_task_upserted(
                    task_session,
                    _envelope(
                        "t1",
                        "tasks.task.created.v1",
                        _task_payload(task_id, tenant_id, project_id),
                    ),
                )  # inserted, not committed
            project_task = asyncio.create_task(_project_v2())
            await asyncio.sleep(0.3)
            waited = not project_task.done()
            await task_session.commit()
        finally:
            await task_session.close()
            if project_task is not None:
                await asyncio.wait_for(project_task, timeout=5)
        assert waited, "project handler did not serialise behind the task tx"

        row = await _row(sessionmaker, task_id)
        assert (row.project_name, row.project_version) == ("Platform v2", 2)


@pytest.mark.asyncio
async def test_rebuild_refreshes_stale_project_fields(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row already stuck with stale project data is repaired by a rebuild
    (project fields used to be insert-only, so rebuild never touched them)."""
    from sqlalchemy import insert

    from projecthub.modules.taskboard import rebuilder as rebuilder_mod
    from projecthub.modules.taskboard.models import ProjectTaskQueryIndex
    from projecthub.modules.taskboard.rebuilder import TaskboardRebuilder

    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    tenant_id, project_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with unscoped():  # seed a stale row
        async with sessionmaker() as session:
            await session.execute(
                insert(ProjectTaskQueryIndex).values(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    task_title="Ship it",
                    task_status="active",
                    task_version=1,
                    project_id=project_id,
                    project_name="Platform v1",
                    project_status="active",
                    project_version=1,
                )
            )
            await session.commit()

    @asynccontextmanager
    async def _open_uow(_database: str | None = None):
        async with sessionmaker() as session:
            yield session
            await session.commit()

    monkeypatch.setattr(rebuilder_mod, "open_uow", _open_uow)

    class _Read:
        async def list_for_tenant(self, _tenant: uuid.UUID) -> list[dict]:
            return [_project_payload(project_id, tenant_id, 3)]

    class _Tasks:
        async def list_for_tenant(self, _tenant: uuid.UUID) -> list[dict]:
            return [_task_payload(task_id, tenant_id, project_id, version=1)]

    with (
        fake_contract(PROJECTS_READ_V1, _Read()),
        fake_contract(TASKS_READ_V1, _Tasks()),
    ):
        result = await TaskboardRebuilder().rebuild_tenant(tenant_id)
    assert result.rows == 1
    row = await _row(sessionmaker, task_id)
    assert (row.project_name, row.project_status, row.project_version) == (
        "Platform v3",
        "archived",
        3,
    )
