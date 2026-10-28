"""Business modules for the helpdesk application.

Each module is atomic and isolated: it imports only ``sandloom.sdk`` (plus the
generated contract stubs and DTOs of ``helpdesk.shared``) and its own package,
and talks to other modules through typed events and contracts — never by
direct import.
"""
