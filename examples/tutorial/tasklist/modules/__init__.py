"""Business modules for the tasklist application.

Each module is atomic and isolated: it imports only ``sandloom.sdk`` (plus the
generated contract stubs and DTOs of ``tasklist.shared``) and its own package,
and talks to other modules through typed events and contracts.
"""
