"""Records whether collect mode is on while it registers, then refuses (KB-55, P11).

A serving entry point must never run in collect mode (ADR-0015 decision 8):
the probe writes whether the kernel's switch is on to the file ``KB_PROBE_OUT``
names and stops the process with a refusal, so a test can run every entry point
that builds the composition and read what each saw. The module reads the switch
through ``importlib`` (a test seam: a business module never imports the kernel).
"""

import importlib
import os

from sandloom.sdk import define_module


def _setup(core: object) -> None:
    switch = importlib.import_module("sandloom.core.config.module_settings")
    state = "on" if switch.collecting() is not None else "off"
    with open(os.environ["KB_PROBE_OUT"], "a", encoding="utf-8") as out:
        out.write(state + "\n")
    raise SystemExit("kb_collect_probe: stop here")


register = define_module(__name__, version="0.1.0", router=None, setup=_setup)
