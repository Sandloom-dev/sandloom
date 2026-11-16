"""Ratchet: a driver that connects to a server by url declares it.

`sandloom dev up` writes the url of each instance whose driver declares a
``url_option`` (``InfraProvider.url_option``); a driver that has a url field in
its options and does not declare it would get no url in the managed ``INFRA`` and
fail startup only at its first use. Every driver of the repository's two driver
packages with a ``url`` or ``broker`` option must declare it, naming a field of
its own ``options_schema``, and the generic reader must return what they declare.

The drivers held to it are the ones whose package sits under this checkout's
``packages/`` (a new driver package joins by existing; a third party's is not
held to it). The option names are matched by word (``url``, ``uri``, ``dsn``,
``endpoint``, ``broker``, also inside ``endpoint_url``): a driver whose url option
is spelled otherwise is invisible to it, and an option that is no server url
(``_NOT_A_SERVER_URL``) is named with its reason, and must still be an option of
its driver that the match sees.

What the ratchet guarantees is the declaration, which ``sandloom dev up`` reads
for any option name. The test harness's overrides (``SANDLOOM_TEST_CACHE_URL`` /
``SANDLOOM_TEST_BROKER_URL``, ``sandloom.core.testing.workers``) replace only the
options ``url`` and ``broker`` (``families.INSTANCE_URL_OPTIONS``) of a
``redis://`` instance: a driver declaring ``dsn`` passes here and gets its url
from ``dev up``, but the harness never replaces it.
"""

from __future__ import annotations

import inspect
import re
from importlib.metadata import entry_points
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel

from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.infra.registry import declared_url_options

_PACKAGES_DIR = Path(__file__).resolve().parents[3] / "packages"
_URL_FIELD = re.compile(r"(^|_)(url|uri|dsn|endpoint|broker)(_|$)")


class _Exemption(NamedTuple):
    """The package under ``packages/`` that holds the driver, and its url-like
    options that name no server the driver needs: stated by the operator or left
    to the SDK's own default (option -> reason)."""

    package: str
    options: dict[str, str]


#: ``"<capability>.<driver>"`` -> its exemption. The option is looked for in the
#: sources of that package only: another package declaring the same name must
#: not keep a dead exemption alive.
_NOT_A_SERVER_URL = {
    "objectstore.s3": _Exemption(
        "sandloom-s3",
        {"endpoint_url": "optional: AWS is the default; S3-compatible only"},
    ),
}


def _package_sources(package: str, packages: Path = _PACKAGES_DIR) -> str:
    """The Python sources of one package under ``packages/``."""
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((packages / package).glob("src/**/*.py"))
    )


def _declares(option: str, sources: str) -> bool:
    """``option`` is declared as a field (``    name: ...``) in ``sources``."""
    return bool(re.search(rf"^\s+{re.escape(option)}\s*:", sources, re.MULTILINE))


def _repository_drivers() -> dict[str, type[InfraProvider]]:
    """``{"<capability>.<driver>": provider class}`` of the drivers whose package
    is under this checkout's ``packages/``."""
    found: dict[str, type[InfraProvider]] = {}
    for entry in entry_points(group="sandloom.drivers"):
        loaded = entry.load()
        if (
            isinstance(loaded, type)
            and issubclass(loaded, InfraProvider)
            and _PACKAGES_DIR in Path(inspect.getfile(loaded)).resolve().parents
        ):
            found[entry.name] = loaded
    return found


def _gaps(drivers: dict[str, type[InfraProvider]]) -> list[str]:
    gaps = []
    for key, cls in sorted(drivers.items()):
        fields = cls.options_schema.model_fields if cls.options_schema else {}
        exempt = _NOT_A_SERVER_URL[key].options if key in _NOT_A_SERVER_URL else {}
        has_field = [
            name for name in fields if _URL_FIELD.search(name) and name not in exempt
        ]
        if has_field and cls.url_option not in has_field:
            gaps.append(
                f"{key}: has the option `{has_field[0]}` but declares "
                f"url_option={cls.url_option!r}"
            )
        if cls.url_option is not None and cls.url_option not in fields:
            gaps.append(f"{key}: url_option {cls.url_option!r} is not an option")
    return gaps


def test_every_repository_url_driver_declares_its_url_option() -> None:
    drivers = _repository_drivers()
    # Checked against the drivers of this repository: with none installed the
    # test would pass over nothing.
    assert {
        "cache.redis",
        "broadcast.redis",
        "lease.redis",
        "atomic_store.redis",
        "eventbus.streams",
        "taskqueue.celery",
    } <= set(drivers)
    assert _gaps(drivers) == []


def test_every_exemption_names_an_option_the_ratchet_would_otherwise_see() -> None:
    """An entry of ``_NOT_A_SERVER_URL`` that no driver option matches any more
    (the option was renamed or removed) is a dead exemption: it fails here.
    The driver need not be installed (``sandloom-s3`` is in the ``packaging``
    group only): the option is looked for as a field in the package sources."""
    for key, exemption in _NOT_A_SERVER_URL.items():
        sources = _package_sources(exemption.package)
        assert sources, f"{key}: package {exemption.package} has no sources"
        for name in exemption.options:
            assert _URL_FIELD.search(name), f"{key}: {name} needs no exemption"
            assert _declares(name, sources), (
                f"{key}: {name} is declared by {exemption.package} no more"
            )


def test_an_exemption_is_checked_against_its_own_package_only(
    tmp_path: Path,
) -> None:
    """Bites on planted sources: a package that declares the option is seen, a
    package that does not is not, even when a sibling package does declare it
    (the case of an exemption kept alive by another package)."""
    declaring = tmp_path / "pkg-a" / "src" / "pkg_a"
    declaring.mkdir(parents=True)
    (declaring / "options.py").write_text(
        "class Options:\n    endpoint_url: str | None = None\n"
    )
    silent = tmp_path / "pkg-b" / "src" / "pkg_b"
    silent.mkdir(parents=True)
    (silent / "options.py").write_text("class Options:\n    bucket: str\n")
    assert _declares("endpoint_url", _package_sources("pkg-a", tmp_path))
    assert not _declares("endpoint_url", _package_sources("pkg-b", tmp_path))
    # a mention that is no field (a comment, a call keyword) is not a declaration
    assert not _declares("endpoint_url", "x = f(endpoint_url=None)  # endpoint_url\n")
    # a package that does not exist has no sources: the test above refuses it
    assert _package_sources("pkg-missing", tmp_path) == ""


def test_the_real_exemptions_name_a_package_that_exists() -> None:
    for key, exemption in _NOT_A_SERVER_URL.items():
        assert (_PACKAGES_DIR / exemption.package).is_dir(), key


def test_the_ratchet_reports_a_driver_that_does_not_declare_it() -> None:
    real = _repository_drivers()

    class Silent(real["cache.redis"]):  # type: ignore[misc, name-defined]
        url_option = None

    class Wrong(real["taskqueue.celery"]):  # type: ignore[misc, name-defined]
        url_option = "url"

    class _DsnOptions(BaseModel):
        dsn: str

    class DsnDriver(real["cache.redis"]):  # type: ignore[misc, name-defined]
        options_schema = _DsnOptions
        url_option = None

    class DsnDeclared(DsnDriver):
        url_option = "dsn"

    # a url option spelled other than `url` / `broker` is seen too
    assert any("cache.dsn" in gap for gap in _gaps({"cache.dsn": DsnDriver}))
    assert _gaps({"cache.dsn": DsnDeclared}) == []
    assert any("cache.redis" in gap for gap in _gaps({"cache.redis": Silent}))
    assert any("taskqueue.celery" in gap for gap in _gaps({"taskqueue.celery": Wrong}))


def test_the_reader_returns_what_the_drivers_declare() -> None:
    selection = {
        "cache": {"default": {"driver": "redis"}},
        "eventbus": {"default": {"driver": "streams"}},
        "taskqueue": {"default": {"driver": "celery"}},
        "lease": {"beat": {"driver": "redis"}},
    }
    assert declared_url_options(selection) == {
        "cache.redis": "url",
        "eventbus.streams": "url",
        "taskqueue.celery": "broker",
        "lease.redis": "url",
    }
