"""The stats module.

Everything it declares is discovered: routes in ``api.py``, events in
``events.py``, tables in ``models/``. It talks to other modules only through
events and contracts.
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
