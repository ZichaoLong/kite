"""
Cross-platform advisory file locks.
"""

from __future__ import annotations

import os


class FileLockBusyError(BlockingIOError):
    """Raised when a non-blocking file lock cannot be acquired."""


def _ensure_lock_file(file_obj) -> None:
    file_obj.seek(0, os.SEEK_END)
    if file_obj.tell() == 0:
        file_obj.write("\0")
        file_obj.flush()
    file_obj.seek(0)


if os.name == "nt":
    import msvcrt

    def acquire_file_lock(file_obj, *, blocking: bool) -> None:
        _ensure_lock_file(file_obj)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            file_obj.seek(0)
            msvcrt.locking(file_obj.fileno(), mode, 1)
        except OSError as exc:
            raise FileLockBusyError(str(exc)) from exc

    def release_file_lock(file_obj) -> None:
        _ensure_lock_file(file_obj)
        file_obj.seek(0)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def acquire_file_lock(file_obj, *, blocking: bool) -> None:
        _ensure_lock_file(file_obj)
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(file_obj.fileno(), flags)
        except BlockingIOError as exc:
            raise FileLockBusyError(str(exc)) from exc

    def release_file_lock(file_obj) -> None:
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
