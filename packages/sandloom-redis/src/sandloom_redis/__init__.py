"""Redis drivers for Sandloom.

One module per capability: ``atomic_store``, ``lease`` and ``broadcast``. Each
registers through the ``sandloom.drivers`` entry-point group and imports only
``sandloom.driverkit``; redis-py loads when a driver runs, not at import.
"""
