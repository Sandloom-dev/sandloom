"""The repository state (``dev.json``) and the machine's port registry.

``<state>/dev.json`` (written under the repository lock): the repository it
was made for, the compose project name, the four host ports, and one row per
checkout using the servers (its database, its two dev Redis indexes, its last
``up``), the app port of each checkout (``dev run``'s default) and when
``down`` last stopped the services. ``~/.sandloom/ports.json`` (written under
the machine lock): every
port any repository on this machine reserved, with that repository's path.
Both are written atomically (a temp file, then ``os.replace``).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATE_FILE = "dev.json"
REGISTRY_FILE = "ports.json"
STATE_VERSION = 1


def atomic_write(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` atomically; ``mode`` sets the permissions
    (else the existing file's, else 0644). A symlinked ``path`` is written
    through the link (its target is replaced, the link stays)."""
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = target.stat().st_mode & 0o7777
        except FileNotFoundError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


@dataclass
class CheckoutRow:
    """One checkout using the repository's servers."""

    path: str
    database: str
    cache_index: int
    broker_index: int
    test_database: str
    last_up: float = 0.0

    @property
    def indexes(self) -> tuple[int, int]:
        return (self.cache_index, self.broker_index)


@dataclass
class State:
    repository: str
    project: str
    machine: str
    ports: dict[str, int] = field(default_factory=dict)
    checkouts: dict[str, CheckoutRow] = field(default_factory=dict)
    compose_sha: str = ""
    version: int = STATE_VERSION
    #: Checkout path -> the port its ``dev run`` serves on (sticky, chosen
    #: by the first ``dev up``). Kept apart from the rows: a CLI that does
    #: not know it still reads the file.
    app_ports: dict[str, int] = field(default_factory=dict)
    #: When ``dev down`` last stopped the services (epoch seconds).
    stopped_at: float = 0.0

    def to_json(self) -> str:
        data = asdict(self)
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> State:
        rows = {
            path: CheckoutRow(**row)
            for path, row in (data.get("checkouts") or {}).items()
        }
        return cls(
            repository=str(data.get("repository") or ""),
            project=str(data.get("project") or ""),
            machine=str(data.get("machine") or ""),
            ports={k: int(v) for k, v in (data.get("ports") or {}).items()},
            checkouts=rows,
            compose_sha=str(data.get("compose_sha") or ""),
            version=int(data.get("version") or STATE_VERSION),
            app_ports={
                str(k): int(v) for k, v in (data.get("app_ports") or {}).items()
            },
            stopped_at=float(data.get("stopped_at") or 0.0),
        )

    def used_indexes(self, *, besides: str | None = None) -> set[int]:
        out: set[int] = set()
        for path, row in self.checkouts.items():
            if path != besides:
                out.update(row.indexes)
        return out


def load_state(state_dir: Path) -> State | None:
    path = state_dir / STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        return None
    return State.from_dict(data) if isinstance(data, dict) else None


def save_state(state_dir: Path, state: State) -> None:
    atomic_write(state_dir / STATE_FILE, state.to_json())


def load_registry(home: Path) -> dict[int, str]:
    """``{port: repository path}`` of every reserved port on this machine."""
    try:
        data = json.loads((home / REGISTRY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ports = data.get("ports") if isinstance(data, dict) else None
    if not isinstance(ports, dict):
        return {}
    out: dict[int, str] = {}
    for port, owner in ports.items():
        with contextlib.suppress(TypeError, ValueError):
            out[int(port)] = str(owner)
    return out


def save_registry(home: Path, registry: dict[int, str]) -> None:
    data = {"ports": {str(p): owner for p, owner in sorted(registry.items())}}
    atomic_write(home / REGISTRY_FILE, json.dumps(data, indent=2) + "\n")


__all__ = [
    "REGISTRY_FILE",
    "STATE_FILE",
    "CheckoutRow",
    "State",
    "atomic_write",
    "load_registry",
    "load_state",
    "save_registry",
    "save_state",
]
