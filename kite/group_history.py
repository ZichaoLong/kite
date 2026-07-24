"""Assistant-mode group context composition (group-chat contract §3.3).

KITE's cut of FOCUS ``group_history_recovery.py``: on an assistant-mode
trigger, the context is the local per-chat log since the trigger boundary,
merged with a Feishu REST history backfill. The two sources are deduped
exactly via the boundary triple ``{seq, created_at, message_ids}`` (a
millisecond timestamp alone is not a cursor) plus message ids; the bot's own
app messages are filtered out of the backfill. The composed prompt is the
``<group_chat_scope>/<group_chat_context>/<group_chat_current_turn>``
envelope, which tells the model to answer the current message, not recite
history.

Fail-closed (contract §4.5): the REST fetch raising propagates to the
caller — the prompt is then BLOCKED with an explicit notice; KITE never
answers silently without the context. The backfill can be switched off
(fetch limit or lookback = 0), in which case the context is the local log
only and no fetch happens.

Scope cut vs FOCUS: no thread scopes (KITE's boundary is one triple per
chat) — the log records and the backfill returns the whole chat flow.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from collections import deque
from typing import Any, Callable

from kite.stores.group_log_store import GroupLogStore

logger = logging.getLogger(__name__)

DEFAULT_GROUP_HISTORY_FETCH_LIMIT = 50
DEFAULT_GROUP_HISTORY_FETCH_LOOKBACK_SECONDS = 24 * 3600
DEFAULT_GROUP_HISTORY_BOUNDARY_SLACK_SECONDS = 5


class GroupHistoryRecovery:
    """Merges the local group log with the Feishu REST history backfill.

    Ports are callables so tests can substitute fakes:

    - ``list_messages``: ``FeishuTransport.list_messages`` — one page of the
      chat history API; raises on failure (the fail-closed signal);
    - ``render_text``: ``(msg_type, content_dict, mentions) -> str`` —
      transport-level text extraction + mention normalization;
    - ``name_of``: ``IdentityNames.name_of`` — display-name resolution with
      the ``open_id[:8]`` / ``机器人:{id[:8]}`` fallback chain;
    - ``app_id``: this bot's app_id (str or zero-arg callable); app senders
      with this id are the bot itself and never enter the context.
    """

    def __init__(
        self,
        *,
        list_messages: Callable[..., Any],
        render_text: Callable[[str, dict[str, Any], list[Any]], str],
        name_of: Callable[..., str],
        log_store: GroupLogStore,
        app_id: str | Callable[[], str] = "",
        fetch_limit: int = DEFAULT_GROUP_HISTORY_FETCH_LIMIT,
        lookback_seconds: int = DEFAULT_GROUP_HISTORY_FETCH_LOOKBACK_SECONDS,
        boundary_slack_seconds: int = DEFAULT_GROUP_HISTORY_BOUNDARY_SLACK_SECONDS,
    ) -> None:
        self._list_messages = list_messages
        self._render_text = render_text
        self._name_of = name_of
        self._log_store = log_store
        if callable(app_id):
            self._app_id_getter = app_id
        else:
            self._app_id_getter = lambda: str(app_id or "").strip()
        self._fetch_limit = max(int(fetch_limit or 0), 0)
        self._lookback_seconds = max(int(lookback_seconds or 0), 0)
        self._boundary_slack_seconds = max(int(boundary_slack_seconds or 0), 0)

    def history_recovery_enabled(self) -> bool:
        return self._fetch_limit > 0 and self._lookback_seconds > 0

    # ------------------------------------------------------------------
    # REST backfill
    # ------------------------------------------------------------------

    def history_entry_from_message(self, item: Any) -> dict[str, Any] | None:
        """Normalize one history API message; None when not context-worthy.

        Drops: messages without id/text, and this bot's own app messages
        (self-app filter — the bot's cards/replies are not group context).
        """
        message_id = str(getattr(item, "message_id", "") or "").strip()
        if not message_id:
            return None

        msg_type = str(getattr(item, "msg_type", "") or "text").strip() or "text"
        body = getattr(item, "body", None)
        raw_content = str(getattr(body, "content", "") or "").strip()
        try:
            content_dict = json.loads(raw_content) if raw_content else {}
        except Exception:
            content_dict = {}
        if not isinstance(content_dict, dict):
            content_dict = {}

        mentions = list(getattr(item, "mentions", None) or [])
        text = self._render_text(msg_type, content_dict, mentions)
        if not text:
            return None

        sender = getattr(item, "sender", None)
        sender_type = str(getattr(sender, "sender_type", "") or "user").strip() or "user"
        sender_id = str(getattr(sender, "id", "") or "").strip()
        if self._is_self_app_sender(sender_type=sender_type, sender_id=sender_id):
            return None
        principal_id = sender_id if sender_type in {"user", "app"} else ""
        try:
            created_at = max(int(getattr(item, "create_time", 0) or 0), 0)
        except (TypeError, ValueError):
            created_at = 0
        return {
            "message_id": message_id,
            "created_at": created_at,
            "sender_open_id": sender_id if sender_type == "user" else "",
            "sender_type": sender_type,
            "sender_name": self._name_of(principal_id, sender_type=sender_type),
            "msg_type": msg_type,
            "text": text,
        }

    def _is_self_app_sender(self, *, sender_type: str, sender_id: str) -> bool:
        normalized_app_id = str(self._app_id_getter() or "").strip()
        return (
            bool(normalized_app_id)
            and sender_type == "app"
            and sender_id == normalized_app_id
        )

    def fetch_history_entries(
        self,
        *,
        chat_id: str,
        current_message_id: str,
        current_create_time: int | str | None,
        existing_message_ids: set[str],
        min_created_at: int,
        boundary_message_ids: set[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Page the chat history API over the lookback window; dedup exactly.

        Dedup discipline (boundary triple): entries older than the boundary
        millisecond are skipped; entries AT the boundary millisecond are
        skipped only when their id is in the boundary's id set (messages
        sharing the millisecond but missing from the boundary are kept).
        The fetch window starts ``boundary_slack_seconds`` before the
        boundary so equal-millisecond messages are actually in scope.
        Raises whatever ``list_messages`` raises (fail-closed, §4.5).
        """
        try:
            end_time = (
                int(int(current_create_time or 0) / 1000)
                if current_create_time
                else int(time.time())
            )
        except (TypeError, ValueError):
            end_time = int(time.time())
        if end_time <= 0:
            end_time = int(time.time())
        start_time = max(0, end_time - self._lookback_seconds)
        if min_created_at > 0:
            start_time = max(
                start_time,
                max(0, int(min_created_at / 1000) - self._boundary_slack_seconds),
            )
        page_token = ""
        entries: deque[dict[str, Any]] = deque(maxlen=limit)
        seen_message_ids = set(existing_message_ids)
        seen_message_ids.add(str(current_message_id or "").strip())
        normalized_boundary_ids = {
            str(item).strip() for item in boundary_message_ids if str(item).strip()
        }

        while True:
            page = self._list_messages(
                chat_id,
                start_time=str(start_time),
                end_time=str(end_time),
                sort_type="ByCreateTimeAsc",
                page_size=50,
                page_token=page_token,
            )
            for item in list(page.items or []):
                entry = self.history_entry_from_message(item)
                if not entry:
                    continue
                entry_created_at = max(int(entry.get("created_at", 0) or 0), 0)
                message_id = str(entry.get("message_id", "") or "").strip()
                if min_created_at > 0 and entry_created_at < min_created_at:
                    continue
                if (
                    min_created_at > 0
                    and entry_created_at == min_created_at
                    and message_id in normalized_boundary_ids
                ):
                    continue
                if not message_id or message_id in seen_message_ids:
                    continue
                entries.append(entry)
                seen_message_ids.add(message_id)

            if not page.has_more:
                break
            page_token = str(page.page_token or "").strip()
            if not page_token:
                break

        return list(entries)

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    @staticmethod
    def context_sort_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
        """Local entries (with seq) sort before backfill ones at equal ms."""
        created_at = max(int(item.get("created_at", 0) or 0), 0)
        seq = item.get("seq")
        if isinstance(seq, int):
            return (created_at, 0, seq, str(item.get("message_id", "") or ""))
        return (created_at, 1, 0, str(item.get("message_id", "") or ""))

    def collect_context_entries(
        self,
        *,
        chat_id: str,
        current_message_id: str,
        current_create_time: int | str | None,
        current_seq: int,
    ) -> list[dict[str, Any]]:
        """Local log since the boundary (excl. the trigger) + REST backfill.

        ``current_seq`` is the trigger message's log seq (0 when the trigger
        itself was not logged); the trigger is excluded from the context —
        it is rendered as the current turn instead.
        """
        boundary = self._log_store.boundary(chat_id)
        local_entries = [
            entry
            for entry in self._log_store.entries_since(chat_id, boundary["seq"])
            if current_seq <= 0 or int(entry.get("seq", 0)) < current_seq
        ]
        if not self.history_recovery_enabled():
            return local_entries

        existing_message_ids = {
            str(entry.get("message_id", "") or "").strip()
            for entry in local_entries
            if str(entry.get("message_id", "") or "").strip()
        }
        history_entries = self.fetch_history_entries(
            chat_id=chat_id,
            current_message_id=current_message_id,
            current_create_time=current_create_time,
            existing_message_ids=existing_message_ids,
            min_created_at=int(boundary["created_at"]),
            boundary_message_ids=set(boundary["message_ids"]),
            limit=self._fetch_limit,
        )
        if not history_entries:
            return local_entries
        return sorted([*local_entries, *history_entries], key=self.context_sort_key)

    @staticmethod
    def collect_boundary_message_ids(
        *,
        current_message_id: str,
        current_created_at: int | str | None,
        context_entries: list[dict[str, Any]],
    ) -> list[str]:
        """Every message id at the trigger millisecond (the boundary's id set)."""
        try:
            normalized_created_at = max(int(current_created_at or 0), 0)
        except (TypeError, ValueError):
            normalized_created_at = 0
        if normalized_created_at <= 0:
            return []
        message_ids = {str(current_message_id or "").strip()}
        for item in context_entries:
            if max(int(item.get("created_at", 0) or 0), 0) != normalized_created_at:
                continue
            message_id = str(item.get("message_id", "") or "").strip()
            if message_id:
                message_ids.add(message_id)
        message_ids.discard("")
        return sorted(message_ids)

    # ------------------------------------------------------------------
    # Envelope
    # ------------------------------------------------------------------

    @staticmethod
    def format_ts(ts_ms: int | str | None) -> str:
        if not ts_ms:
            return "未知时间"
        try:
            from datetime import datetime, timedelta, timezone

            dt = datetime.fromtimestamp(
                int(ts_ms) / 1000,
                tz=timezone(timedelta(hours=8)),
            )
            return dt.strftime("%m-%d %H:%M:%S")
        except (ValueError, OSError):
            return "未知时间"

    @staticmethod
    def normalize_sender_name(sender_name: str) -> str:
        normalized = " ".join(str(sender_name or "").split())
        return normalized or "unknown"

    def format_context_entries(self, entries: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for item in entries:
            seq = item.get("seq")
            ts = self.format_ts(item.get("created_at"))
            sender_name = str(item.get("sender_name", "") or "unknown").strip()
            sender_type = str(item.get("sender_type", "") or "user").strip()
            msg_type = str(item.get("msg_type", "") or "text").strip()
            text = str(item.get("text", "") or "").strip()
            if sender_type == "app" and not sender_name.startswith("机器人:"):
                sender_name = f"{sender_name}[机器人]"
            if isinstance(seq, int) and seq > 0:
                header = f"[#{seq} {ts}] {sender_name}"
            else:
                header = f"[{ts}] {sender_name}"
            if msg_type and msg_type != "text":
                header += f" ({msg_type})"
            if text:
                parts.append(f"{header}\n{text}")
            else:
                parts.append(header)
        return "\n\n".join(parts).strip()

    def build_current_turn_text(self, current_text: str, *, sender_name: str) -> str:
        message_text = str(current_text or "").strip()
        if not message_text:
            message_text = "（发送者没有提供额外文本，请基于上下文回复最近这段讨论。）"
        return (
            "<group_chat_current_turn>\n"
            "以下是当前需要你直接响应的群消息。优先回复这条消息，而不是复述整段历史。\n"
            f"sender_name: {self.normalize_sender_name(sender_name)}\n"
            "message:\n"
            f"{message_text}\n"
            "</group_chat_current_turn>"
        )

    def build_envelope(
        self,
        current_text: str,
        *,
        sender_name: str,
        context_entries: list[dict[str, Any]],
        log_path: pathlib.Path,
    ) -> str:
        """The assistant-mode prompt: scope + context + current turn (§3.3)."""
        context_block = self.format_context_entries(context_entries).strip() or (
            "（上次有效触发之后暂无可用群聊消息）"
        )
        return (
            "<group_chat_scope>\n"
            "当前消息来自一个飞书群聊。你是本群所有成员共享的同一个助手。\n"
            "下方的群聊上下文仅供你理解讨论背景，不要逐条复述；如需引用其中的结论，应明确说明那是群内此前讨论的内容。\n"
            "</group_chat_scope>\n\n"
            "<group_chat_context>\n"
            "以下是本群自上次有效触发到本次触发之前的消息。\n"
            f"群聊日志文件：`{log_path}`\n\n"
            f"{context_block}\n"
            "</group_chat_context>\n\n"
            + self.build_current_turn_text(current_text, sender_name=sender_name)
        )
