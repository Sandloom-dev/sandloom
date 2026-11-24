"""The links module.

Everything it declares is discovered: routes in ``api/``, the
``LinkCreated`` event in ``events.py``, the table in ``models/`` and the
planner's read operation in ``services/operations.py``.
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
