"""
Per-session durable event cursor persistence.

The adapter resubscribes to a session's event stream with the last consumed
``{seq, epoch}`` cursor (see docs/architecture/kite-design.md §5). Cursors are
stored per session_id so a kited restart can resume without replaying the
whole journal. A lost or corrupt cursor is not fatal: the adapter falls back
to a REST snapshot rebuild, so this store treats unreadable data as empty.

Reads and writes are serialized with an in-process lock plus a cross-process
advisory file lock (`.lock` sidecar), and writes are atomic (tmp + rename).
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from kite.file_lock import acquire_file_lock, release_file_lock
from kite.file_permissions import ensure_private_file_permissions

_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class EventCursor:
    seq: int
    epoch: str


class EventCursorStore:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._lock = threading.Lock()

    def get(self, session_id: str) -> EventCursor | None:
        normalized_session_id = self._normalize_session_id(session_id)
        with self._locked_data() as data:
            return self._cursor_from_data(data.get(normalized_session_id))

    def set(self, session_id: str, cursor: EventCursor) -> None:
        normalized_session_id = self._normalize_session_id(session_id)
        normalized = self._normalize_cursor(cursor)
        with self._locked_data() as data:
            data[normalized_session_id] = {"seq": normalized.seq, "epoch": normalized.epoch}
            self._write_all(data)

    def clear(self, session_id: str) -> None:
        normalized_session_id = self._normalize_session_id(session_id)
        with self._locked_data() as data:
            if data.pop(normalized_session_id, None) is None:
                return
            self._write_all(data)

    def _file_path(self) -> pathlib.Path:
        return self._data_dir / "event_cursors.json"

    def _lock_path(self) -> pathlib.Path:
        return self._data_dir / "event_cursors.lock"

    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        normalized = str(session_id or "").strip()
        if not normalized:
            raise ValueError("session_id must not be empty")
        return normalized

    @staticmethod
    def _normalize_cursor(cursor: EventCursor) -> EventCursor:
        seq = cursor.seq
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError("cursor seq must be a non-negative integer")
        epoch = str(cursor.epoch or "").strip()
        if not epoch:
            raise ValueError("cursor epoch must not be empty")
        return EventCursor(seq=seq, epoch=epoch)

    @staticmethod
    def _cursor_from_data(raw: object) -> EventCursor | None:
        if not isinstance(raw, dict):
            return None
        seq = raw.get("seq")
        epoch = raw.get("epoch")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            return None
        if not isinstance(epoch, str) or not epoch.strip():
            return None
        return EventCursor(seq=seq, epoch=epoch.strip())

    @contextmanager
    def _locked_data(self) -> Iterator[dict[str, dict]]:
        with self._lock:
            lock_path = self._lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                acquire_file_lock(lock_file, blocking=True)
                try:
                    yield self._read_all()
                finally:
                    release_file_lock(lock_file)

    def _read_all(self) -> dict[str, dict]:
        path = self._file_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        if raw.get("schema_version") != _SCHEMA_VERSION:
            return {}
        cursors = raw.get("cursors")
        if not isinstance(cursors, dict):
            return {}
        return {
            session_id: value
            for session_id, value in cursors.items()
            if isinstance(value, dict)
        }

    def _write_all(self, data: dict[str, dict]) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "cursors": {key: data[key] for key in sorted(data)},
        }
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        ensure_private_file_permissions(tmp_path)
        os.replace(tmp_path, path)
