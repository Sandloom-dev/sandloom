"""The board tables.

Every module of this package is imported (the app and the migration chain walk
it), so a new ``models/<x>.py`` joins the module's tables and its migration
chain.

Table names start with ``board_``.
Rows of other modules are referenced by UUID, never by a foreign key.
"""
