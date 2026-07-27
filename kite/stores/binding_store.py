"""
Feishu chat binding persistence.

Only local binding facts that must survive a kited restart live here:
- the chat ↔ session bookmark
- whether the chat is attached (receives Feishu pushes for that session)
- the binding-level permission mode (kap ``permission_mode``)
- the binding-level plan mode (kap ``plan_mode``)
- the binding-level thinking effort (kap ``thinking``; "" = unset → omitted
  on submit)

One-shot goal controls (kap ``goal_control`` pause/resume/cancel) are
deliberately NOT persisted: they attach to the next prompt only, so they
live in AppHandler's in-memory per-chat slot and a restart simply drops a
pending one (docs/contracts/mvp-scope.md §2 /goal row).

Session metadata (title, cwd, history) has ``~/.kimi-code`` as its single
source of truth and is never mirrored here. Transient runtime state (active
prompts, execution cards, pending approvals) is not persisted.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from typing import Any, TypedDict

BINDING_STORE_SCHEMA_VERSION = 1
SUPPORTED_BINDING_STORE_SCHEMA_VERSIONS = frozenset({BINDING_STORE_SCHEMA_VERSION})

PERMISSION_MODE_AUTO = "auto"
PERMISSION_MODE_MANUAL = "manual"
PERMISSION_MODE_YOLO = "yolo"
VALID_PERMISSION_MODES = frozenset(
    {PERMISSION_MODE_AUTO, PERMISSION_MODE_MANUAL, PERMISSION_MODE_YOLO}
)
DEFAULT_PERMISSION_MODE = PERMISSION_MODE_AUTO

DEFAULT_ATTACHED = True
DEFAULT_PLAN_MODE = False

# kap per-prompt ``thinking`` levels (packages/protocol/src/rest/prompt.ts
# promptThinkingSchema; the enum list is the verified upstream set).
EFFORT_OFF = "off"
EFFORT_LOW = "low"
EFFORT_MEDIUM = "medium"
EFFORT_HIGH = "high"
EFFORT_XHIGH = "xhigh"
EFFORT_MAX = "max"
VALID_EFFORTS = frozenset(
    {EFFORT_OFF, EFFORT_LOW, EFFORT_MEDIUM, EFFORT_HIGH, EFFORT_XHIGH, EFFORT_MAX}
)
# "" = unset: no explicit thinking level is carried on prompts.
DEFAULT_EFFORT = ""
# "" = no goal objective carried on prompts.


class StoredBinding(TypedDict):
    session_id: str
    attached: bool
    permission_mode: str
    plan_mode: bool
    effort: str


class BindingStore:
    def __init__(self, data_dir: pathlib.Path):
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def load(self, chat_id: str) -> StoredBinding | None:
        normalized_chat_id = self._normalize_chat_id(chat_id)
        with self._lock:
            data = self._read_all()
            state = data["bindings"].get(normalized_chat_id)
            return dict(state) if state is not None else None

    def load_all(self) -> dict[str, StoredBinding]:
        with self._lock:
            data = self._read_all()
            return {chat_id: dict(state) for chat_id, state in data["bindings"].items()}

    def save(self, chat_id: str, state: StoredBinding) -> StoredBinding:
        normalized_chat_id = self._normalize_chat_id(chat_id)
        normalized_state = self._validate_stored_binding(state)
        with self._lock:
            data = self._read_all()
            data["bindings"][normalized_chat_id] = dict(normalized_state)
            self._write_all(data)
        return dict(normalized_state)

    def clear(self, chat_id: str) -> None:
        normalized_chat_id = self._normalize_chat_id(chat_id)
        with self._lock:
            data = self._read_all()
            if data["bindings"].pop(normalized_chat_id, None) is None:
                return
            self._write_all(data)

    def clear_all(self) -> None:
        with self._lock:
            self._delete_file()

    def _state_path(self) -> pathlib.Path:
        return self._data_dir / "bindings.json"

    @staticmethod
    def _normalize_chat_id(chat_id: str) -> str:
        normalized = str(chat_id or "").strip()
        if not normalized:
            raise ValueError("chat_id must not be empty")
        return normalized

    @staticmethod
    def _default_data() -> dict[str, Any]:
        return {
            "schema_version": BINDING_STORE_SCHEMA_VERSION,
            "bindings": {},
        }

    def _read_all(self) -> dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            return self._default_data()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid bindings.json: {exc}") from exc
        return self._validate_store_data(raw)

    def _write_all(self, data: dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))

    def _delete_file(self) -> None:
        path = self._state_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _validate_store_data(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("invalid bindings.json: root must be an object")
        schema_version = raw.get("schema_version")
        if schema_version not in SUPPORTED_BINDING_STORE_SCHEMA_VERSIONS:
            raise ValueError(
                "invalid bindings.json: "
                f"schema_version must be one of {sorted(SUPPORTED_BINDING_STORE_SCHEMA_VERSIONS)}"
            )

        raw_bindings = raw.get("bindings", {})
        if not isinstance(raw_bindings, dict):
            raise ValueError("invalid bindings.json: bindings must be an object")

        bindings: dict[str, StoredBinding] = {}
        for chat_id, raw_state in raw_bindings.items():
            normalized_chat_id = str(chat_id or "").strip()
            if not normalized_chat_id:
                raise ValueError("invalid bindings.json: chat_id cannot be empty")
            bindings[normalized_chat_id] = self._validate_stored_binding(raw_state)

        return {
            "schema_version": BINDING_STORE_SCHEMA_VERSION,
            "bindings": bindings,
        }

    @staticmethod
    def _validate_stored_binding(raw_state: Any) -> StoredBinding:
        if not isinstance(raw_state, dict):
            raise ValueError("invalid bindings.json: binding state must be an object")

        session_id = raw_state.get("session_id", "")
        if not isinstance(session_id, str):
            raise ValueError("invalid bindings.json: session_id must be a string")
        session_id = session_id.strip()
        if not session_id:
            raise ValueError("invalid bindings.json: session_id cannot be empty")

        attached = raw_state.get("attached", DEFAULT_ATTACHED)
        if not isinstance(attached, bool):
            raise ValueError("invalid bindings.json: attached must be a boolean")

        permission_mode = raw_state.get("permission_mode", "")
        if not isinstance(permission_mode, str):
            raise ValueError("invalid bindings.json: permission_mode must be a string")
        permission_mode = permission_mode.strip() or DEFAULT_PERMISSION_MODE
        if permission_mode not in VALID_PERMISSION_MODES:
            raise ValueError(
                "invalid bindings.json: "
                f"permission_mode must be one of {sorted(VALID_PERMISSION_MODES)}"
            )

        plan_mode = raw_state.get("plan_mode", DEFAULT_PLAN_MODE)
        if not isinstance(plan_mode, bool):
            raise ValueError("invalid bindings.json: plan_mode must be a boolean")

        effort = raw_state.get("effort", DEFAULT_EFFORT)
        if not isinstance(effort, str):
            raise ValueError("invalid bindings.json: effort must be a string")
        effort = effort.strip()
        if effort and effort not in VALID_EFFORTS:
            raise ValueError(
                "invalid bindings.json: "
                f"effort must be one of {sorted(VALID_EFFORTS)}"
            )


        return {
            "session_id": session_id,
            "attached": attached,
            "permission_mode": permission_mode,
            "plan_mode": plan_mode,
            "effort": effort,
        }
