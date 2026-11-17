"""Parallel test isolation for a driver whose options hold a URL.

A driver states what one pytest-xdist worker's instance uses through the
classmethod ``test_isolation(options, worker_id) -> options`` of its provider
(``InfraProvider``; the default returns the options unchanged, right for
``memory``). A driver whose options carry a URL of a backend with numbered or
named databases returns them with that URL moved for the worker::

    from sandloom.driverkit.testing_isolation import isolated_url_options

    class MyProvider(CacheProvider):
        @classmethod
        def test_isolation(cls, options, worker_id):
            return isolated_url_options(options, worker_id, keys=("url",))

``keys`` names the options holding such a URL (``("broker", "backend")`` for a
task queue). A ``worker_id`` that is not ``gw<N>`` returns the options as given.

How a URL moves is the backend's harness family's (``sandloom.driverkit.
testing_family``), not the driver's and not the kernel's: the family computes
its layout from its own configuration and installs its *mover* with
:func:`set_url_mover` (keyed by the family's name; each family has one and the
hook applies every installed one in turn) before the harness calls a hook, so a
driver reads no environment. A mover returns a URL its family does not own unchanged, so a
driver whose option may hold a URL of another backend is safe to call. The
harness calls the hook of every configured instance of a driver; a driver
package needs no pytest plugin for it.
"""

from sandloom.core.infra.worker_isolation import isolated_url_options, set_url_mover

__all__ = [
    "isolated_url_options",
    "set_url_mover",
]
