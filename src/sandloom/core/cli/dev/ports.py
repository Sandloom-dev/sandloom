"""The four host ports of a repository's dev services.

Chosen once per repository, under the machine lock, from 20000-32767 (below
every OS's ephemeral range): the search starts at an offset derived from the
repository's project name and steps by 257, skips every port the machine
registry already gives a repository, and test-binds each candidate on
``127.0.0.1`` and ``::1``. The ports are sticky: the next ``up`` keeps them,
unless something else took one while the services were down.
"""

from __future__ import annotations

import errno
import hashlib
import socket
from collections.abc import Callable, Iterable

#: Service -> (port variable of the compose file, container port).
SERVICES: dict[str, tuple[str, int]] = {
    "db": ("SANDLOOM_DEV_POSTGRES_PORT", 5432),
    "redis": ("SANDLOOM_DEV_REDIS_PORT", 6379),
    "postgres_test": ("SANDLOOM_DEV_TEST_POSTGRES_PORT", 5432),
    "redis_test": ("SANDLOOM_DEV_TEST_REDIS_PORT", 6379),
}
LOW, HIGH = 20000, 32767
STRIDE = 257
ATTEMPTS = 256


def port_free(port: int) -> bool:
    """Whether ``port`` can be bound on 127.0.0.1 and on ::1 (when the host
    has IPv6). A port free on one loopback address only is not free:
    ``localhost`` may resolve to the other.

    A port whose only use is closed connections in TIME_WAIT (the app's
    connections to a database that ``dev down`` just stopped) refuses a
    plain bind for half a minute, yet nothing listens there: it is free
    (:func:`_listening` tells the two apart)."""
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue  # no IPv6 on this host
        try:
            sock.bind((host, port))
        except OSError as exc:
            if exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
                continue  # ::1 not configured
            if exc.errno == errno.EADDRINUSE and not _listening(family, host, port):
                continue  # only TIME_WAIT leftovers
            return False
        finally:
            sock.close()
    return True


def _listening(family: int, host: str, port: int) -> bool:
    """Whether something accepts connections at ``host``:``port`` (a refused
    connection means nobody listens; anything else counts as taken)."""
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect((host, port))
    except ConnectionRefusedError:
        return False
    except OSError:
        return True
    return True


def candidates(seed: str) -> Iterable[int]:
    """Ports to try for ``seed``, in order."""
    span = HIGH - LOW + 1
    start = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % span
    for n in range(ATTEMPTS * len(SERVICES)):
        yield LOW + (start + n * STRIDE) % span


def choose_ports(
    seed: str,
    services: Iterable[str],
    *,
    reserved: Iterable[int] = (),
    is_free: Callable[[int], bool] = port_free,
) -> dict[str, int]:
    """A free port for each of ``services`` (raises ``RuntimeError`` when the
    range has none left)."""
    taken = set(reserved)
    out: dict[str, int] = {}
    wanted = list(services)
    for port in candidates(seed):
        if len(out) == len(wanted):
            break
        if port in taken or not is_free(port):
            continue
        out[wanted[len(out)]] = port
        taken.add(port)
    if len(out) < len(wanted):
        raise RuntimeError(
            f"no free port left in {LOW}-{HIGH} for {', '.join(wanted[len(out) :])}"
        )
    return out


__all__ = ["HIGH", "LOW", "SERVICES", "candidates", "choose_ports", "port_free"]
