import ctypes
import os
import sys
from pathlib import Path


class _BSDInfo(ctypes.Structure):
    # Darwin <sys/proc_info.h>, struct proc_bsdinfo (PROC_PIDTBSDINFO).
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("exit_status", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("ids", ctypes.c_uint32 * 7),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("details", ctypes.c_uint32 * 6),
        ("seconds", ctypes.c_uint64),
        ("microseconds", ctypes.c_uint64),
    ]

    status: int
    pid: int
    seconds: int
    microseconds: int


def birth(pid: int) -> str | None:
    """None means confirmed dead; raise if the identity cannot be verified."""
    if pid <= 0:
        raise ValueError("Process identity requires a positive PID")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None

    if sys.platform == "darwin":
        info = _BSDInfo()
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        result = libproc.proc_pidinfo(
            pid, 3, ctypes.c_uint64(0), ctypes.byref(info), ctypes.sizeof(info)
        )
        if result == ctypes.sizeof(info) and info.pid == pid:
            return None if info.status == 5 else f"{info.seconds}:{info.microseconds}"
    elif sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            pass
        else:
            fields = stat[stat.rindex(")") + 2 :].split()
            if fields[0] == "Z":
                return None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return f"{boot}:{fields[19]}"
    else:
        raise OSError(f"Process identity is unsupported on {sys.platform}")

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    raise OSError(f"Cannot verify birth of PID {pid}")
