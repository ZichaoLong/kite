"""
Terminal result text persistence.

When a prompt finishes, the terminal result card text is stored locally so
`/last`-style commands can read it back after restarts. Records are keyed by
the Feishu message id of the terminal card; `session_id` scopes lookups.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
from dataclasses import asdict, dataclass

_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TerminalResultRecord:
    message_id: str
    execution_message_id: str
    final_reply_text: str
    recorded_at: float
    terminal_result_id: str = ""
    session_id: str = ""
    checksum: str = ""


class TerminalResultStore:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._lock = threading.Lock()

    def upsert(self, record: TerminalResultRecord) -> None:
        normalized = self._normalize_record(record)
        if not normalized.message_id or not normalized.final_reply_text:
            return
        try:
            with self._lock:
                records = [
                    item
                    for item in self._read_all()
                    if item.message_id != normalized.message_id
                    and (
                        not normalized.terminal_result_id
                        or item.terminal_result_id != normalized.terminal_result_id
                    )
                ]
                records.append(normalized)
                self._write_all(records)
        except Exception as exc:
            logger.warning("terminal result store upsert failed: %s", exc)

    def get(self, message_id: str) -> str:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return ""
        try:
            with self._lock:
                for item in self._read_all():
                    if item.message_id == normalized_message_id:
                        return item.final_reply_text
        except Exception as exc:
            logger.warning("terminal result store get failed: %s", exc)
        return ""

    def get_by_terminal_result_id(
        self,
        terminal_result_id: str,
        *,
        checksum: str = "",
        session_id: str = "",
    ) -> str:
        normalized_result_id = str(terminal_result_id or "").strip().lower()
        normalized_checksum = str(checksum or "").strip().lower()
        normalized_session_id = str(session_id or "").strip()
        if not normalized_result_id:
            return ""
        try:
            with self._lock:
                for item in reversed(self._read_all()):
                    if item.terminal_result_id != normalized_result_id:
                        continue
                    if normalized_session_id and item.session_id and item.session_id != normalized_session_id:
                        continue
                    if normalized_checksum and item.checksum and not item.checksum.startswith(normalized_checksum):
                        continue
                    return item.final_reply_text
        except Exception as exc:
            logger.warning("terminal result store get_by_terminal_result_id failed: %s", exc)
        return ""

    def latest_for_session(self, session_id: str) -> str:
        normalized_session_id = str(session_id or "").strip()
        try:
            with self._lock:
                for item in sorted(
                    self._read_all(),
                    key=lambda record: (record.recorded_at, record.message_id),
                    reverse=True,
                ):
                    if normalized_session_id and item.session_id != normalized_session_id:
                        continue
                    return item.final_reply_text
        except Exception as exc:
            logger.warning("terminal result store latest_for_session failed: %s", exc)
        return ""

    def has_execution_result(self, *, execution_message_id: str, final_reply_text: str) -> bool:
        normalized_execution_message_id = str(execution_message_id or "").strip()
        text = str(final_reply_text or "")
        if not normalized_execution_message_id or not text:
            return False
        try:
            with self._lock:
                return any(
                    item.execution_message_id == normalized_execution_message_id
                    and item.final_reply_text == text
                    for item in self._read_all()
                )
        except Exception as exc:
            logger.warning("terminal result store has_execution_result failed: %s", exc)
            return False

    def list_all(self) -> tuple[TerminalResultRecord, ...]:
        try:
            with self._lock:
                items = sorted(
                    self._read_all(),
                    key=lambda item: (item.recorded_at, item.message_id),
                )
        except Exception as exc:
            logger.warning("terminal result store list_all failed: %s", exc)
            return ()
        return tuple(items)

    def _file_path(self) -> pathlib.Path:
        return self._data_dir / "terminal_results.json"

    def _read_all(self) -> list[TerminalResultRecord]:
        path = self._file_path()
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise RuntimeError("terminal_results.json is corrupt: root must be an object.")
        schema_version = int(raw.get("schema_version", 0) or 0)
        if schema_version != _SCHEMA_VERSION:
            raise RuntimeError(
                f"terminal_results.json schema_version={schema_version}, expected {_SCHEMA_VERSION}."
            )
        raw_items = raw.get("results")
        if not isinstance(raw_items, list):
            raise RuntimeError("terminal_results.json is corrupt: results must be a list.")
        return [self._record_from_dict(item) for item in raw_items]

    def _write_all(self, records: list[TerminalResultRecord]) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "results": [asdict(record) for record in records],
        }
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    @staticmethod
    def _normalize_record(record: TerminalResultRecord) -> TerminalResultRecord:
        return TerminalResultRecord(
            message_id=str(record.message_id or "").strip(),
            execution_message_id=str(record.execution_message_id or "").strip(),
            final_reply_text=str(record.final_reply_text or ""),
            recorded_at=float(record.recorded_at),
            terminal_result_id=str(record.terminal_result_id or "").strip().lower(),
            session_id=str(record.session_id or "").strip(),
            checksum=str(record.checksum or "").strip().lower(),
        )

    @classmethod
    def _record_from_dict(cls, raw: object) -> TerminalResultRecord:
        if not isinstance(raw, dict):
            raise RuntimeError("terminal_results.json is corrupt: result entries must be objects.")
        try:
            return cls._normalize_record(
                TerminalResultRecord(
                    message_id=str(raw["message_id"]),
                    execution_message_id=str(raw.get("execution_message_id", "")),
                    final_reply_text=str(raw["final_reply_text"]),
                    recorded_at=float(raw["recorded_at"]),
                    terminal_result_id=str(raw.get("terminal_result_id", "")),
                    session_id=str(raw.get("session_id", "")),
                    checksum=str(raw.get("checksum", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("terminal_results.json is corrupt: result entry fields are invalid.") from exc
