"""
Pending inbound attachment persistence (attachment-staging state axis).

Ported from FOCUS ``bot/stores/pending_attachment_store.py`` under KITE
vocabulary (docs/contracts/images.md §1): TTL'd records for staged inbound
attachments, keyed by ``(sender_open_id, chat_id)``, so the next text prompt
from the same sender in the same chat can consume them (consume-once, with
restore on submit failure).

Discipline:
- single writer (kited) serialized on the RuntimeLoop; in-process lock +
  atomic tmp+rename writes, like the binding store;
- ``take`` is consume-once for the target key AND purges every other key's
  expired records in the same pass (the TTL lazy sweep);
- records are strictly validated on write; a corrupt or unreadable file
  reads as empty (same stance as the event cursor store: these records are
  TTL'd transients, and a lost record only leaves a bounded staged file
  behind), with a warning logged.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
from dataclasses import asdict, dataclass

logger = logging.getLogger("kite.stores.pending_attachments")

PENDING_ATTACHMENT_STORE_SCHEMA_VERSION = 1

# Attachment kinds the store will persist; the first cut of the images
# contract admits images only, but the record keeps the kind explicit so a
# later widening does not need a schema change.
ATTACHMENT_TYPE_IMAGE = "image"
VALID_ATTACHMENT_TYPES = frozenset({ATTACHMENT_TYPE_IMAGE})


@dataclass(frozen=True, slots=True)
class PendingAttachmentRecord:
    sender_open_id: str
    chat_id: str
    message_id: str
    attachment_type: str
    display_name: str
    media_type: str
    local_path: str
    created_at: float
    expires_at: float


class PendingAttachmentStore:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._lock = threading.Lock()

    def add(self, record: PendingAttachmentRecord) -> None:
        self.add_many((record,))

    def add_many(
        self,
        records: tuple[PendingAttachmentRecord, ...] | list[PendingAttachmentRecord],
    ) -> None:
        normalized = [self._validate_record(record) for record in records]
        if not normalized:
            return
        with self._lock:
            data = self._read_all()
            data.extend(normalized)
            self._write_all(data)

    def take(
        self,
        *,
        sender_open_id: str,
        chat_id: str,
        now: float,
    ) -> tuple[tuple[PendingAttachmentRecord, ...], tuple[PendingAttachmentRecord, ...]]:
        """Consume the key's active records; returns ``(active, expired)``.

        Consume-once: the target key's records leave the store whether they
        are active or expired. Every other key's expired records are purged
        in the same write (the lazy sweep), so the caller can delete their
        staged files.
        """
        normalized_sender = str(sender_open_id or "").strip()
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_sender or not normalized_chat_id:
            raise ValueError("sender_open_id and chat_id must not be empty")
        active: list[PendingAttachmentRecord] = []
        expired: list[PendingAttachmentRecord] = []
        remaining: list[PendingAttachmentRecord] = []
        with self._lock:
            for record in self._read_all():
                is_target = (
                    record.sender_open_id == normalized_sender
                    and record.chat_id == normalized_chat_id
                )
                if is_target:
                    if record.expires_at <= now:
                        expired.append(record)
                    else:
                        active.append(record)
                    continue
                if record.expires_at <= now:
                    expired.append(record)
                else:
                    remaining.append(record)
            self._write_all(remaining)
        active.sort(key=lambda item: (item.created_at, item.message_id, item.local_path))
        expired.sort(key=lambda item: (item.created_at, item.message_id, item.local_path))
        return tuple(active), tuple(expired)

    def cleanup_expired(self, *, now: float) -> tuple[PendingAttachmentRecord, ...]:
        """Purge every expired record (lazy sweep on each new attachment)."""
        expired: list[PendingAttachmentRecord] = []
        kept: list[PendingAttachmentRecord] = []
        with self._lock:
            for record in self._read_all():
                if record.expires_at <= now:
                    expired.append(record)
                else:
                    kept.append(record)
            self._write_all(kept)
        expired.sort(key=lambda item: (item.created_at, item.message_id, item.local_path))
        return tuple(expired)

    def list_all(self) -> tuple[PendingAttachmentRecord, ...]:
        with self._lock:
            items = sorted(
                self._read_all(),
                key=lambda item: (
                    item.sender_open_id,
                    item.chat_id,
                    item.created_at,
                    item.message_id,
                    item.local_path,
                ),
            )
        return tuple(items)

    def _file_path(self) -> pathlib.Path:
        return self._data_dir / "pending_attachments.json"

    def _read_all(self) -> list[PendingAttachmentRecord]:
        path = self._file_path()
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("root must be an object")
            if raw.get("schema_version") != PENDING_ATTACHMENT_STORE_SCHEMA_VERSION:
                raise ValueError("unsupported schema_version")
            raw_items = raw.get("attachments")
            if not isinstance(raw_items, list):
                raise ValueError("attachments must be a list")
            return [self._validate_record(self._record_from_dict(item)) for item in raw_items]
        except Exception as exc:
            # Corruption reads as empty (see the module docstring); the next
            # write self-heals the file.
            logger.warning("ignoring unreadable %s: %s", path, exc)
            return []

    def _write_all(self, records: list[PendingAttachmentRecord]) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": PENDING_ATTACHMENT_STORE_SCHEMA_VERSION,
            "attachments": [asdict(record) for record in records],
        }
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    @staticmethod
    def _validate_record(record: PendingAttachmentRecord) -> PendingAttachmentRecord:
        normalized = PendingAttachmentRecord(
            sender_open_id=str(record.sender_open_id or "").strip(),
            chat_id=str(record.chat_id or "").strip(),
            message_id=str(record.message_id or "").strip(),
            attachment_type=str(record.attachment_type or "").strip(),
            display_name=str(record.display_name or "").strip(),
            media_type=str(record.media_type or "").strip(),
            local_path=str(record.local_path or "").strip(),
            created_at=float(record.created_at),
            expires_at=float(record.expires_at),
        )
        if not normalized.sender_open_id:
            raise ValueError("pending attachment: sender_open_id must not be empty")
        if not normalized.chat_id:
            raise ValueError("pending attachment: chat_id must not be empty")
        if not normalized.message_id:
            raise ValueError("pending attachment: message_id must not be empty")
        if normalized.attachment_type not in VALID_ATTACHMENT_TYPES:
            raise ValueError(
                f"pending attachment: attachment_type must be one of "
                f"{sorted(VALID_ATTACHMENT_TYPES)}"
            )
        if not normalized.local_path:
            raise ValueError("pending attachment: local_path must not be empty")
        if not normalized.expires_at > normalized.created_at:
            raise ValueError("pending attachment: expires_at must be after created_at")
        return normalized

    @classmethod
    def _record_from_dict(cls, raw: object) -> PendingAttachmentRecord:
        if not isinstance(raw, dict):
            raise ValueError("attachment entry must be an object")
        try:
            return PendingAttachmentRecord(
                sender_open_id=str(raw["sender_open_id"]),
                chat_id=str(raw["chat_id"]),
                message_id=str(raw["message_id"]),
                attachment_type=str(raw["attachment_type"]),
                display_name=str(raw.get("display_name", "")),
                media_type=str(raw.get("media_type", "")),
                local_path=str(raw["local_path"]),
                created_at=float(raw["created_at"]),
                expires_at=float(raw["expires_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("attachment entry has invalid fields") from exc
