"""Notifications: an outbox of messages to users, behind ``notifications.mail.v1``.

``send`` (``contracts.py``, discovered) is a command (deduped on its
``operation_key``): the enrollment saga calls it as a RETRIABLE step after
the pivot — it never refuses, it only records the message (a real deployment
hands it to ``notifier()``).
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
