"""Celery driver for Sandloom.

Three modules: ``taskqueue`` (the ``celery`` taskqueue driver), ``worker`` (its
worker and scheduler host, and the importable Celery app of
``celery -A sandloom_celery.worker``) and ``broker_probe`` (the bounded broker
connection behind ``health()``). The driver registers through the
``sandloom.drivers`` entry-point group and imports only ``sandloom.driverkit``;
Celery loads when a provider is built, not at import.
"""
