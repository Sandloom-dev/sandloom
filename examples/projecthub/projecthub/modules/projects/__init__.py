"""The projects module (a source of the taskboard read model).

Everything it declares is discovered: routes in ``api/``, the project events
in ``events.py``, the public ``projects.read.v2`` contract (and the released ``v1``) in
``contracts.py``, the table in ``models/``.
"""

from sandloom.sdk import define_module

register = define_module(
    __name__, version="0.1.0", capabilities=["database", "eventbus"]
)
