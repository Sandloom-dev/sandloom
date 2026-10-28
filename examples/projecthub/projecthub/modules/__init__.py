"""Business modules for the projecthub application.

Each module is atomic and isolated: it imports only ``sandloom.sdk`` (plus the
generated contract stubs of ``projecthub.shared``) and its own package, and
talks to other modules through typed events and contracts — never by direct
import.
"""
