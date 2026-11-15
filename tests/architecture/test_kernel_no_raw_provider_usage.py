"""Ratchet: the kernel never reaches under a provider's Protocol (ADR-0016).

``test_no_raw_provider_usage.py`` keeps the escape hatches out of business
modules. This test holds the KERNEL (``src/sandloom/core`` outside
``core/drivers`` and the kernel's own unit tests) to the same line, in two
rules:

1. **No escape hatch.** No ``raw_client()``, ``raw_backend()``, ``raw()``,
   ``redis_client()`` or ``native()`` call, and no reflective read of those names. A kernel that
   holds a backend SDK object cannot run with another driver.
2. **No undeclared probe.** No ``getattr(x, "name")`` / ``hasattr(x, "name")``
   where either
   (a) ``name`` is a public member that some driver class in ``core/drivers``
   defines and no capability Protocol in ``sandloom.core.infra.ports``
   declares (``verify_tenant_rls``, ...): caught whatever the receiver is
   called, so ``hasattr(p, "verify_tenant_rls")`` counts like
   ``hasattr(provider, ...)``; or
   (b) ``x`` is a provider-like value (a name or attribute ``provider``,
   ``bus``, ``cache``, ``backend``, ``driver``, ``transport``, ``taskqueue``,
   ``objectstore``, ``notifier``) and ``name`` is not a Protocol member.
   An attribute a driver may or may not have is a feature the Protocol
   declares (``features``), not a probe. A probe whose name is not a literal
   is counted as ``<dynamic>`` (only on a provider-like receiver: with a
   computed name the receiver is all there is to go by). NOT caught: a probe
   of a name no driver class defines, on a receiver with another name.

Both rules carry seeded, shrink-only allowlists (below): a site that is not
listed, or more sites than listed, fails; a stale entry does NOT fail, so the
slice that removes a site never edits this file. ``idk-ratchets-close`` prunes
the lists and makes the test strict. An entry is keyed by file and name, never
by line, and names ``removed_by``, the slice that removes it.
"""

from __future__ import annotations

import ast
import re
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
    # the test harness flushes the Redis cache driver's own database
    ("core/testing/fixtures.py", "redis_client"): (1, "idk-ratchets-close"),
}

#: (file relative to src/sandloom, probed name) -> (sites, removed_by).
#: ``idk-ratchets-close`` classifies the rest: a ``<dynamic>`` probe of a
#: module's contract provider (not an infra provider) is reclassified or
#: justified there.
_NOT_A_PROVIDER = (
    "stays: reads a module descriptor attribute, not a provider (the name collides "
    "with a driver member; justified in idk-ratchets-close)"
)
PROBE_ALLOWLIST: dict[tuple[str, str], tuple[int, str]] = {
    ("core/admin/api/platform.py", "storage"): (1, _NOT_A_PROVIDER),
    ("core/cli/doctor.py", "verify_tenant_rls"): (1, "idk-ratchets-close"),
    ("core/cli/generated.py", "<dynamic>"): (2, "idk-ratchets-close"),
    ("core/di.py", "_sandloom_read_only_uow_factory"): (
        1,
        "stays: an optional contract-pool unit factory of the SQL driver, probed by name (justified in idk-ratchets-close)",
    ),
    ("core/facade/_registries.py", "contract_uow_factory"): (
        1,
        "stays: an optional contract-pool unit factory of the SQL driver, probed by name (justified in idk-ratchets-close)",
    ),
    ("core/facade/_registries.py", "projection"): (1, _NOT_A_PROVIDER),
    ("core/infra/registry.py", "<dynamic>"): (
        1,
        "stays: the registry checks a driver against its Protocol by name (justified in idk-ratchets-close)",
    ),
    ("core/loader/manifest.py", "storage"): (2, _NOT_A_PROVIDER),
    ("core/module_api/_bootstrap.py", "contract_read_uow_factory"): (
        1,
        "stays: an optional contract-pool unit factory of the SQL driver, probed by name (justified in idk-ratchets-close)",
    ),
    ("core/production_profile.py", "tenant_rls_coverage"): (1, "idk-ratchets-close"),
    ("core/sagas/runtime.py", "<dynamic>"): (1, "idk-ratchets-close"),
    ("core/sagas/runtime.py", "session_factory"): (1, "idk-ratchets-close"),
    ("core/tasks/pool_metrics.py", "pool_stats"): (1, "idk-ratchets-close"),
    ("core/testing/fixtures.py", "_contract_engine"): (1, "idk-ratchets-close"),
    ("core/testing/fixtures.py", "_engine"): (1, "idk-ratchets-close"),
    ("core/testing/fixtures.py", "redis_client"): (1, "idk-ratchets-close"),
    ("core/worker.py", "verify_tenant_rls"): (1, "idk-ratchets-close"),
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


def driver_only_members() -> frozenset[str]:
    """Public names a driver's provider class defines that no Protocol declares.

    A provider class is a class in ``core/drivers`` named ``*Provider`` (the
    class that implements a capability; option models, adapters and value types
    carry generic names such as ``path`` or ``host``). These names are the
    backend-specific extras (``verify_tenant_rls``, ...): a kernel ``getattr`` /
    ``hasattr`` of one of them is a probe whatever the receiver is called, so
    renaming ``provider`` to ``p`` hides nothing.
    """
    names: set[str] = set()
    for path in DRIVERS.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.ClassDef) and node.name.endswith("Provider")):
                continue
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.add(item.name)
                elif isinstance(item, ast.AnnAssign) and isinstance(
                    item.target, ast.Name
                ):
                    names.add(item.target.id)
                elif isinstance(item, ast.Assign):
                    names.update(t.id for t in item.targets if isinstance(t, ast.Name))
    return frozenset(n for n in names if not n.startswith("_")) - declared_members()


def _receiver_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id.lstrip("_")
    if isinstance(node, ast.Attribute):
        return node.attr.lstrip("_")
    return None


def probe_sites(
    path: Path,
    declared: frozenset[str],
    driver_only: frozenset[str] = frozenset(),
) -> Counter[str]:
    sites: Counter[str] = Counter()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr"}
            and len(node.args) >= 2
        ):
            continue
        provider_like = _receiver_name(node.args[0]) in PROVIDER_NAMES
        attr = node.args[1]
        if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
            if provider_like:  # the name is not a literal: only the receiver tells
                sites["<dynamic>"] += 1
        elif attr.value.startswith("__"):
            continue
        elif attr.value in driver_only or (
            provider_like and attr.value not in declared
        ):
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
    driver_only = driver_only_members()
    offences = new_offences(
        _scan(lambda p: probe_sites(p, declared, driver_only)), PROBE_ALLOWLIST
    )
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
            assert sites > 0 and name.strip(), (rel, name)
            assert re.fullmatch(r"idk-[a-z0-9-]+", removed_by) or (
                removed_by.startswith("stays:") and removed_by[len("stays:") :].strip()
            ), (rel, name, removed_by)
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
        "    h = await c.redis_client()\n"
        "    i = getattr(c, 'redis_client', None)\n"
    )
    assert dict(escape_hatch_sites(sample)) == {
        "raw_client": 2,
        "raw_backend": 1,
        "raw": 1,
        "native": 1,
        "redis_client": 2,
    }


def test_the_scanner_finds_an_undeclared_probe_and_spares_a_declared_one(
    tmp_path: Path,
) -> None:
    declared = declared_members()
    assert {"features", "driver", "get_many", "windowed_counter"} <= declared
    assert "raw_client" not in declared
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


def test_a_driver_only_probe_is_caught_whatever_the_receiver_is_called(
    tmp_path: Path,
) -> None:
    """``hasattr(p, "verify_tenant_rls")`` (worker.py) must count like
    ``hasattr(provider, ...)``: the rule keys on the attribute name."""
    declared = declared_members()
    driver_only = driver_only_members()
    assert {"verify_tenant_rls", "pool_stats"} <= driver_only
    assert not driver_only & declared
    sample = tmp_path / "probe.py"
    sample.write_text(
        "def f(p, q, obj, x):\n"
        "    a = hasattr(p, 'verify_tenant_rls')\n"
        "    b = getattr(q, 'pool_stats', None)\n"
        "    c = getattr(obj.inner, 'verify_tenant_rls')\n"
        "    d = hasattr(p, 'features')\n"  # declared: fine
        "    e = hasattr(p, 'unrelated_name')\n"  # not a driver member, other receiver
        "    g = getattr(p, x)\n"  # dynamic on a non-provider receiver: not counted
    )
    assert dict(probe_sites(sample, declared, driver_only)) == {
        "verify_tenant_rls": 2,
        "pool_stats": 1,
    }
    # the same file without the driver-member table sees none of them: the
    # receiver-name rule alone is what the first version had
    assert dict(probe_sites(sample, declared)) == {}


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
