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

Both rules carry exact allowlists (below, strict since ``idk-ratchets-close``): a
site that is not listed, or more sites than listed, fails, and so does a stale
entry (fewer sites than listed, or none): the change that removes a site
lowers or drops its entry. An entry is keyed by file and name, never by line,
and states why the site stays.
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

#: (file relative to src/sandloom, escape hatch) -> (sites, reason).
RAW_ALLOWLIST: dict[tuple[str, str], tuple[int, str]] = {}

#: (file relative to src/sandloom, probed name) -> (sites, reason). Exact: a
#: site not listed, a higher count and a stale entry all fail.
_NOT_A_PROVIDER = (
    "stays: reads a module descriptor attribute, not a provider (the name collides "
    "with a driver member)"
)
_CONTRACT_IMPL = (
    "stays: reads a method of a module's contract implementation by its contract "
    "name, not an infra provider (the receiver is called `provider`)"
)
_UOW_FACTORY = (
    "stays: an optional contract-pool unit factory of the SQL driver, probed by name"
)
_SQL_EXTRA = (
    "stays: {what} is a member of the SQL database driver that ships in the "
    "kernel (ADR-0016 section 4) and no capability Protocol declares; probed by "
    "name, so a database driver without it is skipped"
)
PROBE_ALLOWLIST: dict[tuple[str, str], tuple[int, str]] = {
    ("core/admin/api/platform.py", "storage"): (1, _NOT_A_PROVIDER),
    ("core/cli/doctor.py", "verify_tenant_rls"): (
        1,
        _SQL_EXTRA.format(what="the Row-Level Security check")
        + " (behind the `tenant_rls` feature)",
    ),
    ("core/cli/generated.py", "<dynamic>"): (2, _CONTRACT_IMPL),
    ("core/di.py", "_sandloom_read_only_uow_factory"): (1, _UOW_FACTORY),
    ("core/facade/_registries.py", "contract_uow_factory"): (1, _UOW_FACTORY),
    ("core/facade/_registries.py", "projection"): (1, _NOT_A_PROVIDER),
    ("core/infra/registry.py", "<dynamic>"): (
        1,
        "stays: the registry checks a driver against its Protocol by name",
    ),
    ("core/loader/manifest.py", "storage"): (2, _NOT_A_PROVIDER),
    ("core/module_api/_bootstrap.py", "contract_read_uow_factory"): (
        1,
        _UOW_FACTORY,
    ),
    ("core/production_profile.py", "tenant_rls_coverage"): (
        1,
        _SQL_EXTRA.format(what="the tenant-table coverage query"),
    ),
    ("core/sagas/runtime.py", "<dynamic>"): (1, _CONTRACT_IMPL),
    ("core/tasks/pool_metrics.py", "pool_stats"): (
        1,
        _SQL_EXTRA.format(what="the connection-pool sample"),
    ),
    ("core/testing/fixtures.py", "_contract_engine"): (
        1,
        "stays: the test harness reaches the SQL driver's private engine to dispose "
        "it between event loops (a SQLAlchemy-only fixture)",
    ),
    ("core/testing/fixtures.py", "_engine"): (
        1,
        "stays: the test harness reaches the SQL driver's private engine to dispose "
        "it between event loops (a SQLAlchemy-only fixture)",
    ),
    ("core/worker.py", "verify_tenant_rls"): (
        1,
        _SQL_EXTRA.format(what="the Row-Level Security check")
        + " (behind the `tenant_rls` feature)",
    ),
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


def driver_sources(
    drivers: Path = DRIVERS, packages: Path = REPO / "packages"
) -> list[Path]:
    """Where provider classes are defined: ``core/drivers/*.py`` and every
    source file of a driver package."""
    return [*drivers.glob("*.py"), *packages.glob("*/src/**/*.py")]


def driver_only_members() -> frozenset[str]:
    """Public names a driver's provider class defines that no Protocol declares.

    A provider class is a class named ``*Provider`` in ``core/drivers`` or in a
    driver package (``packages/*/src``, where the Redis and Celery providers
    live): the class that implements a capability; option models, adapters and
    value types carry generic names such as ``path`` or ``host``. These names are the
    backend-specific extras (``verify_tenant_rls``, ...): a kernel ``getattr`` /
    ``hasattr`` of one of them is a probe whatever the receiver is called, so
    renaming ``provider`` to ``p`` hides nothing.
    """
    names: set[str] = set()
    for path in driver_sources():
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


def stale_entries(
    found: dict[tuple[str, str], int],
    allowed: dict[tuple[str, str], tuple[int, str]],
) -> list[str]:
    """Entries that list more sites than the scan finds (or any, for a gone one)."""
    return [
        f"{rel}: lists {limit} site(s) of {name!r}, the scan finds "
        f"{found.get((rel, name), 0)}"
        for (rel, name), (limit, _reason) in sorted(allowed.items())
        if found.get((rel, name), 0) < limit
    ]


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
        "feature in the Protocol) instead of guessing, or justify the site in "
        "PROBE_ALLOWLIST:\n" + "\n".join(offences)
    )


def test_no_allowlist_entry_is_stale() -> None:
    declared = declared_members()
    driver_only = driver_only_members()
    stale = [
        *stale_entries(_scan(escape_hatch_sites), RAW_ALLOWLIST),
        *stale_entries(
            _scan(lambda p: probe_sites(p, declared, driver_only)), PROBE_ALLOWLIST
        ),
    ]
    assert stale == [], (
        "allowlist entries for sites that are gone: lower or drop them in the "
        "commit that removed the site:\n" + "\n".join(stale)
    )


def test_every_allowlist_entry_says_why_it_stays() -> None:
    for allowed in (RAW_ALLOWLIST, PROBE_ALLOWLIST):
        for (rel, name), (sites, reason) in allowed.items():
            assert (SANDLOOM_ROOT / rel).is_file(), rel
            assert sites > 0 and name.strip(), (rel, name)
            assert reason.startswith("stays:") and reason[len("stays:") :].strip(), (
                rel,
                name,
                reason,
            )
            assert "{" not in reason, (rel, name, "unformatted reason")
            assert not rel.startswith("core/drivers/"), rel
        assert list(allowed) == sorted(allowed), "keep the allowlist sorted"


def test_a_stale_entry_fails_and_an_exact_one_does_not() -> None:
    allowed = {("core/x.py", "pool_stats"): (2, "stays: x")}
    assert stale_entries({("core/x.py", "pool_stats"): 2}, allowed) == []
    assert len(stale_entries({("core/x.py", "pool_stats"): 1}, allowed)) == 1
    assert len(stale_entries({}, allowed)) == 1
    assert new_offences({("core/x.py", "pool_stats"): 3}, allowed) != []
    assert new_offences({("core/y.py", "pool_stats"): 1}, allowed) != []


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


def test_a_provider_class_of_a_driver_package_is_a_driver_only_source(
    tmp_path: Path,
) -> None:
    drivers = tmp_path / "drivers"
    drivers.mkdir()
    package = tmp_path / "packages" / "sandloom-x" / "src" / "sandloom_x" / "sub"
    package.mkdir(parents=True)
    source = package / "thing.py"
    source.write_text("class XProvider:\n    def backend_only_extra(self): ...\n")
    assert driver_sources(drivers, tmp_path / "packages") == [source]
    # The real tree: the Redis and Celery providers are scanned, so a name only
    # they define (``RedisCacheProvider.redis_client``) is a driver-only member.
    real = {p.relative_to(REPO).parts[:2] for p in driver_sources()}
    assert ("packages", "sandloom-redis") in real
    assert ("packages", "sandloom-celery") in real
    assert "redis_client" in driver_only_members()


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
