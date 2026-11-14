"""Ratchet: the kernel never reaches under a provider's Protocol (ADR-0016).

``test_no_raw_provider_usage.py`` keeps the escape hatches out of business
modules. This test holds the KERNEL (``src/sandloom/core`` outside
``core/drivers`` and the kernel's own unit tests) to the same line, in two
rules:

1. **No escape hatch.** No ``raw_client()``, ``raw_backend()``, ``raw()`` or
   ``native()`` call, and no reflective read of those names. A kernel that
   holds a backend SDK object cannot run with another driver.
2. **No undeclared probe.** No ``getattr(p, "x")`` / ``hasattr(p, "x")`` where
   ``p`` is a provider-like value (a name or attribute ``provider``, ``bus``,
   ``cache``, ``backend``, ``driver``, ``transport``, ``taskqueue``,
   ``objectstore``, ``notifier``) and ``x`` is not a member of a capability
   Protocol in ``sandloom.core.infra.ports``. An attribute a driver may or
   may not have is a feature the Protocol declares (``features``), not a probe.
   A probe whose name is not a literal is counted as ``<dynamic>``.

Both rules carry seeded, shrink-only allowlists (below): a site that is not
listed, or more sites than listed, fails; a stale entry does NOT fail, so the
slice that removes a site never edits this file. ``idk-ratchets-close`` prunes
the lists and makes the test strict. An entry is keyed by file and name, never
by line, and names ``removed_by``, the slice that removes it.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from sandloom.core.archcheck.analyzers import (
    RAW_PROVIDER_METHODS,
    find_raw_provider_calls,
)

REPO = Path(__file__).resolve().parents[2]
SANDLOOM_ROOT = REPO / "src" / "sandloom"
CORE = SANDLOOM_ROOT / "core"
DRIVERS = CORE / "drivers"
PORTS = CORE / "infra" / "ports"

NATIVE_METHODS = frozenset({"native"})
PROVIDER_NAMES = frozenset(
    {
        "provider",
        "bus",
        "cache",
        "backend",
        "driver",
        "transport",
        "taskqueue",
        "objectstore",
        "notifier",
    }
)

#: (file relative to src/sandloom, escape hatch) -> (sites, removed_by).
RAW_ALLOWLIST: dict[tuple[str, str], tuple[int, str]] = {
    ("core/contract_cache_shadow.py", "raw_client"): (3, "idk-broadcast"),
    ("core/testing/fixtures.py", "raw_client"): (1, "idk-test-harness"),
    ("core/worker.py", "raw_backend"): (1, "idk-worker-port"),
}

#: (file relative to src/sandloom, probed name) -> (sites, removed_by).
#: ``idk-ratchets-close`` classifies the rest: a ``<dynamic>`` probe of a
#: module's contract provider (not an infra provider) is reclassified or
#: justified there.
PROBE_ALLOWLIST: dict[tuple[str, str], tuple[int, str]] = {
    ("core/app_factory/revocation_applier.py", "dead_letters"): (
        1,
        "idk-eventbus-contract",
    ),
    ("core/app_factory/revocation_applier.py", "list_dlq"): (
        1,
        "idk-eventbus-contract",
    ),
    ("core/app_factory/revocation_applier.py", "transport"): (
        1,
        "idk-eventbus-contract",
    ),
    ("core/cli/doctor.py", "group_info"): (1, "idk-eventbus-contract"),
    ("core/cli/doctor.py", "verify_tenant_rls"): (1, "idk-db-provider"),
    ("core/cli/extract.py", "list_group_owners"): (1, "idk-eventbus-contract"),
    ("core/cli/extract.py", "transfer_group_owner"): (1, "idk-eventbus-contract"),
    ("core/cli/generated.py", "<dynamic>"): (2, "idk-ratchets-close"),
    ("core/consumers/callbacks.py", "durable"): (1, "idk-eventbus-contract"),
    ("core/consumers/callbacks.py", "in_process"): (1, "idk-eventbus-contract"),
    ("core/consumers/runtime.py", "claim_group_owner"): (1, "idk-eventbus-contract"),
    ("core/di.py", "_sandloom_read_only_uow_factory"): (1, "idk-db-provider"),
    ("core/eventing/transport_capabilities.py", "<dynamic>"): (
        1,
        "idk-eventbus-contract",
    ),
    ("core/eventing/transport_capabilities.py", "consume"): (
        1,
        "idk-eventbus-contract",
    ),
    ("core/eventing/transport_capabilities.py", "ensure_group"): (
        1,
        "idk-eventbus-contract",
    ),
    ("core/facade/_registries.py", "contract_uow_factory"): (1, "idk-db-provider"),
    ("core/infra/registry.py", "<dynamic>"): (
        1,
        "stays: the registry checks a driver against its Protocol by name (justified in idk-ratchets-close)",
    ),
    ("core/module_api/_bootstrap.py", "contract_read_uow_factory"): (
        1,
        "idk-db-provider",
    ),
    ("core/planner.py", "contract_execution_adapter"): (1, "idk-db-provider"),
    ("core/production_profile.py", "tenant_rls_coverage"): (1, "idk-db-provider"),
    ("core/projections/operations.py", "list_dlq"): (1, "idk-event-ops"),
    ("core/sagas/runtime.py", "<dynamic>"): (1, "idk-ratchets-close"),
    ("core/sagas/runtime.py", "session_factory"): (1, "idk-db-provider"),
    ("core/tasks/event_ops.py", "release_group_owner"): (1, "idk-event-ops"),
    ("core/tasks/event_ops.py", "replay_dlq"): (1, "idk-event-ops"),
    ("core/tasks/relay.py", "native_batch_publish"): (1, "idk-eventbus-contract"),
    ("core/tasks/relay.py", "publish_event_batch"): (1, "idk-eventbus-contract"),
    ("core/testing/fixtures.py", "_contract_engine"): (1, "idk-db-provider"),
    ("core/testing/fixtures.py", "_engine"): (1, "idk-db-provider"),
    ("core/unit_of_work/dispatch.py", "in_process"): (1, "idk-eventbus-contract"),
    ("core/unit_of_work/dispatch.py", "publish_event_batch"): (
        1,
        "idk-eventbus-contract",
    ),
    ("core/unit_of_work/dispatch.py", "supports_batch_publish"): (
        1,
        "idk-eventbus-contract",
    ),
    ("core/unit_of_work/dispatch.py", "supports_prevalidated"): (
        1,
        "idk-eventbus-contract",
    ),
    ("core/worker.py", "on_after_finalize"): (1, "idk-worker-port"),
    ("core/worker.py", "task"): (1, "idk-worker-port"),
}


def kernel_files() -> list[Path]:
    return sorted(
        path
        for path in CORE.rglob("*.py")
        if DRIVERS not in path.parents and "tests" not in path.relative_to(CORE).parts
    )


# -- rule 1: escape hatches ----------------------------------------------------


def find_native_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"Line {node.lineno}: .{node.func.attr}(...)"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in NATIVE_METHODS
    ]


def escape_hatch_sites(path: Path) -> Counter[str]:
    sites: Counter[str] = Counter()
    for hit in [*find_raw_provider_calls(path), *find_native_calls(path)]:
        name = next(
            (
                m
                for m in sorted(RAW_PROVIDER_METHODS | NATIVE_METHODS)
                if f"'{m}'" in hit or f".{m}(" in hit
            ),
            "<unknown>",
        )
        sites[name] += 1
    return sites


# -- rule 2: undeclared probes -------------------------------------------------


def declared_members() -> frozenset[str]:
    """Members of every capability Protocol / provider base in the ports."""
    names: set[str] = set()
    for path in PORTS.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef) or not node.bases:
                continue  # a value type (dataclass) is not a provider contract
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.add(item.name)
                elif isinstance(item, ast.AnnAssign) and isinstance(
                    item.target, ast.Name
                ):
                    names.add(item.target.id)
                elif isinstance(item, ast.Assign):
                    names.update(t.id for t in item.targets if isinstance(t, ast.Name))
    return frozenset(names)


def _receiver_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id.lstrip("_")
    if isinstance(node, ast.Attribute):
        return node.attr.lstrip("_")
    return None


def probe_sites(path: Path, declared: frozenset[str]) -> Counter[str]:
    sites: Counter[str] = Counter()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr"}
            and len(node.args) >= 2
            and _receiver_name(node.args[0]) in PROVIDER_NAMES
        ):
            continue
        attr = node.args[1]
        if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
            sites["<dynamic>"] += 1
        elif attr.value not in declared and not attr.value.startswith("__"):
            sites[attr.value] += 1
    return sites


# -- the ratchet ---------------------------------------------------------------


def new_offences(
    found: dict[tuple[str, str], int],
    allowed: dict[tuple[str, str], tuple[int, str]],
) -> list[str]:
    offences: list[str] = []
    for (rel, name), count in sorted(found.items()):
        limit = allowed.get((rel, name), (0, ""))[0]
        if count > limit:
            offences.append(f"{rel}: {count} site(s) of {name!r} (allowed {limit})")
    return offences


def _scan(per_file: object) -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in kernel_files():
        rel = str(path.relative_to(SANDLOOM_ROOT))
        for name, count in per_file(path).items():  # type: ignore[operator]
            found[(rel, name)] = count
    return found


def test_the_kernel_calls_no_escape_hatch() -> None:
    offences = new_offences(_scan(escape_hatch_sites), RAW_ALLOWLIST)
    assert offences == [], (
        "The kernel reaches a backend object through raw_client / raw_backend / "
        "raw / native (ADR-0016). Ask the capability Protocol for the operation; "
        "the allowlist only shrinks:\n" + "\n".join(offences)
    )


def test_the_kernel_probes_no_attribute_its_protocol_does_not_declare() -> None:
    declared = declared_members()
    offences = new_offences(_scan(lambda p: probe_sites(p, declared)), PROBE_ALLOWLIST)
    assert offences == [], (
        "The kernel probes a provider attribute with getattr/hasattr that no "
        "capability Protocol declares (ADR-0016). Declare it (a method or a "
        "feature in the Protocol) instead of guessing; the allowlist only "
        "shrinks:\n" + "\n".join(offences)
    )


def test_every_allowlist_entry_names_its_removal() -> None:
    for allowed in (RAW_ALLOWLIST, PROBE_ALLOWLIST):
        for (rel, name), (sites, removed_by) in allowed.items():
            assert (SANDLOOM_ROOT / rel).is_file(), rel
            assert sites > 0 and removed_by.strip(), (rel, name)
            assert not rel.startswith("core/drivers/"), rel
        assert list(allowed) == sorted(allowed), "keep the allowlist sorted"


# -- self-tests ----------------------------------------------------------------


def test_the_scanner_finds_each_escape_hatch(tmp_path: Path) -> None:
    sample = tmp_path / "leaky.py"
    sample.write_text(
        "async def f(c, p):\n"
        "    a = await c.raw_client()\n"
        "    b = p.raw_backend()\n"
        "    d = c.raw()\n"
        "    e = c.native()\n"
        "    g = getattr(c, 'raw_client')\n"
    )
    assert dict(escape_hatch_sites(sample)) == {
        "raw_client": 2,
        "raw_backend": 1,
        "raw": 1,
        "native": 1,
    }


def test_the_scanner_finds_an_undeclared_probe_and_spares_a_declared_one(
    tmp_path: Path,
) -> None:
    declared = declared_members()
    assert {"features", "driver", "get_many", "raw_client"} <= declared
    assert "native_batch_publish" not in declared
    sample = tmp_path / "probe.py"
    sample.write_text(
        "def f(provider, bus, self, other, name):\n"
        "    a = getattr(provider, 'features', None)\n"
        "    b = getattr(bus, 'native_batch_publish', False)\n"
        "    c = hasattr(self._cache, 'incr_many_fast')\n"
        "    d = getattr(provider, name)\n"
        "    e = getattr(other, 'whatever', None)\n"
        "    f = getattr(provider, '__name__', None)\n"
    )
    assert dict(probe_sites(sample, declared)) == {
        "native_batch_publish": 1,
        "incr_many_fast": 1,
        "<dynamic>": 1,
    }


def test_a_new_site_fails_and_a_stale_entry_does_not() -> None:
    allowed = {("core/x.py", "raw_client"): (2, "idk-x")}
    assert new_offences({("core/x.py", "raw_client"): 2}, allowed) == []
    assert new_offences({("core/x.py", "raw_client"): 1}, allowed) == []
    assert new_offences({}, allowed) == []
    assert len(new_offences({("core/x.py", "raw_client"): 3}, allowed)) == 1
    assert len(new_offences({("core/y.py", "raw_client"): 1}, allowed)) == 1


@pytest.mark.parametrize("rel", ["worker.py", "contract_cache_shadow.py"])
def test_the_scan_covers_the_kernel_files(rel: str) -> None:
    assert (CORE / rel) in kernel_files()
