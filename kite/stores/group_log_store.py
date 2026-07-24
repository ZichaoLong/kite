"""Per-chat assistant message log + trigger boundary persistence (state axis 6).

Implements the assistant-mode half of the group-chat contract
(docs/contracts/group-chat.md §1): a per-chat JSONL append-only message log
with a monotonic ``seq`` plus the trigger **boundary triple** ``{seq,
created_at, message_ids}``. A timestamp alone is not a cursor — several
messages can share one millisecond — so the boundary also records every
message id seen at that millisecond, and the Feishu REST history backfill
dedups against it exactly (``kite/group_history.py``).

Layout (FOCUS ``group_chat_store.py`` log half, KITE store conventions):

- ``group_log_state.json``: one atomic JSON file with the per-chat
  ``last_seq`` counter and the boundary triple (tmp file + os.replace);
- ``group_logs/<chat_id>.jsonl``: one line per logged entry, each carrying
  its ``seq``; capped at ``GROUP_LOG_SIZE_CAP`` lines — appends beyond the
  cap drop the oldest lines (the seq counter keeps moving; the log is a
  recent-history window, and the REST backfill covers the gap).

Fail-closed discipline (contract §4, same as GroupConfigStore): reads never
raise on corruption. A corrupt state file or per-chat record reads as an
empty log (``last_seq`` 0, zero boundary); corrupt log lines are skipped.
Writes stay strict — an invalid entry or boundary being written is a
programming error and raises, exactly like BindingStore/GroupConfigStore.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

GROUP_LOG_STORE_SCHEMA_VERSION = 1
SUPPORTED_GROUP_LOG_STORE_SCHEMA_VERSIONS = frozenset(
    {GROUP_LOG_STORE_SCHEMA_VERSION}
)

# Per-chat log window: appends beyond this many lines drop the oldest.
GROUP_LOG_SIZE_CAP = 200


class GroupLogEntry(TypedDict):
    """One loggable member message (the append-time input shape)."""

    message_id: str
    created_at: int  # Feishu create_time, milliseconds
    sender_open_id: str
    sender_type: str
    sender_name: str
    msg_type: str
    text: str


class StoredGroupLogEntry(GroupLogEntry):
    """A log line as persisted: the entry plus its monotonic ``seq``."""

    seq: int


class GroupBoundary(TypedDict):
    """The trigger boundary triple (contract §1)."""

    seq: int
    created_at: int  # milliseconds
    message_ids: list[str]


class GroupLogStore:
    """Chat-keyed assistant log + boundary (axis 6; fail-closed reads)."""

    def __init__(self, data_dir: pathlib.Path, *, size_cap: int = GROUP_LOG_SIZE_CAP):
        self._data_dir = data_dir
        self._size_cap = max(int(size_cap), 1)
        self._lock = threading.Lock()

    def append(self, chat_id: str, entry: GroupLogEntry) -> int:
        """Append one entry and return its seq; drops oldest beyond the cap."""
        normalized_chat_id = self._normalize_chat_id(chat_id)
        payload = self._validate_entry(entry, context="append")
        with self._lock:
            data = self._read_state()
            chat_state = data["chats"].get(normalized_chat_id) or self._default_chat_state()
            next_seq = int(chat_state["last_seq"]) + 1
            chat_state["last_seq"] = next_seq
            data["chats"][normalized_chat_id] = chat_state
            # State first, then the log line (FOCUS ordering): a crash between
            # the two leaves last_seq ahead of the file, which reads tolerate
            # (entries_since simply finds fewer lines; seq stays monotonic).
            self._write_state(data)
            line: StoredGroupLogEntry = {**payload, "seq": next_seq}
            path = self._log_path(normalized_chat_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            self._enforce_size_cap(path)
            return next_seq

    def entries_since(self, chat_id: str, seq: int) -> list[StoredGroupLogEntry]:
        """Log entries with ``seq`` greater than the given one, in order."""
        normalized_chat_id = self._normalize_chat_id(chat_id)
        lower = max(int(seq), 0)
        entries: list[StoredGroupLogEntry] = []
        with self._lock:
            for line in self._read_log_lines(normalized_chat_id):
                entry_seq = line.get("seq")
                if not isinstance(entry_seq, int) or isinstance(entry_seq, bool):
                    continue
                if entry_seq <= lower:
                    continue
                entries.append(line)  # type: ignore[typeddict-item]
        return entries

    def boundary(self, chat_id: str) -> GroupBoundary:
        """The chat's trigger boundary triple (zero boundary when absent)."""
        normalized_chat_id = self._normalize_chat_id(chat_id)
        with self._lock:
            data = self._read_state()
            chat_state = data["chats"].get(normalized_chat_id)
            if chat_state is None:
                return self._default_boundary()
            return dict(chat_state["boundary"])

    def set_boundary(self, chat_id: str, boundary: GroupBoundary) -> GroupBoundary:
        normalized_chat_id = self._normalize_chat_id(chat_id)
        normalized_boundary = self._validate_boundary(boundary, context="set_boundary")
        with self._lock:
            data = self._read_state()
            chat_state = data["chats"].get(normalized_chat_id) or self._default_chat_state()
            chat_state["boundary"] = dict(normalized_boundary)
            data["chats"][normalized_chat_id] = chat_state
            self._write_state(data)
        return dict(normalized_boundary)

    def log_path(self, chat_id: str) -> pathlib.Path:
        return self._log_path(self._normalize_chat_id(chat_id))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _state_path(self) -> pathlib.Path:
        return self._data_dir / "group_log_state.json"

    def _log_path(self, chat_id: str) -> pathlib.Path:
        safe_chat_id = chat_id.replace("/", "_")
        return self._data_dir / "group_logs" / f"{safe_chat_id}.jsonl"

    @staticmethod
    def _normalize_chat_id(chat_id: str) -> str:
        normalized = str(chat_id or "").strip()
        if not normalized:
            raise ValueError("chat_id must not be empty")
        return normalized

    @staticmethod
    def _default_boundary() -> GroupBoundary:
        return {"seq": 0, "created_at": 0, "message_ids": []}

    @classmethod
    def _default_chat_state(cls) -> dict[str, Any]:
        return {"last_seq": 0, "boundary": cls._default_boundary()}

    @staticmethod
    def _default_data() -> dict[str, Any]:
        return {
            "schema_version": GROUP_LOG_STORE_SCHEMA_VERSION,
            "chats": {},
        }

    def _read_state(self) -> dict[str, Any]:
        """Tolerant read (contract §4): corruption degrades to an empty log.

        - missing or unparseable file -> empty state (every chat reads as an
          empty log with a zero boundary);
        - unsupported schema version -> empty state;
        - one corrupt chat record -> that record is skipped, the rest load.
        """
        path = self._state_path()
        if not path.exists():
            return self._default_data()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("invalid %s: %s; reading all logs as empty", path.name, exc)
            return self._default_data()
        if not isinstance(raw, dict):
            logger.warning(
                "invalid %s: root must be an object; reading all logs as empty",
                path.name,
            )
            return self._default_data()
        schema_version = raw.get("schema_version")
        if schema_version not in SUPPORTED_GROUP_LOG_STORE_SCHEMA_VERSIONS:
            logger.warning(
                "invalid %s: schema_version must be one of %s; reading all logs as empty",
                path.name,
                sorted(SUPPORTED_GROUP_LOG_STORE_SCHEMA_VERSIONS),
            )
            return self._default_data()
        raw_chats = raw.get("chats", {})
        if not isinstance(raw_chats, dict):
            logger.warning(
                "invalid %s: chats must be an object; reading all logs as empty",
                path.name,
            )
            return self._default_data()
        chats: dict[str, dict[str, Any]] = {}
        for chat_id, raw_state in raw_chats.items():
            normalized_chat_id = str(chat_id or "").strip()
            if not normalized_chat_id:
                logger.warning("invalid %s: empty chat_id; record skipped", path.name)
                continue
            try:
                chats[normalized_chat_id] = self._validate_chat_state(
                    raw_state, context=f"chat {normalized_chat_id}"
                )
            except ValueError as exc:
                logger.warning(
                    "invalid %s: %s; the chat reads as an empty log", path.name, exc
                )
        return {
            "schema_version": GROUP_LOG_STORE_SCHEMA_VERSION,
            "chats": chats,
        }

    def _write_state(self, data: dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))

    def _read_log_lines(self, chat_id: str) -> list[dict[str, Any]]:
        """Tolerant log read: a missing file is empty, bad lines are skipped."""
        path = self._log_path(chat_id)
        if not path.exists():
            return []
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("failed to read %s: %s; reading as empty log", path.name, exc)
            return []
        lines: list[dict[str, Any]] = []
        for raw_line in raw_lines:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except Exception:
                logger.warning("invalid %s: unparseable line skipped", path.name)
                continue
            if not isinstance(item, dict):
                logger.warning("invalid %s: non-object line skipped", path.name)
                continue
            lines.append(item)
        return lines

    def _enforce_size_cap(self, path: pathlib.Path) -> None:
        """Drop the oldest lines beyond the cap (atomic rewrite)."""
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(raw_lines) <= self._size_cap:
            return
        kept = raw_lines[-self._size_cap :]
        tmp_path = path.with_suffix(".jsonl.tmp")
        tmp_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        os.replace(str(tmp_path), str(path))

    @classmethod
    def _validate_chat_state(cls, raw_state: Any, *, context: str) -> dict[str, Any]:
        if not isinstance(raw_state, dict):
            raise ValueError(f"{context}: chat log state must be an object")
        last_seq = raw_state.get("last_seq", 0)
        if isinstance(last_seq, bool) or not isinstance(last_seq, int) or last_seq < 0:
            raise ValueError(f"{context}: last_seq must be a non-negative integer")
        boundary = cls._validate_boundary(
            raw_state.get("boundary", {}), context=context
        )
        return {"last_seq": last_seq, "boundary": boundary}

    @classmethod
    def _validate_boundary(cls, raw_boundary: Any, *, context: str) -> GroupBoundary:
        if not isinstance(raw_boundary, dict):
            raise ValueError(f"{context}: boundary must be an object")
        seq = raw_boundary.get("seq", 0)
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError(f"{context}: boundary seq must be a non-negative integer")
        created_at = raw_boundary.get("created_at", 0)
        if isinstance(created_at, bool) or not isinstance(created_at, int) or created_at < 0:
            raise ValueError(
                f"{context}: boundary created_at must be a non-negative integer (ms)"
            )
        message_ids = raw_boundary.get("message_ids", [])
        if not isinstance(message_ids, list):
            raise ValueError(f"{context}: boundary message_ids must be a list")
        normalized_ids = sorted(
            {
                str(item).strip()
                for item in message_ids
                if isinstance(item, str) and str(item).strip()
            }
        )
        return {"seq": seq, "created_at": created_at, "message_ids": normalized_ids}

    @staticmethod
    def _validate_entry(raw_entry: Any, *, context: str) -> GroupLogEntry:
        if not isinstance(raw_entry, dict):
            raise ValueError(f"{context}: log entry must be an object")

        message_id = raw_entry.get("message_id", "")
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError(f"{context}: message_id must be a non-empty string")

        created_at = raw_entry.get("created_at", 0)
        if isinstance(created_at, bool) or not isinstance(created_at, int) or created_at < 0:
            raise ValueError(f"{context}: created_at must be a non-negative integer (ms)")

        text = raw_entry.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{context}: text must be a non-empty string")

        def _str_field(key: str) -> str:
            value = raw_entry.get(key, "")
            if not isinstance(value, str):
                raise ValueError(f"{context}: {key} must be a string")
            return value.strip()

        return {
            "message_id": message_id.strip(),
            "created_at": created_at,
            "sender_open_id": _str_field("sender_open_id"),
            "sender_type": _str_field("sender_type") or "user",
            "sender_name": _str_field("sender_name"),
            "msg_type": _str_field("msg_type") or "text",
            "text": text,
        }
