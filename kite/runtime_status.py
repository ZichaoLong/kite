"""
Best-effort runtime status published by kited for kitectl.

kitectl is a separate process and cannot ask kited for its WS connection age
or last resync time directly, so kited publishes them as a small JSON file in
the data directory (atomic write, same discipline as the stores). The file is
removed on clean shutdown; a file whose recorded kited pid is no longer alive
is treated as absent by readers. This is observability only — never a
control channel.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from typing import Any, Mapping

from kite.file_permissions import ensure_private_file_permissions

_SCHEMA_VERSION = 1
_FILE_NAME = "runtime_status.json"


class RuntimeStatusWriter:
    """Thread-safe incremental writer (kited callbacks update it live)."""

    def __init__(self, data_dir: pathlib.Path | str) -> None:
        self._path = pathlib.Path(data_dir) / _FILE_NAME
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "kited_pid": os.getpid(),
            "started_at": time.time(),
        }

    @property
    def path(self) -> pathlib.Path:
        return self._path

    def update(self, **fields: Any) -> None:
        """Merge fields into the status; dict values merge one level deep."""
        with self._lock:
            for key, value in fields.items():
                if isinstance(value, Mapping) and isinstance(self._status.get(key), dict):
                    merged = dict(self._status[key])
                    merged.update(value)
                    self._status[key] = merged
                else:
                    self._status[key] = value
            self._status["updated_at"] = time.time()
            self._write()

    def clear(self) -> None:
        with self._lock:
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(f"{self._path.name}.tmp")
        tmp_path.write_text(
            json.dumps(self._status, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ensure_private_file_permissions(tmp_path)
        os.replace(tmp_path, self._path)


def read_runtime_status(data_dir: pathlib.Path | str) -> dict[str, Any] | None:
    """Read the published status; None when absent or unreadable."""
    path = pathlib.Path(data_dir) / _FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        return None
    return raw
