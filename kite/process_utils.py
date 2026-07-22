"""
Process existence helpers.
"""

from __future__ import annotations

import ctypes
import os
import pathlib

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _linux_process_state(pid: int) -> str:
    status_path = pathlib.Path("/proc") / str(pid) / "status"
    try:
        with status_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                if raw_line.startswith("State:"):
                    parts = raw_line.split()
                    if len(parts) >= 2:
                        return str(parts[1]).strip().upper()
                    return ""
    except OSError:
        return ""
    return ""


def process_exists(pid: int) -> bool:
    normalized_pid = int(pid or 0)
    if normalized_pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(normalized_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        if _linux_process_state(normalized_pid) == "Z":
            return False
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, normalized_pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True
