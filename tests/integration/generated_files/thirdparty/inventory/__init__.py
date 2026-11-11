"""AD-5: a third-party module outside ``src/`` that declares console screens.

Its ``admin.py`` names one of its own routes; ``sandloom generate`` adds its
section to the console's manifest with no kernel and no console change.
"""

from sandloom.sdk import define_module

register = define_module(__name__, version="0.1.0")
