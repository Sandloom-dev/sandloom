"""Reusable architecture analyzers (``sandloom check``).

The framework's pytest architecture suite and an application's ``sandloom check``
gate share these analyzers, so a generated app is held to the same isolation
rules as the bundled modules. The package is pure stdlib (+ PyYAML to read the
manifest) and imports nothing else from the framework.

    from sandloom.core.archcheck import load_rules, run_checks
    violations = run_checks(load_rules(Path("architecture_rules.yml")))
"""

from sandloom.core.archcheck.manifest import ArchRules, ManifestError, load_rules
from sandloom.core.archcheck.runner import RULES, Violation, run_checks

__all__ = [
    "RULES",
    "ArchRules",
    "ManifestError",
    "Violation",
    "load_rules",
    "run_checks",
]
