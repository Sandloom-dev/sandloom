"""Per-process CPU counters on macOS: ``proc_pid_rusage(RUSAGE_INFO_V6)``.

Beyond psutil's user/sys time: instructions and cycles retired, the share of
both on PERFORMANCE cores (``ri_user_ptime`` / ``ri_pinstructions`` …), idle
package / interrupt wakeups and the time spent runnable but not running. On
Apple Silicon CPU *time* is not CPU *work*: a thread on an efficiency core,
or on a core at a lower clock, needs more µs for the same instructions.

    from darwin_rusage import rusage
    rusage(pid) -> dict | None   (None off macOS / on error)
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

_FIELDS = {  # index into the uint64 array after the 16-byte uuid
    "user_time": 0,
    "system_time": 1,
    "pkg_idle_wkups": 2,
    "interrupt_wkups": 3,
    "instructions": 29,
    "cycles": 30,
    "runnable_time": 34,
    "user_ptime": 36,
    "system_ptime": 37,
    "pinstructions": 38,
    "pcycles": 39,
}
_TIME_FIELDS = ("user_time", "system_time", "runnable_time", "user_ptime", "system_ptime")
_lib = None
_timebase: float | None = None


def _load() -> bool:
    global _lib, _timebase
    if _lib is not None:
        return True
    if sys.platform != "darwin":
        return False
    _lib = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")

    class _TB(ctypes.Structure):
        _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    tb = _TB()
    libc.mach_timebase_info(ctypes.byref(tb))
    _timebase = tb.numer / tb.denom  # mach ticks -> ns
    return True


def rusage(pid: int) -> dict[str, float] | None:
    if not _load():
        return None
    buf = (ctypes.c_uint64 * 80)()
    if _lib.proc_pid_rusage(pid, 6, ctypes.byref(buf)) != 0:  # RUSAGE_INFO_V6
        return None
    raw = list(buf)[2:]
    out: dict[str, float] = {}
    for name, i in _FIELDS.items():
        v = float(raw[i])
        if name in _TIME_FIELDS:
            v = v * (_timebase or 1.0) / 1e3  # -> µs
        out[name] = v
    return out


if __name__ == "__main__":
    import os

    print(rusage(int(sys.argv[1]) if len(sys.argv) > 1 else os.getpid()))
