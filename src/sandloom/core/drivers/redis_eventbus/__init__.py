"""The Redis Streams transport of the ``eventbus`` capability (spec 7).

``streams_transport`` holds ``RedisStreamsTransport``, the ``streams``
``EventTransportProvider``'s transport; ``streams_consume``, ``streams_groups``
and ``streams_transport_base`` are its consume half, its consumer-group half and
its shared constants. The kernel's durable bus logic above the transport
(``streams_bus``, ``streams_dispatch``, ``streams_inventory``, ``streams_lease``)
stays in :mod:`sandloom.core.eventing`.
"""
