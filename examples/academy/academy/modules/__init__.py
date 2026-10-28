"""Business modules for the academy application.

Each module is atomic and isolated: it imports only ``sandloom.sdk`` (plus the
generated contract stubs of ``academy.shared``) and its own package, and
talks to other modules through typed contracts and sagas — never by direct
import.
"""
