"""The board tables.

Every module of this package is imported, so a new ``models/<x>.py`` joins
the module's tables and its migration chain. Table names start with
``board_``; rows of other modules are referenced by UUID, never by a
foreign key.
"""

import importlib
import pkgutil

for _info in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_info.name}")
