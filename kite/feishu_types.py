"""Feishu transport value types.

Direct port of FOCUS ``bot/feishu_types.py``, cut down to the types the
transport layer actually uses. Cut points (application-layer state, not
transport):

- ``MessageContextPayload`` / group-chat store TypedDicts: FOCUS kept them
  for group-mode state and message context caches; KITE's transport only
  caches the parent-message thread id it needs for reply-in-thread.
- ``StoredChatBinding`` / ``ChatBindingsFileData``: binding state belongs to
  the application layer and already exists in ``kite/stores/binding_store.py``
  with kimi-code vocabulary (session, not codex-era thread).
- ``BotIdentitySnapshot``: only consumed by admin diagnostics; reintroduce
  with the application layer that needs it.
"""

from __future__ import annotations

from typing import TypedDict


class MentionPayload(TypedDict):
    key: str
    name: str
    open_id: str
