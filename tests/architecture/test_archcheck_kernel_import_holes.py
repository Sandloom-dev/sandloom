"""The kernel is reachable by no road but ``import``, and no manifest key opens one.

``framework-imports`` (SANDLOOM-C002) judged import statements only, and three
keys of ``architecture_rules.yml`` let a package off the check altogether:

* ``sys.modules["sandloom.core.x"]`` is an import that never says ``import``. The
  scan reads it like one, under any spelling of ``sys``, and refuses a use it cannot
  follow (a key it cannot read, the whole mapping, ``sys`` handed to other code).
* ``kernel_package`` and ``framework_packages`` exempt each package they name, so
  an application may not set them (SANDLOOM-C040).
* ``test_api`` and ``shared_api`` may not list the kernel (SANDLOOM-C041), and an
  ``allowlist`` entry may not excuse it for more than named modules of one business
  module (SANDLOOM-C042).

Each ``ok`` case is the control of a refusal: the same shape with the one thing
that makes it legitimate.
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
from pathlib import Path

import pytest

from sandloom.core import error_codes
from sandloom.core.archcheck import load_rules, run_checks
from sandloom.core.archcheck import manifest as manifest_module
from sandloom.core.archcheck.analyzers import sys_modules_reads
from sandloom.core.archcheck.manifest import (
    CORE_IN_ALLOWLIST_CODE,
    CORE_IN_API_LIST_CODE,
    CORE_IN_PUBLIC_API_CODE,
    KERNEL_EXEMPT_PACKAGE_CODE,
    MANIFEST_REFUSAL_CODES,
    ManifestError,
)
from sandloom.core.cli.add import GeneratorError
from sandloom.core.cli.add import layout as add_layout
from sandloom.core.cli.check import main as check_main

BACKEND = Path(__file__).resolve().parents[2]
_K = "sandloom.core.x"


def _scan(source: str) -> tuple[list[tuple[int, str, str]], list[str]]:
    return sys_modules_reads(ast.parse(textwrap.dedent(source)))


# --------------------------------------------------------------------------- #
# The scan: what it reads
# --------------------------------------------------------------------------- #

#: One read of ``sandloom.core.x`` per source, however ``sys.modules`` is reached.
_READS: dict[str, str] = {
    "subscript": f'import sys\nsys.modules["{_K}"]\n',
    "import_alias": f'import sys as s\ns.modules["{_K}"]\n',
    "from_import": f'from sys import modules\nmodules["{_K}"]\n',
    "from_import_alias": f'from sys import modules as m\nm["{_K}"]\n',
    "star_import": f'from sys import *\nmodules["{_K}"]\n',
    "sys_from_os": f'from os import sys\nsys.modules["{_K}"]\n',
    "os_sys": f'import os\nos.sys.modules["{_K}"]\n',
    "importlib_sys": f'import importlib\nimportlib.sys.modules["{_K}"]\n',
    "sys_alias": f'import sys\ns = sys\ns.modules["{_K}"]\n',
    "sys_alias_chain_out_of_order": (
        f'import sys\nc.modules["{_K}"]\nc = b\nb = a\na = sys\n'
    ),
    "modules_alias": f'import sys\nm = sys.modules\nm["{_K}"]\n',
    "modules_alias_of_alias": f'import sys\ns = sys\nm = s.modules\nm["{_K}"]\n',
    "modules_alias_chain": f'import sys\nb = a\na = sys.modules\nb["{_K}"]\n',
    "modules_tuple_alias": f'import sys\nm, n = sys.modules, 1\nm["{_K}"]\n',
    "modules_annotated_alias": f'import sys\nm: dict = sys.modules\nm["{_K}"]\n',
    "walrus_modules": f'import sys\n(m := sys.modules)["{_K}"]\n',
    "walrus_sys": f'import sys\n(s := sys).modules["{_K}"]\n',
    "walrus_vars": f'import sys\n(d := vars(sys))["modules"]["{_K}"]\n',
    "getattr": f'import sys\ngetattr(sys, "modules")["{_K}"]\n',
    "getattr_default": f'import sys\ngetattr(sys, "modules", {{}})["{_K}"]\n',
    "getattr_folded_name": f'import sys\ngetattr(sys, "mod" + "ules")["{_K}"]\n',
    "builtins_getattr": (
        f'import builtins\nimport sys\nbuiltins.getattr(sys, "modules")["{_K}"]\n'
    ),
    "from_builtins_getattr": (
        f'from builtins import getattr as g\nimport sys\ng(sys, "modules")["{_K}"]\n'
    ),
    "getattr_alias": f'import sys\ng = getattr\ng(sys, "modules")["{_K}"]\n',
    "vars": f'import sys\nvars(sys)["modules"]["{_K}"]\n',
    "dunder_dict": f'import sys\nsys.__dict__["modules"]["{_K}"]\n',
    "vars_alias": f'import sys\nd = vars(sys)\nd["modules"]["{_K}"]\n',
    "vars_get": f'import sys\nvars(sys).get("modules")["{_K}"]\n',
    "dunder_dict_get": f'import sys\nsys.__dict__.get("modules")["{_K}"]\n',
    "getattr_dunder_dict": (
        f'import sys\ngetattr(sys, "__dict__")["modules"]["{_K}"]\n'
    ),
    "from_sys_dict": f'from sys import __dict__ as d\nd["modules"]["{_K}"]\n',
    "attrgetter": (
        f'import operator\nimport sys\noperator.attrgetter("modules")(sys)["{_K}"]\n'
    ),
    "attrgetter_from_import": (
        f'from operator import attrgetter as ag\nimport sys\nag("modules")(sys)["{_K}"]\n'
    ),
    "dunder_import": f'__import__("sys").modules["{_K}"]\n',
    "import_module": (
        f'import importlib\nimportlib.import_module("sys").modules["{_K}"]\n'
    ),
    "import_module_alias": (
        f'from importlib import import_module as im\nim("sys").modules["{_K}"]\n'
    ),
    "sys_through_modules": f'import sys\nsys.modules["sys"].modules["{_K}"]\n',
    "sys_through_get": f'import sys\nsys.modules.get("sys").modules["{_K}"]\n',
    "get": f'import sys\nsys.modules.get("{_K}")\n',
    "get_default": f'import sys\nsys.modules.get("{_K}", None)\n',
    "pop": f'import sys\nsys.modules.pop("{_K}")\n',
    "setdefault": f'import sys\nsys.modules.setdefault("{_K}", object())\n',
    "getitem_method": f'import sys\nsys.modules.__getitem__("{_K}")\n',
    "setitem_method": f'import sys\nsys.modules.__setitem__("{_K}", object())\n',
    "delitem_method": f'import sys\nsys.modules.__delitem__("{_K}")\n',
    "contains_method": f'import sys\nsys.modules.__contains__("{_K}")\n',
    "membership": f'import sys\n"{_K}" in sys.modules\n',
    "non_membership": f'import sys\n"{_K}" not in sys.modules\n',
    "assignment": f'import sys\nsys.modules["{_K}"] = object()\n',
    "deletion": f'import sys\ndel sys.modules["{_K}"]\n',
    "folded_concatenation": 'import sys\nsys.modules["sandloom." + "core.x"]\n',
    "folded_fstring": "import sys\nsys.modules[f\"{'sandloom'}.core.x\"]\n",
    "name_constant": f'import sys\nK = "{_K}"\nsys.modules[K]\n',
    "name_constant_twice": f'import sys\nK = "{_K}"\nK = "{_K}"\nsys.modules[K]\n',
    "name_in_fstring": 'import sys\nP = "sandloom"\nsys.modules[f"{P}.core.x"]\n',
    "name_plus_text": 'import sys\nP = "sandloom.core"\nsys.modules[P + ".x"]\n',
    "in_a_function": f'import sys\n\ndef f():\n    return sys.modules["{_K}"]\n',
    "local_import": f'def f():\n    import sys\n    return sys.modules["{_K}"]\n',
}


@pytest.mark.parametrize("name", list(_READS))
def test_a_read_of_the_kernel_is_found(name: str) -> None:
    reads, refused = _scan(_READS[name])
    assert refused == [], refused
    assert {module for _line, module, _text in reads} >= {_K}, reads
    assert all(text == f"sys.modules[{module!r}]" for _l, module, text in reads)


def test_a_read_carries_its_line_and_is_reported_once() -> None:
    reads, refused = _scan(
        f'import sys\n\n\nx = sys.modules["{_K}"] or sys.modules["{_K}"]\n'
    )
    assert refused == []
    assert reads == [(4, _K, f"sys.modules[{_K!r}]")]


def test_a_read_of_another_module_is_still_returned_for_the_rules_to_judge() -> None:
    reads, refused = _scan(
        'import sys\nsys.modules["json"]\nsys.modules["sandloom.sdk.web"]\n'
    )
    assert refused == []
    assert sorted(module for _l, module, _t in reads) == ["json", "sandloom.sdk.web"]


# --------------------------------------------------------------------------- #
# The scan: what it refuses
# --------------------------------------------------------------------------- #

_KEY = "the key of sys.modules"
_WHOLE = "reads the whole of sys.modules"
_HANDED = "sys.modules is handed to code the check cannot follow"
_SYS = "the sys module is handed to code the check cannot follow"

#: ``(source, a fragment of the refusal)``: each is refused, on its own line.
_REFUSED: dict[str, tuple[str, str]] = {
    # a key the scan cannot read
    "variable_key": ("import sys\nsys.modules[name]\n", f"{_KEY} is in a way"),
    "fstring_hole": (
        'import sys\nsys.modules[f"sandloom.{x}"]\n',
        f"{_KEY} starts with 'sandloom.'",
    ),
    "prefix_of_the_kernel": (
        'import sys\nsys.modules["sand" + x]\n',
        f"{_KEY} starts with 'sand'",
    ),
    "hole_first": ('import sys\nsys.modules[x + ".core.x"]\n', f"{_KEY} is in a way"),
    "percent_template": (
        'import sys\nsys.modules["sandloom.core.%s" % x]\n',
        f"{_KEY} starts with 'sandloom.core.'",
    ),
    "format_template": (
        'import sys\nsys.modules["sandloom.core.{}".format(x)]\n',
        f"{_KEY} starts with 'sandloom.core.'",
    ),
    "join": ('import sys\nsys.modules[".".join(parts)]\n', f"{_KEY} is in a way"),
    "item_of_a_list": ("import sys\nsys.modules[names[0]]\n", f"{_KEY} is in a way"),
    "call": ("import sys\nsys.modules[get_name()]\n", f"{_KEY} is in a way"),
    "slice": ("import sys\nsys.modules[:]\n", f"{_KEY} is in a way"),
    "no_key": ("import sys\nsys.modules.get()\n", f"{_KEY} is in a way"),
    "get_variable": ("import sys\nsys.modules.get(name)\n", f"{_KEY} is in a way"),
    "membership_variable": (
        "import sys\nname in sys.modules\n",
        f"{_KEY} is in a way",
    ),
    "name_bound_twice": (
        'import sys\nK = "json"\nK = "sandloom.core.x"\nsys.modules[K]\n',
        f"{_KEY} is in a way",
    ),
    "name_is_a_parameter": (
        'import sys\ndef f(K="sandloom.core.x"):\n    return sys.modules[K]\n',
        f"{_KEY} is in a way",
    ),
    "name_is_a_loop_variable": (
        'import sys\nfor K in ("sandloom.core.x",):\n    sys.modules[K]\n',
        f"{_KEY} is in a way",
    ),
    "name_is_extended": (
        'import sys\nK = "sandloom.core"\nK += ".x"\nsys.modules[K]\n',
        f"{_KEY} is in a way",
    ),
    "name_is_imported_over": (
        'import sys\nfrom a import K\nK = "json"\nsys.modules[K]\n',
        f"{_KEY} is in a way",
    ),
    "vars_with_a_variable_key": (
        "import sys\nvars(sys)[key]\n",
        "the namespace of sys is read in a way",
    ),
    # the package itself hands out the kernel
    "the_package": ('import sys\nsys.modules["sandloom"]\n', "hands out the package"),
    "the_package_by_get": (
        'import sys\nsys.modules.get("sandloom")\n',
        "hands out the package",
    ),
    "the_package_folded": (
        'import sys\nsys.modules["sand" + "loom"]\n',
        "hands out the package",
    ),
    # the whole mapping
    "items": ("import sys\nsys.modules.items()\n", f"sys.modules.items {_WHOLE}"),
    "keys": ("import sys\nsys.modules.keys()\n", f"sys.modules.keys {_WHOLE}"),
    "values": ("import sys\nsys.modules.values()\n", f"sys.modules.values {_WHOLE}"),
    "copy": ("import sys\nsys.modules.copy()\n", f"sys.modules.copy {_WHOLE}"),
    "update": ("import sys\nsys.modules.update({})\n", f"sys.modules.update {_WHOLE}"),
    "bound_method": (
        "import sys\nget = sys.modules.get\n",
        f"sys.modules.get {_WHOLE}",
    ),
    "alias_items": (
        "import sys\nm = sys.modules\nm.items()\n",
        f"sys.modules.items {_WHOLE}",
    ),
    "loop": ("import sys\nfor n in sys.modules:\n    pass\n", _HANDED),
    "alias_loop": ("import sys\nm = sys.modules\nfor n in m:\n    pass\n", _HANDED),
    "list": ("import sys\nlist(sys.modules)\n", _HANDED),
    "dict": ("import sys\ndict(sys.modules)\n", _HANDED),
    "unpacked": ("import sys\n{**sys.modules}\n", _HANDED),
    "len": ("import sys\nlen(sys.modules)\n", _HANDED),
    "argument": ("import sys\nf(sys.modules)\n", _HANDED),
    "stored_in_a_field": ("import sys\nself.m = sys.modules\n", _HANDED),
    "walrus_as_a_statement": ("import sys\n(m := sys.modules)\n", _HANDED),
    "stored_in_a_list": ("import sys\nx = [sys.modules]\n", _HANDED),
    "conditional": ("import sys\nx = sys.modules if c else {}\n", _HANDED),
    "returned": ("import sys\ndef f():\n    return sys.modules\n", _HANDED),
    "replaced": ("import sys\nsys.modules = {}\n", _HANDED),
    "lambda": ("import sys\nf = lambda: sys.modules\n", _HANDED),
    "from_import_loop": (
        "from sys import modules\nfor n in modules:\n    pass\n",
        _HANDED,
    ),
    "getattr_modules_argument": ('import sys\nf(getattr(sys, "modules"))\n', _HANDED),
    # sys handed on, or read by a name the scan cannot follow
    "sys_argument": ("import sys\nf(sys)\n", _SYS),
    "sys_in_a_list": ("import sys\nx = [sys]\n", _SYS),
    "sys_returned": ("import sys\ndef f():\n    return sys\n", _SYS),
    "sys_stored": ("import sys\nself.s = sys\n", _SYS),
    "sys_patched": ('import sys\nmock.patch.object(sys, "argv", [])\n', _SYS),
    "sys_through_an_alias": ("import sys\ns = sys\nf(s)\n", _SYS),
    "getattr_variable": (
        "import sys\ngetattr(sys, name)\n",
        "getattr reads an attribute of sys by a name it cannot read",
    ),
    "getattr_built_name": (
        'import sys\ngetattr(sys, "mod" + x)\n',
        "getattr reads an attribute of sys by a name it cannot read",
    ),
    "attrgetter_variable": (
        "import operator\nimport sys\noperator.attrgetter(name)(sys)\n",
        "attrgetter reads sys by a path the check cannot follow",
    ),
    "attrgetter_dotted": (
        'import operator\nimport sys\noperator.attrgetter("modules.get")(sys)\n',
        "attrgetter reads sys by a path the check cannot follow",
    ),
    "attrgetter_dict": (
        'from operator import attrgetter\nimport sys\nattrgetter("__dict__")(sys)\n',
        "attrgetter reads sys by a path the check cannot follow",
    ),
    # the namespace of sys, as a whole
    "vars_items": (
        "import sys\nvars(sys).items()\n",
        "the namespace of sys is read in a way",
    ),
    "dunder_dict_copy": (
        "import sys\nsys.__dict__.copy()\n",
        "the namespace of sys is read in a way",
    ),
    "vars_loop": (
        "import sys\nd = vars(sys)\nfor k in d:\n    pass\n",
        "the namespace of sys is read in a way",
    ),
    "vars_dict": (
        "import sys\ndict(vars(sys))\n",
        "the namespace of sys is read in a way",
    ),
    "vars_get_variable": (
        "import sys\nvars(sys).get(key)\n",
        "the namespace of sys is read in a way",
    ),
}


@pytest.mark.parametrize("name", list(_REFUSED))
def test_a_use_the_scan_cannot_follow_is_refused(name: str) -> None:
    source, fragment = _REFUSED[name]
    _reads, refused = _scan(source)
    assert refused, "not refused"
    assert any(fragment in text for text in refused), (fragment, refused)
    for text in refused:
        assert text.startswith("Line "), text
        assert "(sys_modules: name the module with a constant string" in text, text


def test_a_refusal_carries_its_line() -> None:
    _reads, refused = _scan("import sys\n\n\nx = sys.modules[name]\n")
    assert len(refused) == 1 and refused[0].startswith("Line 4: "), refused


# --------------------------------------------------------------------------- #
# The scan: the ``sandloom`` package object is judged like ``sys``
# --------------------------------------------------------------------------- #
# ``import sandloom.sdk`` binds ``sandloom``, and ``sandloom.core.x`` is then an
# attribute away: no import statement, no ``sys.modules`` key. Only ``.sdk`` and
# ``.shared`` are public. The rule catches the code a module author writes by
# mistake; a deliberate bypass is not caught statically.

_CORE = "`sandloom.core` is the kernel"
_GETATTR = "getattr reads an attribute of sandloom by a name"
_HANDED_PACKAGE = "the sandloom package is handed to code the check cannot follow"

#: ``(source, a fragment of the refusal)``: each is refused, on its own line.
_PACKAGE_REFUSED: dict[str, tuple[str, str]] = {
    "core_attribute": ("import sandloom.sdk\nsandloom.core.database\n", _CORE),
    "core_call": ("import sandloom.sdk\nsandloom.core.database.engine()\n", _CORE),
    "after_the_shared_import": ("import sandloom.shared\nsandloom.core.x\n", _CORE),
    "another_private_attribute": (
        "import sandloom.sdk\nsandloom.cli\n",
        "`sandloom.cli` is the kernel",
    ),
    "alias_of_the_package": ("import sandloom.sdk\nm = sandloom\nm.core.x\n", _CORE),
    "alias_chain": ("import sandloom.sdk\nb = a\na = sandloom\nb.core.x\n", _CORE),
    "walrus": ("import sandloom.sdk\n(m := sandloom).core.x\n", _CORE),
    "dunder_import": ('__import__("sandloom.sdk").core.x\n', _CORE),
    "dunder_import_shared": ('__import__("sandloom.shared").core.x\n', _CORE),
    "dunder_import_alias": ('p = __import__("sandloom.sdk")\np.core.x\n', _CORE),
    "dunder_import_stored": (
        'self.p = __import__("sandloom.sdk")\n',
        _HANDED_PACKAGE,
    ),
    "getattr_core": (
        'import sandloom.sdk\ngetattr(sandloom, "core")\n',
        _GETATTR,
    ),
    "getattr_variable": ("import sandloom.sdk\ngetattr(sandloom, name)\n", _GETATTR),
    "getattr_built_name": (
        'import sandloom.sdk\ngetattr(sandloom, "co" + x)\n',
        _GETATTR,
    ),
    "handed_on": ("import sandloom.sdk\nf(sandloom)\n", _HANDED_PACKAGE),
    "stored": ("import sandloom.sdk\nself.p = sandloom\n", _HANDED_PACKAGE),
    "in_a_list": ("import sandloom.sdk\nx = [sandloom]\n", _HANDED_PACKAGE),
    "returned": (
        "import sandloom.sdk\ndef f():\n    return sandloom\n",
        _HANDED_PACKAGE,
    ),
    "star_import": ("from sandloom import *\n", "a star import from sandloom"),
    "star_import_with_code": (
        "from sandloom import *\nx = 1\n",
        "a star import from sandloom",
    ),
}


@pytest.mark.parametrize("name", list(_PACKAGE_REFUSED))
def test_a_use_of_the_sandloom_package_that_may_reach_the_kernel_is_refused(
    name: str,
) -> None:
    source, fragment = _PACKAGE_REFUSED[name]
    reads, refused = _scan(source)
    assert reads == [], reads
    assert len(refused) == 1, refused
    assert fragment in refused[0], (fragment, refused)
    assert re.match(r"Line \d+: ", refused[0]), refused
    assert (
        "(the sandloom package: only `sandloom.sdk` and `sandloom.shared`"
        in (refused[0])
    )


#: The same shapes with the one thing that makes them legitimate.
_PACKAGE_OK: dict[str, str] = {
    "sdk_attribute": "import sandloom.sdk\nsandloom.sdk.web.route\n",
    "sdk_itself": "import sandloom.sdk\nsandloom.sdk\n",
    "shared_attribute": "import sandloom.shared\nsandloom.shared.authz\n",
    "both_imports": "import sandloom.sdk\nimport sandloom.shared\nsandloom.shared.x\n",
    "an_imported_submodule": "import sandloom.sdk.web\nsandloom.sdk.web.route\n",
    "the_alias_of_a_submodule": "import sandloom.sdk as s\ns.web\n",
    "alias_of_the_package_then_sdk": "import sandloom.sdk\nm = sandloom\nm.sdk.web\n",
    "dunder_import_then_sdk": '__import__("sandloom.sdk").sdk.web\n',
    "dunder_import_as_a_statement": '__import__("sandloom.sdk")\n',
    "dunder_import_with_a_fromlist": '__import__("sandloom.sdk", fromlist=["web"]).web\n',
    "getattr_sdk": 'import sandloom.sdk\ngetattr(sandloom, "sdk")\n',
    "getattr_shared_default": 'import sandloom.sdk\ngetattr(sandloom, "shared", None)\n',
    "hasattr": 'import sandloom.sdk\nhasattr(sandloom, "core")\n',
    "from_import_of_sdk": "from sandloom import sdk\nsdk.web\n",
    "from_import_of_a_submodule": "from sandloom.sdk import *\n",
    "another_package": "import django.core\ndjango.core.x\n",
    "a_local_named_sandloom": "sandloom = object()\nsandloom.core\n",
}


@pytest.mark.parametrize("name", list(_PACKAGE_OK))
def test_a_use_of_the_sandloom_package_that_stays_public_is_left_alone(
    name: str,
) -> None:
    assert _scan(_PACKAGE_OK[name]) == ([], [])


# --------------------------------------------------------------------------- #
# The scan: what it leaves alone
# --------------------------------------------------------------------------- #

#: Not a read of the kernel: no read, no refusal.
_OK: dict[str, str] = {
    "argv": "import sys\nsys.argv\n",
    "exit": "import sys\nsys.exit(0)\n",
    "stderr": 'import sys\nprint("x", file=sys.stderr)\n',
    "version": "import sys\nsys.version_info >= (3, 11)\n",
    "hasattr": 'import sys\nhasattr(sys, "frozen")\n',
    "getattr_other_attribute": 'import sys\ngetattr(sys, "frozen", False)\n',
    "own_name": "import sys\nsys.modules[__name__]\n",
    "own_name_by_get": "import sys\nsys.modules.get(__name__)\n",
    "own_name_membership": "import sys\n__name__ in sys.modules\n",
    "vars_other_key": 'import sys\nvars(sys)["argv"]\n',
    "dunder_dict_other_key": 'import sys\nsys.__dict__.get("argv")\n',
    "another_package_prefix": 'import sys\nsys.modules["django." + name]\n',
    "another_package_fstring": 'import sys\nsys.modules[f"django.{name}"]\n',
    "an_installed_module_prefix": 'import sys\nsys.modules[f"sandloom_billing.{name}"]\n',
    "not_sys": 'modules = {}\nmodules["sandloom.core.x"] = 1\n',
    "an_attribute_named_modules": 'class A:\n    modules = {}\nA.modules["sandloom.core.x"]\n',
    "an_object_with_modules": 'app.modules["sandloom.core.x"]\n',
    "sys_imported_and_unused": "import sys\n",
}


@pytest.mark.parametrize("name", list(_OK))
def test_a_use_that_cannot_reach_the_kernel_is_left_alone(name: str) -> None:
    assert _scan(_OK[name]) == ([], [])


# --------------------------------------------------------------------------- #
# ``sandloom check``: the read is judged as the import is
# --------------------------------------------------------------------------- #

_APP = "modules_path: acme/modules\nmodules_package: acme.modules\n"


def _app(tmp_path: Path, files: dict[str, str], manifest: str = "") -> Path:
    modules = tmp_path / "acme" / "modules"
    (modules / "orders").mkdir(parents=True)
    (modules / "__init__.py").write_text("")
    (modules / "orders" / "__init__.py").write_text("")
    for name, body in files.items():
        target = modules / "orders" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    path = tmp_path / "architecture_rules.yml"
    path.write_text(_APP + (manifest or "public_api:\n  - sandloom.sdk\n"))
    return path


def _framework_tree(
    tmp_path: Path, files: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A tree shaped like the framework checkout. ``<tmp>/src/sandloom`` is the
    kernel of this test: the manifest module's idea of where the running kernel
    lives is pointed at it, as it points at ``src/sandloom`` in the real checkout."""
    monkeypatch.setattr(
        manifest_module,
        "_kernel_dir",
        lambda: (tmp_path / "src" / "sandloom").resolve(),
    )
    (tmp_path / "src" / "sandloom" / "core").mkdir(parents=True)
    (tmp_path / "src" / "sandloom" / "__init__.py").write_text("")
    (tmp_path / "src" / "sandloom" / "core" / "__init__.py").write_text("")
    (tmp_path / "src" / "sandloom_orders").mkdir()
    (tmp_path / "src" / "sandloom_orders" / "__init__.py").write_text("")
    for name, body in files.items():
        target = tmp_path / "src" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    path = tmp_path / "architecture_rules.yml"
    path.write_text(
        'modules_path: src\nmodules_package: ""\nsource_root: src\n'
        "kernel_package: sandloom\npublic_api:\n  - sandloom.sdk\n"
    )
    return path


def _check(manifest: Path) -> list[tuple[str, int | None, str]]:
    found = run_checks(load_rules(manifest), only=["framework-imports"])
    return [(v.file.name, v.line, v.message) for v in found]


_READ_KERNEL = (
    'import sys\n\n\ndef kernel():\n    return sys.modules["sandloom.core.database"]\n'
)


def test_a_read_of_a_kernel_module_is_reported_like_the_import(tmp_path: Path) -> None:
    manifest = _app(
        tmp_path,
        {
            "read.py": _READ_KERNEL,
            "imported.py": "from sandloom.core import database\n",
        },
    )
    assert sorted(_check(manifest)) == [
        (
            "imported.py",
            1,
            "sandloom.core.database (non_public_api: not in public_api)",
        ),
        (
            "read.py",
            5,
            "sandloom.core.database via sys.modules "
            "(non_public_api: not in public_api)",
        ),
    ]
    codes = {
        v.code for v in run_checks(load_rules(manifest), only=["framework-imports"])
    }
    assert codes == {"SANDLOOM-C002"}


def test_a_read_in_the_framework_tree_is_reported_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _framework_tree(
        tmp_path, {"sandloom_orders/service.py": _READ_KERNEL}, monkeypatch
    )
    assert _check(manifest) == [
        (
            "service.py",
            5,
            "sandloom.core.database via sys.modules "
            "(non_public_api: not in public_api)",
        )
    ]


def test_the_kernel_itself_reads_sys_modules_freely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _framework_tree(
        tmp_path,
        {
            "sandloom/core/loader.py": "import sys\nm = sys.modules.items()\n",
            "sandloom_orders/ok.py": "import sys\nsys.modules[__name__]\n",
        },
        monkeypatch,
    )
    assert _check(manifest) == []


def test_an_import_and_a_read_of_one_module_are_one_report(tmp_path: Path) -> None:
    both = "import sys\nfrom sandloom.core import database\nsys.modules['sandloom.core.database']\n"
    assert _check(_app(tmp_path, {"both.py": both})) == [
        ("both.py", 2, "sandloom.core.database (non_public_api: not in public_api)")
    ]


def test_a_read_of_a_module_an_application_may_not_import_is_reported(
    tmp_path: Path,
) -> None:
    source = (
        "import sys\n"
        'sys.modules["sandloom.driverkit"]\n'
        'sys.modules["sandloom.sdk._prim"]\n'
    )
    found = _check(_app(tmp_path, {"driver.py": source}))
    assert [(line, message.split(" (")[0]) for _f, line, message in found] == [
        (2, "sandloom.driverkit via sys.modules"),
        (3, "sandloom.sdk._prim via sys.modules"),
    ]
    assert "(framework_module: only the public API" in found[0][2]
    assert found[1][2].endswith("(forbidden_pattern)")


def test_a_read_of_a_public_or_foreign_module_is_not_reported(tmp_path: Path) -> None:
    source = (
        "import sys\n"
        'sys.modules["sandloom.sdk.web"]\n'
        'sys.modules["json"]\n'
        "sys.modules[__name__]\n"
        'sys.modules["django." + name]\n'
        'vars(sys)["argv"]\n'
    )
    assert _check(_app(tmp_path, {"fine.py": source})) == []


def test_an_unreadable_read_is_a_framework_imports_violation(tmp_path: Path) -> None:
    source = "import sys\n\n\ndef f(name):\n    return sys.modules[name]\n"
    manifest = _app(tmp_path, {"lookup.py": source, "loop.py": "import sys\nf(sys)\n"})
    found = run_checks(load_rules(manifest), only=["framework-imports"])
    assert sorted((v.file.name, v.line, v.code) for v in found) == [
        ("lookup.py", 5, "SANDLOOM-C002"),
        ("loop.py", 2, "SANDLOOM-C002"),
    ]
    assert all("(sys_modules: " in v.message for v in found)


def test_the_kernel_through_the_sandloom_package_is_a_framework_imports_violation(
    tmp_path: Path,
) -> None:
    manifest = _app(
        tmp_path,
        {
            "attr.py": "import sandloom.sdk\n\nx = sandloom.core.database\n",
            "dunder.py": '__import__("sandloom.sdk").core.database\n',
            "star.py": "from sandloom import *\n",
            "fine.py": "import sandloom.sdk\n\nx = sandloom.sdk.web\n",
        },
    )
    found = run_checks(load_rules(manifest), only=["framework-imports"])
    assert sorted((v.file.name, v.line, v.code) for v in found) == [
        ("attr.py", 3, "SANDLOOM-C002"),
        ("dunder.py", 1, "SANDLOOM-C002"),
        ("star.py", 1, "SANDLOOM-C002"),
    ]
    assert all("(the sandloom package: only `sandloom.sdk`" in v.message for v in found)


def test_tests_and_migrations_are_not_scanned_for_reads(tmp_path: Path) -> None:
    manifest = _app(
        tmp_path,
        {
            "tests/test_x.py": _READ_KERNEL,
            "migrations/versions/0001.py": _READ_KERNEL + "import sys\nf(sys)\n",
            "service.py": "x = 1\n",
        },
    )
    assert _check(manifest) == []


def test_a_waiver_on_the_line_admits_one_read(tmp_path: Path) -> None:
    waived = _READ_KERNEL.replace(
        '"sandloom.core.database"]',
        '"sandloom.core.database"]  # sandloom: allow framework-imports reviewed: probe',
    )
    unreadable = "import sys\n\n\ndef f(n):\n    return sys.modules[n]  # sandloom: allow framework-imports reviewed\n"
    manifest = _app(
        tmp_path,
        {"waived.py": waived, "unreadable.py": unreadable, "other.py": _READ_KERNEL},
    )
    assert [name for name, _line, _message in _check(manifest)] == ["other.py"]


def test_an_allowlist_entry_for_one_module_excuses_its_read_in_the_framework_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _framework_tree(
        tmp_path,
        {
            "sandloom_orders/read.py": _READ_KERNEL,
            "sandloom_orders/other.py": _READ_KERNEL,
        },
        monkeypatch,
    )
    manifest.write_text(
        manifest.read_text() + 'allowlist:\n  - {file: "src/sandloom_orders/read.py", '
        'imports: ["sandloom.core.database*"]}\n'
    )
    assert [name for name, _line, _message in _check(manifest)] == ["other.py"]


def test_an_application_allowlist_excuses_no_read_of_the_kernel(
    tmp_path: Path,
) -> None:
    """The same entry in an application is refused before any file is read: the
    allowlist of an application keeps imports of other packages only."""
    allow = (
        "allowlist:\n"
        '  - {file: "acme/modules/orders/read.py", imports: ["sandloom.core.database*"]}\n'
    )
    manifest = _app(
        tmp_path,
        {"read.py": _READ_KERNEL},
        manifest="public_api:\n  - sandloom.sdk\n" + allow,
    )
    message = _refused(manifest, CORE_IN_ALLOWLIST_CODE)
    assert (
        "file 'acme/modules/orders/read.py' admits sandloom.core.database*" in message
    )


# --------------------------------------------------------------------------- #
# SANDLOOM-C040: kernel_package and framework_packages
# --------------------------------------------------------------------------- #


def _manifest(tmp_path: Path, body: str, *dirs: str) -> Path:
    for directory in dirs or ("acme/modules/orders",):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    path = tmp_path / "architecture_rules.yml"
    path.write_text(body)
    return path


def _refused(manifest: Path, code: str) -> str:
    with pytest.raises(ManifestError) as raised:
        load_rules(manifest)
    message = str(raised.value)
    assert code in message, message
    return message


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        ("kernel_package: orders\n", "`kernel_package: orders`"),
        ("kernel_package: sandloom\n", "`kernel_package: sandloom`"),
        ("framework_packages: [orders]\n", "`framework_packages: [orders]`"),
        ("framework_packages: [orders, sdk]\n", "`framework_packages: [orders, sdk]`"),
        (
            "kernel_package: orders\nframework_packages: [orders]\n",
            "`kernel_package: orders` and `framework_packages: [orders]`",
        ),
    ],
    ids=["kernel", "kernel-named-sandloom", "framework", "framework-two", "both"],
)
def test_an_application_may_not_exempt_a_package(
    tmp_path: Path, extra: str, named: str
) -> None:
    message = _refused(_manifest(tmp_path, _APP + extra), KERNEL_EXEMPT_PACKAGE_CODE)
    assert named in message
    assert "exempts the package it names from every check" in message
    assert "delete `kernel_package` and `framework_packages`" in message


def test_a_manifest_that_only_spells_the_framework_tree_is_refused(
    tmp_path: Path,
) -> None:
    """``modules_package: ""`` and ``kernel_package: sandloom`` are what the
    framework tree writes; without the kernel under ``modules_path`` they are an
    application's disguise."""
    body = 'modules_path: acme/modules\nmodules_package: ""\nkernel_package: sandloom\n'
    message = _refused(_manifest(tmp_path, body), KERNEL_EXEMPT_PACKAGE_CODE)
    assert "the running kernel itself as `sandloom` under `modules_path`" in message


def test_a_made_up_kernel_directory_does_not_make_an_application_the_framework(
    tmp_path: Path,
) -> None:
    """``mkdir <modules_path>/sandloom/core`` is what a disguised application
    adds to look like the framework tree. The check compares the resolved path
    with the kernel this process runs, not the names under ``modules_path``."""
    body = (
        'modules_path: acme/modules\nmodules_package: ""\nkernel_package: sandloom\n'
        "framework_packages: [orders]\n"
    )
    manifest = _manifest(tmp_path, body, "acme/modules/sandloom/core")
    (tmp_path / "acme" / "modules" / "sandloom" / "__init__.py").write_text("")
    message = _refused(manifest, KERNEL_EXEMPT_PACKAGE_CODE)
    assert "`kernel_package: sandloom` and `framework_packages: [orders]`" in message


def test_a_framework_shaped_tree_with_another_kernel_names_the_real_cause(
    tmp_path: Path,
) -> None:
    """The framework's own checkout, checked with another checkout's
    environment, has a ``sandloom`` under ``modules_path`` that is not the kernel
    the process runs. The refusal says so and says what to run, instead of
    telling the framework to delete its own manifest keys. A manifest that does
    not have the framework tree's shape (no ``sandloom`` directory, or another
    ``modules_package``) gets no such hint."""
    shaped = (
        'modules_path: acme/modules\nmodules_package: ""\nkernel_package: sandloom\n'
    )
    hint = "is not the kernel this process runs"
    with_dir = tmp_path / "with-dir"
    with_dir.mkdir()
    message = _refused(
        _manifest(with_dir, shaped, "acme/modules/sandloom"), KERNEL_EXEMPT_PACKAGE_CODE
    )
    assert hint in message and "uv run sandloom check" in message, message
    without_dir = tmp_path / "without-dir"
    without_dir.mkdir()
    assert hint not in _refused(
        _manifest(without_dir, shaped), KERNEL_EXEMPT_PACKAGE_CODE
    )
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    assert hint not in _refused(
        _manifest(
            packaged, _APP + "kernel_package: sandloom\n", "acme/modules/sandloom"
        ),
        KERNEL_EXEMPT_PACKAGE_CODE,
    )


def test_the_real_framework_manifest_is_the_framework_tree() -> None:
    """The control of the two tests above: the checkout's own manifest names
    ``src/sandloom``, which resolves to the kernel this process runs."""
    rules = load_rules(BACKEND / "architecture_rules.yml")
    assert rules.kernel_package == "sandloom" and rules.is_framework_tree


def test_the_kernel_is_compared_by_its_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An editable install makes ``src/sandloom`` a link to the checkout: it is
    the kernel when the link resolves to the running kernel, and not otherwise."""
    real = tmp_path / "checkout" / "sandloom"
    (real / "core").mkdir(parents=True)
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "sandloom").symlink_to(real, target_is_directory=True)
    manifest = tree / "architecture_rules.yml"
    manifest.write_text(
        'modules_path: src\nmodules_package: ""\nsource_root: src\n'
        "kernel_package: sandloom\n"
    )
    monkeypatch.setattr(manifest_module, "_kernel_dir", lambda: real.resolve())
    assert load_rules(manifest).is_framework_tree
    monkeypatch.setattr(
        manifest_module, "_kernel_dir", lambda: (tmp_path / "elsewhere").resolve()
    )
    _refused(manifest, KERNEL_EXEMPT_PACKAGE_CODE)


def test_the_exempting_keys_stay_open_to_the_framework_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _framework_tree(tmp_path, {}, monkeypatch)
    rules = load_rules(manifest)
    assert rules.kernel_package == "sandloom" and rules.is_framework_tree
    manifest.write_text(manifest.read_text() + "framework_packages: [sdk]\n")
    assert load_rules(manifest).framework_packages == {"sdk"}


@pytest.mark.parametrize(
    "extra",
    ["", "kernel_package: ''\n", "kernel_package:\n", "framework_packages: []\n"],
    ids=["absent", "empty-string", "null", "empty-list"],
)
def test_an_application_without_the_exempting_keys_loads(
    tmp_path: Path, extra: str
) -> None:
    rules = load_rules(_manifest(tmp_path, _APP + extra))
    assert rules.kernel_package is None and rules.framework_packages == set()


def test_an_ecosystem_repository_of_top_level_modules_loads(tmp_path: Path) -> None:
    body = 'modules_path: src\nmodules_package: ""\nsource_root: src\n'
    rules = load_rules(_manifest(tmp_path, body, "src/acme_orders"))
    assert rules.kernel_package is None and not rules.is_framework_tree


# --------------------------------------------------------------------------- #
# SANDLOOM-C041: test_api and shared_api
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["test_api", "shared_api"])
@pytest.mark.parametrize(
    "entries",
    [
        ["sandloom"],
        ["sandloom.core"],
        ["sandloom.core.database"],
        ["sandloom.core.di", "sandloom.core.testing"],
        ["sandloom.core.testing_helpers"],
        ["sandloom.core.testing.fixtures", "sandloom.core.config"],
    ],
    ids=["package", "core", "module", "mixed", "lookalike", "submodule-and-module"],
)
def test_the_api_lists_may_not_cover_the_kernel(
    tmp_path: Path, key: str, entries: list[str]
) -> None:
    body = _APP + f"{key}: {json.dumps(entries)}\n"
    message = _refused(_manifest(tmp_path, body), CORE_IN_API_LIST_CODE)
    assert f"`{key}` lists the kernel" in message
    beyond = [e for e in entries if e not in ("sandloom.core.testing",)]
    beyond = [e for e in beyond if not e.startswith("sandloom.core.testing.")]
    assert f"delete {', '.join(sorted(beyond))} from `{key}`" in message


@pytest.mark.parametrize(
    ("key", "entries"),
    [
        ("test_api", ["sandloom.core.testing", "sandloom.core.contract_testing"]),
        ("test_api", ["sandloom.core.testing.fixtures"]),
        ("test_api", ["sandloom.sdk.testing"]),
        ("test_api", ["sandloom_billing"]),
        ("shared_api", ["sandloom.shared"]),
        ("shared_api", ["sandloom.core.testing"]),
        ("shared_api", ["sandloom.driverkit"]),
    ],
)
def test_the_api_lists_keep_what_they_may(
    tmp_path: Path, key: str, entries: list[str]
) -> None:
    rules = load_rules(_manifest(tmp_path, _APP + f"{key}: {json.dumps(entries)}\n"))
    assert set(entries) == getattr(rules, key)


def test_the_defaults_of_the_api_lists_cover_no_kernel_module(tmp_path: Path) -> None:
    rules = load_rules(_manifest(tmp_path, _APP))
    assert rules.test_api == {"sandloom.core.testing", "sandloom.core.contract_testing"}
    assert rules.shared_api == {"sandloom.shared"}


# --------------------------------------------------------------------------- #
# SANDLOOM-C042: allowlist
# --------------------------------------------------------------------------- #


def _allowlist(file: str, imports: list[str]) -> str:
    return f"allowlist:\n  - file: {json.dumps(file)}\n    imports: {json.dumps(imports)}\n"


_ORDERS = "acme/modules/orders/service.py"


#: ``imports`` that match several kernel modules (a family of them).
_FAMILIES = {
    "core-star": ["sandloom.core*"],
    "package-star": ["sandloom*"],
    "star": ["*"],
    "core-dot-star": ["sandloom.core.*"],
    "wild-first-segment": ["*.core.*"],
    "wild-both-segments": ["s*.core.d*"],
    "several-d-modules": ["sandloom.core.d*"],
    "character-class": ["sandloom.core.[a-z]*"],
    "private-sdk": ["sandloom.sdk._*"],
    "one-of-two": ["sqlalchemy*", "sandloom.core*"],
}
#: ``imports`` that name or match ONE kernel module (and its submodules).
_ONE_KERNEL_MODULE = {
    "core-itself": ["sandloom.core"],
    "core-with-a-question-mark": ["sandloom.cor?"],
    "core-with-a-class": ["sandloom.cor[e]"],
    "a-module": ["sandloom.core.database"],
    "a-module-and-its-children": ["sandloom.core.database*"],
    "a-module-and-another": ["sandloom.core.database.*", "sandloom.core.di"],
    "one-private-layer": ["sandloom.sdk._prim*"],
    # one private layer (``_decl`` and the modules inside it) is one unit
    "one-private-layer-and-its-modules": ["sandloom.sdk._decl._*"],
    "a-module-not-there-yet": ["sandloom.core.nothing_yet"],
}


@pytest.mark.parametrize(
    "imports",
    [*_FAMILIES.values(), *_ONE_KERNEL_MODULE.values()],
    ids=[*_FAMILIES, *_ONE_KERNEL_MODULE],
)
def test_an_application_allowlist_excuses_no_kernel_module(
    tmp_path: Path, imports: list[str]
) -> None:
    """Whatever the pattern and whatever the file, even the files of one module:
    an application's allowlist keeps imports of other packages only. The text
    says so, not that one module's named imports would have been fine."""
    message = _refused(
        _manifest(tmp_path, _APP + _allowlist(_ORDERS, imports)), CORE_IN_ALLOWLIST_CODE
    )
    assert f"file {_ORDERS!r} admits" in message
    assert "which an application's manifest may not do for any file" in message
    assert "delete the entry" in message
    assert "for more than the named modules" not in message


@pytest.mark.parametrize("imports", list(_FAMILIES.values()), ids=list(_FAMILIES))
def test_a_framework_tree_allowlist_may_not_excuse_a_family_of_kernel_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, imports: list[str]
) -> None:
    # even in the files of one module, a pattern that matches several is refused
    manifest = _framework_tree(tmp_path, {}, monkeypatch)
    manifest.write_text(
        manifest.read_text() + _allowlist("src/sandloom_orders/service.py", imports)
    )
    message = _refused(manifest, CORE_IN_ALLOWLIST_CODE)
    assert "for more than the named modules of one business module" in message


@pytest.mark.parametrize(
    "file",
    [
        "*",
        "**",
        "src/*",
        "src/*/tests/*",
        "src/sandloom_or*/service.py",
        "src/sandloom_orders",
        "src/sandloom_orders*",
        "src/[s]andloom_orders/service.py",
        "src/sandloom_o?ders/service.py",
        "src/../src/sandloom_orders/service.py",
        "other/sandloom_orders/service.py",
        "/src/sandloom_orders/service.py",
        "src/",
    ],
)
def test_a_framework_tree_allowlist_entry_for_a_kernel_module_names_one_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file: str
) -> None:
    manifest = _framework_tree(tmp_path, {}, monkeypatch)
    manifest.write_text(
        manifest.read_text() + _allowlist(file, ["sandloom.core.database*"])
    )
    message = _refused(manifest, CORE_IN_ALLOWLIST_CODE)
    assert f"file {file!r} admits sandloom.core.database*" in message


@pytest.mark.parametrize(
    ("file", "imports"),
    [
        (
            "src/sandloom_orders/tests/*",
            _ONE_KERNEL_MODULE["a-module-and-its-children"],
        ),
        ("src/sandloom_orders/service.py", _ONE_KERNEL_MODULE["core-itself"]),
        ("src/sandloom_orders/service.py", _ONE_KERNEL_MODULE["a-module"]),
        ("src/sandloom_orders/*", _ONE_KERNEL_MODULE["a-module-and-another"]),
        ("src/sandloom_orders/service.py", _ONE_KERNEL_MODULE["one-private-layer"]),
        (
            "src/sandloom_orders/service.py",
            _ONE_KERNEL_MODULE["one-private-layer-and-its-modules"],
        ),
        ("src/sandloom_orders/api/*", ["sandloom.core.nothing_yet*"]),
    ],
)
def test_a_framework_tree_allowlist_entry_for_one_module_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file: str, imports: list[str]
) -> None:
    manifest = _framework_tree(tmp_path, {}, monkeypatch)
    manifest.write_text(manifest.read_text() + _allowlist(file, imports))
    assert load_rules(manifest).allowlist == [{"file": file, "imports": imports}]


@pytest.mark.parametrize(
    "imports",
    [["sandloom.sdk._prim*"], ["sandloom.sdk._decl._*"], ["sandloom.core.nothing_yet"]],
)
def test_a_private_layer_or_a_named_kernel_module_needs_its_file_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, imports: list[str]
) -> None:
    manifest = _framework_tree(tmp_path, {}, monkeypatch)
    manifest.write_text(manifest.read_text() + _allowlist("src/*", imports))
    _refused(manifest, CORE_IN_ALLOWLIST_CODE)


@pytest.mark.parametrize(
    ("file", "imports"),
    [
        ("*", ["sqlalchemy*"]),
        ("acme/modules/*", ["acme.modules.shared*"]),
        ("acme/*", ["sandloom.shared*"]),
        ("*", ["sandloom.driverkit*"]),
        ("acme/modules/orders/*", ["sandloom_billing*"]),
        ("*", ["sandloom_*"]),
        ("acme/modules/orders/*", ["sandloom.coredump"]),
        ("acme/modules/orders/*", ["sandloom.corex*"]),
    ],
)
def test_an_application_allowlist_entry_for_another_package_loads(
    tmp_path: Path, file: str, imports: list[str]
) -> None:
    """Not the kernel: a wide file or a wide pattern is fine."""
    rules = load_rules(_manifest(tmp_path, _APP + _allowlist(file, imports)))
    assert rules.allowlist == [{"file": file, "imports": imports}]


@pytest.mark.parametrize(
    "allowlist",
    [
        "allowlist: nope\n",
        "allowlist: {file: a, imports: [b]}\n",
        "allowlist: [nope]\n",
        "allowlist:\n  - {file: a}\n",
        "allowlist:\n  - {imports: [b]}\n",
        "allowlist:\n  - {file: a, imports: b}\n",
        "allowlist:\n  - {file: 1, imports: [b]}\n",
        'allowlist:\n  - {file: "", imports: [b]}\n',
        "allowlist:\n  - {file: a, imports: [1]}\n",
    ],
    ids=[
        "string",
        "mapping",
        "string-entry",
        "no-imports",
        "no-file",
        "imports-string",
        "file-number",
        "file-empty",
        "import-number",
    ],
)
def test_a_malformed_allowlist_is_refused(tmp_path: Path, allowlist: str) -> None:
    with pytest.raises(ManifestError, match="allowlist"):
        load_rules(_manifest(tmp_path, _APP + allowlist))


def test_an_empty_allowlist_loads(tmp_path: Path) -> None:
    assert load_rules(_manifest(tmp_path, _APP + "allowlist: []\n")).allowlist == []
    assert load_rules(_manifest(tmp_path, _APP + "allowlist:\n")).allowlist == []


# --------------------------------------------------------------------------- #
# The refusals as the commands show them
# --------------------------------------------------------------------------- #

_REFUSALS = {
    "kernel_package: orders\n": KERNEL_EXEMPT_PACKAGE_CODE,
    "framework_packages: [orders]\n": KERNEL_EXEMPT_PACKAGE_CODE,
    "test_api: [sandloom]\n": CORE_IN_API_LIST_CODE,
    "shared_api: [sandloom.core]\n": CORE_IN_API_LIST_CODE,
    _allowlist("*", ["sandloom.core*"]): CORE_IN_ALLOWLIST_CODE,
    _allowlist(_ORDERS, ["sandloom.core"]): CORE_IN_ALLOWLIST_CODE,
    _allowlist(_ORDERS, ["sandloom.cor?"]): CORE_IN_ALLOWLIST_CODE,
}


@pytest.mark.parametrize("extra", list(_REFUSALS), ids=list(_REFUSALS.values()))
def test_sandloom_check_stops_on_the_refusal_with_exit_code_2(
    tmp_path: Path, extra: str, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _app(
        tmp_path,
        {"leak.py": "async def f(db):\n    await db.rollback()\n"},
        manifest="public_api:\n  - sandloom.sdk\n" + extra,
    )
    assert check_main(["--rules", str(manifest)]) == 2  # not 1: no module was scanned
    err = capsys.readouterr().err
    assert _REFUSALS[extra] in err
    assert "docs/operate/upgrade.md" in err


@pytest.mark.parametrize("extra", list(_REFUSALS), ids=list(_REFUSALS.values()))
def test_sandloom_add_does_not_send_a_refused_manifest_to_the_project_root(
    tmp_path: Path, extra: str
) -> None:
    _app(tmp_path, {}, manifest="public_api:\n  - sandloom.sdk\n" + extra)
    with pytest.raises(GeneratorError) as raised:
        add_layout(tmp_path)
    assert _REFUSALS[extra] in str(raised.value)
    assert "from the project root" not in str(raised.value)


def test_sandloom_check_of_the_same_app_without_the_refusal_reports_the_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _app(
        tmp_path, {"leak.py": "async def f(db):\n    await db.rollback()\n"}
    )
    assert check_main(["--rules", str(manifest)]) == 1
    capsys.readouterr()


# --------------------------------------------------------------------------- #
# The catalogue and the pages that explain it
# --------------------------------------------------------------------------- #

_NEW_CODES = (
    KERNEL_EXEMPT_PACKAGE_CODE,
    CORE_IN_API_LIST_CODE,
    CORE_IN_ALLOWLIST_CODE,
)


def test_the_new_refusals_are_listed_as_manifest_refusals() -> None:
    assert set(_NEW_CODES) <= set(MANIFEST_REFUSAL_CODES)
    assert CORE_IN_PUBLIC_API_CODE in MANIFEST_REFUSAL_CODES


@pytest.mark.parametrize("code", _NEW_CODES)
def test_a_new_refusal_is_catalogued_as_a_check_with_the_upgrade_page(
    code: str,
) -> None:
    entry = error_codes.get(code)
    assert entry.kind == "check"
    assert "docs/operate/upgrade.md" in entry.fix
    assert entry.docs == "docs/operate/upgrade.md"
    page = (BACKEND / "docs" / "operate" / "upgrade.md").read_text(encoding="utf-8")
    assert f"reference/error-codes.md#{code.lower()}" in page


def test_the_c038_text_no_longer_says_the_other_keys_are_unchecked() -> None:
    explain = error_codes.get(CORE_IN_PUBLIC_API_CODE).explain
    assert "not covered" not in explain
    assert all(code in explain for code in _NEW_CODES)
    page = (BACKEND / "docs" / "operate" / "upgrade.md").read_text(encoding="utf-8")
    assert "are not checked for a kernel entry" not in page


def test_the_framework_imports_text_names_the_sys_modules_reads() -> None:
    explain = error_codes.get("SANDLOOM-C002").explain
    assert "sys.modules" in explain
    assert "framework-imports <reason>" in explain
