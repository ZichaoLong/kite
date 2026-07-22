"""In-memory prompt ownership map (state axis 4, kite-design.md §4).

Which chat initiated each active/queued prompt. This determines where
approval/question cards route to (mvp-scope §3: they go only to the prompt
initiator's chat) and who may /abort it (initiator or admins).

The map is deliberately in-memory only. After a kited restart it is rebuilt
on a best-effort basis from ``GET .../prompts`` (mvp-scope §4.6); entries
carry a certainty marker so the outbound path (E3) can distinguish:

- ``certain``: this process submitted the prompt itself and observed the
  REST response — the ownership fact is exact;
- ``best_effort``: rebuilt after a restart by attributing a session's
  active/queued prompts to the chat bound to that session — approval cards
  whose ownership cannot be established with certainty are explicitly
  expired instead of routed on a guess (fail-closed).

Thread-safety: a single lock around the dict. The RuntimeLoop serializes
normal mutations; the lock only has to survive diagnostic reads and the
startup rebuild racing the first inbound messages.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterable

CERTAINTY_CERTAIN = "certain"
CERTAINTY_BEST_EFFORT = "best_effort"
VALID_CERTAINTIES = frozenset({CERTAINTY_CERTAIN, CERTAINTY_BEST_EFFORT})


@dataclass(frozen=True, slots=True)
class PromptOwnershipEntry:
    """One ownership fact: prompt -> initiating chat, with certainty."""

    prompt_id: str
    chat_id: str
    certainty: str


class PromptOwnership:
    """The prompt_id -> chat_id ownership map."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, PromptOwnershipEntry] = {}

    def record(self, prompt_id: str, chat_id: str) -> PromptOwnershipEntry:
        """Record certain ownership (we submitted this prompt ourselves)."""
        return self._store(prompt_id, chat_id, CERTAINTY_CERTAIN)

    def record_best_effort(self, prompt_id: str, chat_id: str) -> PromptOwnershipEntry:
        """Record a single best-effort ownership (restart rebuild)."""
        return self._store(prompt_id, chat_id, CERTAINTY_BEST_EFFORT)

    def owner_of(self, prompt_id: str) -> str | None:
        """The initiating chat_id for a prompt, or None when unknown."""
        with self._lock:
            entry = self._entries.get(str(prompt_id or "").strip())
            return entry.chat_id if entry is not None else None

    def entry_of(self, prompt_id: str) -> PromptOwnershipEntry | None:
        with self._lock:
            return self._entries.get(str(prompt_id or "").strip())

    def certainty_of(self, prompt_id: str) -> str | None:
        with self._lock:
            entry = self._entries.get(str(prompt_id or "").strip())
            return entry.certainty if entry is not None else None

    def forget(self, prompt_id: str) -> None:
        """Drop a prompt from the map (terminal cleanup, no-op when absent)."""
        with self._lock:
            self._entries.pop(str(prompt_id or "").strip(), None)

    def rebuild(self, entries: Iterable[PromptOwnershipEntry]) -> None:
        """Replace the whole map from a restart rebuild.

        Wholesale replace, not a merge: the rebuild input is the complete
        set of prompts kap still knows about, so anything it omits is gone
        and must not keep a stale owner.
        """
        rebuilt: dict[str, PromptOwnershipEntry] = {}
        for entry in entries:
            normalized = self._validate(entry.prompt_id, entry.chat_id, entry.certainty)
            rebuilt[normalized.prompt_id] = normalized
        with self._lock:
            self._entries = rebuilt

    def snapshot(self) -> tuple[PromptOwnershipEntry, ...]:
        with self._lock:
            return tuple(self._entries.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _store(self, prompt_id: str, chat_id: str, certainty: str) -> PromptOwnershipEntry:
        entry = self._validate(prompt_id, chat_id, certainty)
        with self._lock:
            self._entries[entry.prompt_id] = entry
        return entry

    @staticmethod
    def _validate(prompt_id: str, chat_id: str, certainty: str) -> PromptOwnershipEntry:
        normalized_prompt_id = str(prompt_id or "").strip()
        normalized_chat_id = str(chat_id or "").strip()
        normalized_certainty = str(certainty or "").strip()
        if not normalized_prompt_id:
            raise ValueError("prompt_id must not be empty")
        if not normalized_chat_id:
            raise ValueError("chat_id must not be empty")
        if normalized_certainty not in VALID_CERTAINTIES:
            raise ValueError(f"certainty must be one of {sorted(VALID_CERTAINTIES)}")
        return PromptOwnershipEntry(
            prompt_id=normalized_prompt_id,
            chat_id=normalized_chat_id,
            certainty=normalized_certainty,
        )
