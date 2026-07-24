"""Per-chat group activation config persistence (state axis 5).

Implements the group-chat contract (docs/contracts/group-chat.md): one
persistent, chat-keyed record ``{activated, activated_by, activated_at,
mode}`` per group chat. The first cut admits ``mention_only`` groups only.

Fail-closed discipline (contract §4.3): reads never raise on corruption.
A corrupt store file or a corrupt per-chat record reads as *non-activated*
(fail closed to silence, never to open); the bad record is skipped and a
warning is logged. Writes stay strict — an invalid record being written is a
programming error and raises, exactly like BindingStore. Writes are atomic
(tmp file + os.replace).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

GROUP_CONFIG_STORE_SCHEMA_VERSION = 1
SUPPORTED_GROUP_CONFIG_STORE_SCHEMA_VERSIONS = frozenset(
    {GROUP_CONFIG_STORE_SCHEMA_VERSION}
)

GROUP_MODE_MENTION_ONLY = "mention_only"
VALID_GROUP_MODES = frozenset({GROUP_MODE_MENTION_ONLY})
DEFAULT_GROUP_MODE = GROUP_MODE_MENTION_ONLY


class StoredGroupConfig(TypedDict):
    activated: bool
    activated_by: str
    activated_at: float
    mode: str


class GroupConfigStore:
    """Chat-keyed group activation config (axis 5; fail-closed reads)."""

    def __init__(self, data_dir: pathlib.Path):
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def load(self, chat_id: str) -> StoredGroupConfig | None:
        """The chat's group config, or None when absent or corrupt."""
        normalized_chat_id = self._normalize_chat_id(chat_id)
        with self._lock:
            data = self._read_all()
            state = data["groups"].get(normalized_chat_id)
            return dict(state) if state is not None else None

    def is_activated(self, chat_id: str) -> bool:
        """Whether the chat is an activated group (corrupt -> False)."""
        state = self.load(chat_id)
        return bool(state is not None and state["activated"])

    def save(self, chat_id: str, state: StoredGroupConfig) -> StoredGroupConfig:
        normalized_chat_id = self._normalize_chat_id(chat_id)
        normalized_state = self._validate_group_config(state, context="save")
        with self._lock:
            data = self._read_all()
            data["groups"][normalized_chat_id] = dict(normalized_state)
            self._write_all(data)
        return dict(normalized_state)

    def activate(
        self,
        chat_id: str,
        *,
        activated_by: str,
        activated_at: float | None = None,
    ) -> StoredGroupConfig:
        """Mark the chat activated (mention_only) by ``activated_by``."""
        normalized_by = str(activated_by or "").strip()
        if not normalized_by:
            raise ValueError("activated_by must not be empty")
        at = time.time() if activated_at is None else float(activated_at)
        existing = self.load(chat_id)
        mode = existing["mode"] if existing is not None else DEFAULT_GROUP_MODE
        return self.save(
            chat_id,
            {
                "activated": True,
                "activated_by": normalized_by,
                "activated_at": at,
                "mode": mode,
            },
        )

    def deactivate(self, chat_id: str) -> StoredGroupConfig:
        """Mark the chat non-activated; the mode preference is kept."""
        existing = self.load(chat_id)
        mode = existing["mode"] if existing is not None else DEFAULT_GROUP_MODE
        return self.save(
            chat_id,
            {
                "activated": False,
                "activated_by": "",
                "activated_at": 0.0,
                "mode": mode,
            },
        )

    def _state_path(self) -> pathlib.Path:
        return self._data_dir / "group_configs.json"

    @staticmethod
    def _normalize_chat_id(chat_id: str) -> str:
        normalized = str(chat_id or "").strip()
        if not normalized:
            raise ValueError("chat_id must not be empty")
        return normalized

    @staticmethod
    def _default_data() -> dict[str, Any]:
        return {
            "schema_version": GROUP_CONFIG_STORE_SCHEMA_VERSION,
            "groups": {},
        }

    def _read_all(self) -> dict[str, Any]:
        """Tolerant read (contract §4.3): corruption degrades to empty/skip.

        - missing or unparseable file -> empty store (every group reads as
          non-activated);
        - unsupported schema version -> empty store;
        - one corrupt record -> that record is skipped, the rest still load.
        """
        path = self._state_path()
        if not path.exists():
            return self._default_data()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("invalid %s: %s; reading all groups as non-activated", path.name, exc)
            return self._default_data()
        if not isinstance(raw, dict):
            logger.warning(
                "invalid %s: root must be an object; reading all groups as non-activated",
                path.name,
            )
            return self._default_data()
        schema_version = raw.get("schema_version")
        if schema_version not in SUPPORTED_GROUP_CONFIG_STORE_SCHEMA_VERSIONS:
            logger.warning(
                "invalid %s: schema_version must be one of %s; reading all groups as non-activated",
                path.name,
                sorted(SUPPORTED_GROUP_CONFIG_STORE_SCHEMA_VERSIONS),
            )
            return self._default_data()
        raw_groups = raw.get("groups", {})
        if not isinstance(raw_groups, dict):
            logger.warning(
                "invalid %s: groups must be an object; reading all groups as non-activated",
                path.name,
            )
            return self._default_data()
        groups: dict[str, StoredGroupConfig] = {}
        for chat_id, raw_state in raw_groups.items():
            normalized_chat_id = str(chat_id or "").strip()
            if not normalized_chat_id:
                logger.warning("invalid %s: empty chat_id; record skipped", path.name)
                continue
            try:
                groups[normalized_chat_id] = self._validate_group_config(
                    raw_state, context=f"group {normalized_chat_id}"
                )
            except ValueError as exc:
                logger.warning(
                    "invalid %s: %s; the group reads as non-activated", path.name, exc
                )
        return {
            "schema_version": GROUP_CONFIG_STORE_SCHEMA_VERSION,
            "groups": groups,
        }

    def _write_all(self, data: dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))

    @staticmethod
    def _validate_group_config(raw_state: Any, *, context: str) -> StoredGroupConfig:
        if not isinstance(raw_state, dict):
            raise ValueError(f"{context}: group config must be an object")

        activated = raw_state.get("activated", False)
        if not isinstance(activated, bool):
            raise ValueError(f"{context}: activated must be a boolean")

        activated_by = raw_state.get("activated_by", "")
        if not isinstance(activated_by, str):
            raise ValueError(f"{context}: activated_by must be a string")
        activated_by = activated_by.strip()
        if activated and not activated_by:
            raise ValueError(f"{context}: activated_by cannot be empty when activated")

        activated_at = raw_state.get("activated_at", 0.0)
        if isinstance(activated_at, bool) or not isinstance(activated_at, (int, float)):
            raise ValueError(f"{context}: activated_at must be a number")
        activated_at = max(float(activated_at), 0.0)

        mode = raw_state.get("mode", "")
        if not isinstance(mode, str):
            raise ValueError(f"{context}: mode must be a string")
        mode = mode.strip() or DEFAULT_GROUP_MODE
        if mode not in VALID_GROUP_MODES:
            raise ValueError(
                f"{context}: mode must be one of {sorted(VALID_GROUP_MODES)}"
            )

        return {
            "activated": activated,
            "activated_by": activated_by,
            "activated_at": activated_at,
            "mode": mode,
        }
