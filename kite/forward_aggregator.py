"""Merge-forward aggregation for inbound Feishu merge_forward messages.

Ported from FOCUS ``bot/forward_aggregator.py``, cut to KITE's group contract
(docs/contracts/group-chat.md §3.7): group forwards dispatch per mode at
ingress — mention_only groups drop them, assistant-mode groups append the
flattened transcript to the group log via ``render_transcript()`` (never a
trigger), and all-mode groups buffer here exactly like p2p. The shared core:

- Feishu delivers one forwarded bundle as N separate merge_forward messages;
  ``buffer()`` keeps them per (sender, chat) and (re)arms a short aggregation
  window, so the whole bundle dispatches as one callback;
- on flush the buffered merge_forward trees are expanded recursively (item
  and depth caps, per-item error isolation so one bad child never kills the
  batch) into a single ``<forwarded_messages>`` transcript with resolved
  sender display names and timestamps;
- timer discipline: re-buffering cancels the pending timer, and ``close()``
  (kited shutdown) cancels everything and fails closed.

Threading: ``buffer()`` runs on the RuntimeLoop; the flush runs on the
timer's own thread, so ``on_batch`` must re-enter the RuntimeLoop (the
AppHandler wiring does). The pending map has its own lock.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

DEFAULT_FORWARD_WINDOW_SECONDS = 2.0
DEFAULT_MERGE_FORWARD_MAX_ITEMS = 50
DEFAULT_MERGE_FORWARD_MAX_DEPTH = 10

_FORWARDED_OPEN = "<forwarded_messages>"
_FORWARDED_CLOSE = "</forwarded_messages>"

# Non-textual child types rendered as a bare label (FOCUS parity).
_TYPE_LABELS = {
    "image": "图片",
    "audio": "语音",
    "video": "视频",
    "sticker": "表情",
    "file": "文件",
    "media": "媒体",
}


class _Timer(Protocol):
    def start(self) -> None: ...

    def cancel(self) -> None: ...


def _default_timer_factory(
    timeout_seconds: float,
    callback: Callable[..., None],
    args: list[str],
) -> _Timer:
    return threading.Timer(timeout_seconds, callback, args=args)


@dataclass(frozen=True, slots=True)
class MergedForwardBatch:
    """One flushed aggregation window.

    ``message_id`` is the latest buffered merge_forward message and serves as
    the reply anchor; ``text`` is the full ``<forwarded_messages>`` transcript
    covering every bundle buffered in the window. ``chat_type`` carries the
    ingress chat kind through the timer hop so the handler can dispatch the
    flush (p2p prompt path vs all-mode group path, group-chat §3.7).
    """

    chat_id: str
    sender_open_id: str
    message_id: str
    text: str
    chat_type: str


@dataclass(slots=True)
class _BufferedForward:
    message_id: str
    items: list[Any]


@dataclass(slots=True)
class _PendingForward:
    bundles: list[_BufferedForward] = field(default_factory=list)
    timer: Optional[_Timer] = field(default=None, repr=False)
    chat_type: str = "p2p"


class ForwardAggregator:
    """Per-(sender, chat) aggregation window for merge_forward bundles.

    - ``on_batch``: called once per flushed window with the merged
      MergedForwardBatch; runs on the timer thread, never raises (flush
      failures are logged and dropped).
    - ``name_of``: ``IdentityNames.name_of``-compatible display-name resolver
      (``name_of(open_id, sender_type=...) -> str``); must not raise.
    - ``max_items`` caps the flattened child list per bundle, ``max_depth``
      caps nested merge_forward recursion.
    """

    def __init__(
        self,
        on_batch: Callable[[MergedForwardBatch], None],
        name_of: Callable[..., str],
        *,
        window_seconds: float = DEFAULT_FORWARD_WINDOW_SECONDS,
        max_items: int = DEFAULT_MERGE_FORWARD_MAX_ITEMS,
        max_depth: int = DEFAULT_MERGE_FORWARD_MAX_DEPTH,
        timer_factory: Optional[
            Callable[[float, Callable[..., None], list[str]], _Timer]
        ] = None,
    ) -> None:
        self._on_batch = on_batch
        self._name_of = name_of
        self._window_seconds = max(float(window_seconds), 0.0)
        self._max_items = max(int(max_items), 1)
        self._max_depth = max(int(max_depth), 1)
        self._timer_factory = timer_factory or _default_timer_factory
        self._pending: dict[tuple[str, str], _PendingForward] = {}
        self._lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Buffering (RuntimeLoop thread)
    # ------------------------------------------------------------------

    def buffer(
        self,
        *,
        sender_open_id: str,
        chat_id: str,
        message_id: str,
        items: list[Any],
        chat_type: str = "p2p",
    ) -> None:
        """Add one merge_forward bundle to the window and (re)arm the timer."""
        key = (sender_open_id, chat_id)
        with self._lock:
            if self._closed:
                logger.info(
                    "aggregator closed, merge_forward dropped: sender=%s chat=%s message_id=%s",
                    sender_open_id,
                    chat_id,
                    message_id,
                )
                return
            timer = self._timer_factory(self._window_seconds, self._flush, [sender_open_id, chat_id])
            pending = self._pending.get(key)
            if pending is None:
                pending = _PendingForward()
                self._pending[key] = pending
            elif pending.timer is not None:
                # Re-buffer: the window restarts, the old timer must not fire.
                pending.timer.cancel()
            pending.bundles.append(
                _BufferedForward(message_id=message_id, items=list(items))
            )
            pending.chat_type = chat_type
            pending.timer = timer
            bundle_count = len(pending.bundles)
        timer.start()
        logger.info(
            "merge_forward buffered: sender=%s chat=%s message_id=%s bundles=%d",
            sender_open_id,
            chat_id,
            message_id,
            bundle_count,
        )

    # ------------------------------------------------------------------
    # Flush (timer thread)
    # ------------------------------------------------------------------

    def _flush(self, sender_open_id: str, chat_id: str) -> None:
        try:
            key = (sender_open_id, chat_id)
            with self._lock:
                pending = self._pending.pop(key, None)
            if pending is None:
                return
            parts: list[str] = []
            for bundle in pending.bundles:
                try:
                    rendered = self._render_bundle(bundle.message_id, bundle.items)
                except Exception:  # noqa: BLE001 - per-bundle isolation
                    logger.warning(
                        "merge_forward bundle expansion failed: message_id=%s",
                        bundle.message_id,
                        exc_info=True,
                    )
                    continue
                if rendered:
                    parts.append(rendered)
            if not parts:
                logger.info(
                    "merge_forward expansion produced no renderable content: sender=%s chat=%s",
                    sender_open_id,
                    chat_id,
                )
                return
            text = f"{_FORWARDED_OPEN}\n" + "\n".join(parts) + f"\n{_FORWARDED_CLOSE}"
            self._on_batch(
                MergedForwardBatch(
                    chat_id=chat_id,
                    sender_open_id=sender_open_id,
                    message_id=pending.bundles[-1].message_id,
                    text=text,
                    chat_type=pending.chat_type,
                )
            )
        except Exception:  # noqa: BLE001 - a timer thread must never die noisy
            logger.error(
                "merge_forward flush failed: sender=%s chat=%s",
                sender_open_id,
                chat_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Cancel every pending timer and fail closed (kited shutdown)."""
        with self._lock:
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for entry in pending:
            if entry.timer is not None:
                entry.timer.cancel()

    # ------------------------------------------------------------------
    # Expansion / rendering
    # ------------------------------------------------------------------

    def render_transcript(self, root_message_id: str, items: list[Any]) -> str:
        """Render one fetched bundle as the full ``<forwarded_messages>``
        transcript; "" when nothing in the bundle is renderable.

        The assistant-mode group path (group-chat §3.7) logs the flattened
        content directly instead of buffering, and shares the same recursive
        expansion + tag shape the flush produces.
        """
        rendered = self._render_bundle(root_message_id, items)
        if not rendered:
            return ""
        return f"{_FORWARDED_OPEN}\n{rendered}\n{_FORWARDED_CLOSE}"

    def _render_bundle(self, root_message_id: str, items: list[Any]) -> str:
        """Render one bundle's flattened child list as an indented transcript.

        The Feishu GET-message response carries the merge_forward root plus
        every descendant in one flat list; ``upper_message_id`` links children
        to their parent (a nested merge_forward or the root).
        """
        children_map: dict[str, list[Any]] = {}
        for item in list(items or [])[: self._max_items]:
            sub_id = getattr(item, "message_id", None)
            if not sub_id or sub_id == root_message_id:
                continue
            parent_id = getattr(item, "upper_message_id", None) or root_message_id
            children_map.setdefault(parent_id, []).append(item)
        return self._format_tree(root_message_id, children_map, depth=0)

    def _format_tree(
        self,
        parent_id: str,
        children_map: dict[str, list[Any]],
        depth: int,
    ) -> str:
        indent = "    " * depth
        if depth >= self._max_depth:
            return f"{indent}[嵌套转发层数过深，已截断]"
        parts: list[str] = []
        for item in children_map.get(parent_id, []):
            try:
                rendered = self._format_item(item, children_map, depth)
            except Exception:  # noqa: BLE001 - per-item isolation
                logger.warning(
                    "failed to render forwarded item: message_id=%s",
                    getattr(item, "message_id", "?"),
                    exc_info=True,
                )
                continue
            if rendered:
                parts.append(rendered)
        return "\n".join(parts)

    def _format_item(
        self,
        item: Any,
        children_map: dict[str, list[Any]],
        depth: int,
    ) -> str:
        indent = "    " * depth
        sub_id = str(getattr(item, "message_id", "") or "").strip()
        sub_type = str(getattr(item, "msg_type", "") or "").strip()

        sender = getattr(item, "sender", None)
        sender_id = str(getattr(sender, "id", "") or "").strip() if sender else ""
        sender_type = str(getattr(sender, "sender_type", "") or "").strip() if sender else ""
        sender_name = self._name_of(sender_id, sender_type=sender_type or "user")
        if sender_type == "app":
            sender_name = f"{sender_name}[机器人]"
        ts_str = self._format_ts(getattr(item, "create_time", None))
        header = f"{indent}[{ts_str}] {sender_name}:"

        if sub_type == "merge_forward":
            block = f"{header} [forwarded messages]"
            nested = self._format_tree(sub_id, children_map, depth + 1)
            return f"{block}\n{nested}" if nested else block

        text = self._item_text(sub_type, item)
        if text:
            content_indent = indent + "    "
            indented_lines = "\n".join(
                f"{content_indent}{line}" for line in text.splitlines()
            )
            return f"{header}\n{indented_lines}"

        if sub_type in _TYPE_LABELS:
            return f"{header} [{_TYPE_LABELS[sub_type]}]"
        return f"{header} [{sub_type or 'unknown'} 消息]"

    @staticmethod
    def _item_text(sub_type: str, item: Any) -> str:
        """The child's plain text; "" when unparseable or non-textual."""
        body = getattr(item, "body", None)
        raw_content = getattr(body, "content", "") if body is not None else ""
        try:
            content = json.loads(raw_content)
        except (json.JSONDecodeError, TypeError):
            return ""
        if not isinstance(content, dict):
            return ""
        return _extract_item_text(sub_type, content)

    @staticmethod
    def _format_ts(ts_ms: Any) -> str:
        if not ts_ms:
            return "未知时间"
        try:
            from datetime import datetime, timedelta, timezone

            # FOCUS parity: transcript timestamps render in UTC+8.
            dt = datetime.fromtimestamp(
                int(ts_ms) / 1000,
                tz=timezone(timedelta(hours=8)),
            )
            return dt.strftime("%m-%d %H:%M:%S")
        except (ValueError, OSError):
            return "未知时间"


def _extract_item_text(msg_type: str, content_dict: dict[str, Any]) -> str:
    """Plain-text extraction for forwarded children.

    Mirrors ``FeishuTransport._extract_text`` (text + post). Richer FOCUS
    renderings (interactive-card projection, share_user/hongbao labels) stay
    out of scope: card projection is the application-layer concern the KITE
    transport deliberately cut, and unknown types degrade to a type label.
    """
    if msg_type == "text":
        return str(content_dict.get("text", "") or "").strip()

    if msg_type == "post":
        paragraphs = content_dict.get("content")
        if isinstance(paragraphs, dict):
            for lang_content in paragraphs.values():
                if isinstance(lang_content, dict):
                    paragraphs = lang_content.get("content", [])
                else:
                    paragraphs = lang_content
                break
        if not isinstance(paragraphs, list):
            return ""
        parts: list[str] = []
        for para in paragraphs:
            if not isinstance(para, list):
                continue
            line_parts: list[str] = []
            for elem in para:
                if isinstance(elem, dict) and elem.get("tag") == "text":
                    t = str(elem.get("text", "") or "")
                    if t:
                        line_parts.append(t)
            line = "".join(line_parts)
            parts.append(line if line.strip() else "")
        while parts and not parts[0]:
            parts.pop(0)
        while parts and not parts[-1]:
            parts.pop()
        return "\n".join(parts)

    return ""
