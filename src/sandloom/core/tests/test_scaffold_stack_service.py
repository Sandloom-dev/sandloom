"""The scaffold takes the standard stack's server from its package.

``sandloom new`` and ``sandloom add deploy`` place the compose text, the service
name and the URL forms of the stack's keyed server, as the package that describes
it supplies them (:mod:`sandloom.core.cli.stack_service`); the kernel writes none
of those lines. These tests run the scaffold on a stand-in whose every member is
a marker, so a line the kernel still wrote itself shows. The standard stack's
output, byte for byte: ``tests/integration/with_batteries/
test_scaffold_standard_stack_snapshot.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sandloom.core.cli import project as scaffold_project
from sandloom.core.cli import scaffold_deploy as deploy
from sandloom.core.cli import stack_service as stack
from sandloom.core.tests._fake_stack import FakeStack, install_fake_stack


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeStack:
    fake = install_fake_stack(monkeypatch)
    fake.name = "kvstore"
    fake.container_port = 7001
    fake.local_port = 7002
    fake.ci_port = 7003
    fake.test_cache_index = 9
    fake.test_broker_index = 8
    fake.prod_compose = "  kvstore:\n    image: PROD-MARK"
    fake.prod_volume = "kv_vol"
    fake.ci_service = "      kvstore:\n        image: CI-MARK"
    fake.microservices_infra = '{"cache": {"default": {"url": "MS-INFRA-MARK"}}}'
    fake.microservices_service = "  kvstore:\n    image: MS-SERVICE-MARK"
    fake.microservices_volumes = "  kv_msdata:"
    return fake


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def test_new_writes_the_compose_text_and_urls_the_package_supplies(
    fake: FakeStack, tmp_path: Path
) -> None:
    scaffold_project.generate_project("probe", tmp_path)
    root = tmp_path / "probe"

    prod = _read(root, "deploy/docker-compose.yml")
    assert "image: PROD-MARK" in prod
    assert "kvstore:\n        condition: service_healthy" in prod
    assert "  kv_vol:" in prod
    ci = _read(root, ".github/workflows/ci.yml")
    assert "image: CI-MARK" in ci
    for text in (prod, ci, _read(root, "deploy/docker-compose.dev.yml")):
        assert "redis:7" not in text
        assert "redis_data" not in text
        assert "redis-cli" not in text

    def infra(rel: str) -> dict[str, dict[str, dict[str, str]]]:
        found = re.search(r"INFRA[=:] ?'(\{.*?\})'", _read(root, rel))
        assert found, rel
        parsed: dict[str, dict[str, dict[str, str]]] = json.loads(found.group(1))
        return parsed

    # CI: the published port and the test run's two indexes; the compose
    # profile: the service name and the container port; a person's server: the
    # local port.
    assert infra(".env.test")["cache"]["default"]["url"] == (
        "redis://127.0.0.1:7003/9"
    )
    assert infra(".env.test")["lease"]["beat"]["url"] == "redis://127.0.0.1:7003/8"
    assert infra("deploy/docker-compose.yml")["atomic_store"]["security"]["url"] == (
        "redis://kvstore:7001/2"
    )
    assert infra("README.md")["cache"]["default"]["url"] == "redis://127.0.0.1:7002/0"


def test_deploy_writes_the_microservices_text_the_package_supplies(
    fake: FakeStack, tmp_path: Path
) -> None:
    project = deploy.Project(
        root=tmp_path,
        deploy_dir=tmp_path / "deploy",
        modules_package="probeapp.modules",
        title="Probe",
        build_context=".",
        image="probe",
        port=8000,
        db_prefix="probe",
    )
    deploy.init_files(project)
    text = (tmp_path / "deploy" / deploy.PROFILE).read_text(encoding="utf-8")

    assert "MS-INFRA-MARK" in text
    assert "image: MS-SERVICE-MARK" in text
    assert "  kv_msdata:" in text
    assert "  kvstore:\n    condition: service_started" in text
    assert "redisdata" not in text
    assert "redis:7" not in text

    svc = deploy.Service("orders", ["probeapp.modules.orders"], broker_db=3)
    broker = deploy._service_infra({}, svc)["taskqueue"]["default"]["broker"]
    assert broker == "redis://kvstore:7001/3"
    with pytest.raises(deploy.DeployError, match="reserved"):
        deploy._service_name(["probeapp.modules.orders"], "kvstore")


def test_no_package_is_one_line_and_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(stack, "_discover", stack._discover.__wrapped__)
    monkeypatch.setattr(
        stack.metadata, "entry_points", lambda: stack.metadata.EntryPoints(())
    )
    with pytest.raises(stack.StackServiceError, match="sandloom\\[redis\\]"):
        scaffold_project.generate_project("probe", tmp_path)
    assert not (tmp_path / "probe").exists()
