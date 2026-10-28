"""Enrollment: a student enrolls in a course — the worked saga example.

``ENROLL`` (``sagas.py``, ADR-0008, discovered): reserve the seat (local,
compensated by cancelling it) -> charge the price (``payments.charges.v1``,
compensated by a refund) -> grant access (local, the PIVOT) -> notify the
student (``notifications.mail.v1``, retriable). The enrollment row's
``status`` is the semantic lock: routes show ``pending`` / ``reserved``
until the saga decides.

The saga's typed steps add ``payments.charges.v1`` and
``notifications.mail.v1`` to this module's consumes: it boots only with both
providers, co-loaded or bound in ``SERVICE_BINDINGS``.
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
