"""Payments: prepaid customer accounts and the charges command contract.

``payments.charges.v1`` (``contracts.py``, discovered) is what the
enrollment saga calls: ``charge`` (402 when the balance is short — a
definitive refusal), ``refund`` (accepts "nothing to undo") and ``topup``.
Every method is a command: the framework dedupes it durably on its
``operation_key`` in this module's database, so a saga step re-issued with
the same key never charges twice.
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
