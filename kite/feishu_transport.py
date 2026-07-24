"""Feishu transport layer.

WS long-connection lifecycle via lark-oapi, inbound message dedup, receive ->
normalized inbound types, outbound send text/card, card patch, attachment
download. Per docs/architecture/kite-design.md section 3 this module depends
only on the Feishu SDK; it knows nothing about kap-server, sessions, command
routing, or the business meaning of cards.

Ported from FOCUS ``bot/feishu_bot.py`` (class ``FeishuBot``), cut down to the
transport core. Where FOCUS called directly into codex application logic, the
transport now dispatches to a ``TransportHandler`` instead. Deliberate cuts
(the application layer must reimplement these if/when it needs them):

- Group-mode state machine (all / mention_only / assistant), group activation
  and per-group ACL: FOCUS kept them in the base class via GroupChatStore.
- Merge-forward buffering and reassembly: the transport dispatches a
  normalized merge_forward event and fetches bundle children on demand; the
  aggregation window and tree expansion live in the application layer
  (``kite/forward_aggregator.py``, wired by AppHandler).
- Group history recovery / assistant context building (GroupHistoryRecovery).
- Admin gating and non-admin p2p bootstrap filtering: every normalized
  message is dispatched; access policy is the handler's job.
- Card text projection / interactive message re-read (bot.card_text_projection):
  ``interactive`` messages extract to empty text at this layer.
- Sender display-name resolution via the contacts API, chat-type/chat-name
  caches, bot identity discovery (``/bot-status`` diagnostics).
- The "only text messages supported" p2p auto-reply: UX policy, not transport.

The only inbound-side state kept here is what the transport itself needs:
message dedup and a small parent-message thread-id cache for reply-in-thread
routing of outbound replies.
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageRequest,
    GetMessageRequest,
    GetMessageResourceRequest,
    ListMessageRequest,
    P2ImChatDisbandedV1,
    P2ImChatMemberBotDeletedV1,
    P2ImMessageRecalledV1,
    P2ImMessageReceiveV1,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.api.application.v6.model.p2_application_bot_menu_v6 import (
    P2ApplicationBotMenuV6,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
    CallBackCard,
    CallBackToast,
)

from kite.feishu_types import MentionPayload
from kite.feishu_ws_proxy import (
    DEFAULT_FEISHU_WS_PROXY,
    configure_feishu_ws_proxy,
    normalize_feishu_ws_proxy_mode,
)
from kite.message_patch_result import MessagePatchResult

logger = logging.getLogger(__name__)

DEFAULT_FEISHU_REQUEST_TIMEOUT_SECONDS = 5.0

# Message dedup cache capacity and TTL.
_DEDUP_MAX_SIZE = 500
_DEDUP_TTL = 300  # 5 minutes

# Parent-message thread-id cache, used to route replies into Feishu threads.
_MESSAGE_THREAD_CACHE_MAX_SIZE = 1000
_MESSAGE_THREAD_CACHE_TTL = 600

_PATCH_MESSAGE_RETRY_SECONDS = 2.0

_DOWNLOADABLE_ATTACHMENT_MESSAGE_TYPES = {"image", "file", "audio", "media"}
_UNSUPPORTED_ATTACHMENT_MESSAGE_TYPES = {"folder", "sticker"}
_ATTACHMENT_MESSAGE_TYPES = _DOWNLOADABLE_ATTACHMENT_MESSAGE_TYPES | _UNSUPPORTED_ATTACHMENT_MESSAGE_TYPES


def _evict_expired_fifo_entries(
    entries: OrderedDict[str, Any],
    *,
    now: float,
    ttl_seconds: float,
    created_at: Callable[[Any], float],
) -> None:
    while entries:
        oldest_key, oldest_value = next(iter(entries.items()))
        if now - created_at(oldest_value) > ttl_seconds:
            entries.pop(oldest_key, None)
        else:
            break


def _store_fifo_ttl_entry(
    entries: OrderedDict[str, Any],
    *,
    key: str,
    value: Any,
    ttl_seconds: float,
    max_size: int,
    created_at: Callable[[Any], float],
) -> None:
    now = time.time()
    _evict_expired_fifo_entries(
        entries,
        now=now,
        ttl_seconds=ttl_seconds,
        created_at=created_at,
    )
    entries.pop(key, None)
    entries[key] = value
    while len(entries) > max_size:
        entries.popitem(last=False)


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """A normalized inbound Feishu message handed to the application layer."""

    message_id: str
    chat_id: str
    chat_type: str
    msg_type: str
    text: str
    sender_open_id: str
    sender_user_id: str
    sender_type: str
    bot_mentioned: bool
    mentions: list[MentionPayload]
    thread_id: str
    root_id: str
    parent_id: str
    create_time: int


@dataclass(frozen=True, slots=True)
class InboundAttachment:
    """A normalized inbound attachment message (image/file/audio/media/...).

    ``resource_key`` is the Feishu image_key/file_key; download the bytes via
    ``FeishuTransport.download_message_resource``.
    """

    message_id: str
    chat_id: str
    chat_type: str
    attachment_type: str
    resource_key: str
    file_name: str
    sender_open_id: str
    sender_user_id: str
    sender_type: str
    thread_id: str
    root_id: str
    parent_id: str
    create_time: int


@dataclass(frozen=True, slots=True)
class InboundMergeForward:
    """A normalized inbound merge_forward message (a forwarded bundle).

    The event content is the fixed string "Merged and Forwarded Message"
    (not JSON); the child messages are fetched on demand via
    ``FeishuTransport.fetch_merge_forward_items`` and expanded by the
    application layer (``kite/forward_aggregator.py``).
    """

    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    sender_user_id: str
    sender_type: str
    thread_id: str
    root_id: str
    parent_id: str
    create_time: int


@dataclass(frozen=True, slots=True)
class CardAction:
    """A normalized card button click / form submission.

    ``value`` is the card action value dict; the transport injects
    ``_operator_open_id`` / ``_operator_user_id`` and, for form submissions,
    ``_form_value`` (same convention as FOCUS).
    """

    operator_open_id: str
    operator_user_id: str
    chat_id: str
    message_id: str
    value: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CardActionResponse:
    """Transport-level card action result: update the card and/or show a toast."""

    card: Optional[dict] = None
    toast: Optional[str] = None
    toast_type: str = "info"


@dataclass(frozen=True, slots=True)
class DownloadedMessageResource:
    content: bytes
    file_name: str
    content_type: str


@dataclass(frozen=True, slots=True)
class ListedMessagesPage:
    """One page of the chat message-history list API."""

    items: list[Any]
    has_more: bool = False
    page_token: str = ""


@dataclass
class _MessageThreadContext:
    chat_id: str
    thread_id: str
    created_at: float


def make_card_response(
    card: Optional[dict] = None,
    toast: Optional[str] = None,
    toast_type: str = "info",
) -> P2CardActionTriggerResponse:
    """Build the lark SDK card-action response (update card and/or toast)."""
    resp = P2CardActionTriggerResponse()
    if toast:
        resp.toast = CallBackToast()
        resp.toast.type = toast_type
        resp.toast.content = toast
    if card:
        resp.card = CallBackCard()
        resp.card.type = "raw"
        resp.card.data = card
    return resp


class TransportHandler(ABC):
    """Application-layer callbacks the transport dispatches to.

    Implementations must not block; long work belongs on the RuntimeLoop
    (see docs/architecture/kite-design.md section 3).
    """

    @abstractmethod
    def on_message(self, message: InboundMessage) -> None:
        """Handle a normalized inbound message."""
        ...

    def on_attachment(self, attachment: InboundAttachment) -> None:
        """Handle an inbound attachment message."""

    def on_merge_forward(self, message: InboundMergeForward) -> None:
        """Handle an inbound merge_forward message (default: log and drop)."""
        logger.info(
            "merge_forward dropped by default handler: chat=%s message_id=%s",
            message.chat_id,
            message.message_id,
        )

    def on_card_action(self, action: CardAction) -> CardActionResponse:
        """Handle a card button click / form submission."""
        return CardActionResponse()

    def on_message_recalled(self, chat_id: str, message_id: str) -> None:
        """Handle a Feishu message-recalled event."""

    def on_chat_unavailable(self, chat_id: str, *, reason: str = "") -> None:
        """Handle chat disbanded / bot removed lifecycle events."""

    def on_bot_menu(self, open_id: str, event_key: str) -> None:
        """Handle a bot menu click."""


class FeishuTransport:
    """Feishu websocket transport: connection lifecycle + message I/O.

    Key parts:
    1. Connection: __init__ builds the lark.Client and event dispatcher,
       start() opens the websocket long connection.
    2. Inbound: raw events -> dedup -> normalize -> TransportHandler.
    3. Outbound: send_message / reply / reply_card / patch_message /
       download_message_resource.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        handler: TransportHandler,
        request_timeout_seconds: float = DEFAULT_FEISHU_REQUEST_TIMEOUT_SECONDS,
        *,
        bot_open_id: str = "",
        trigger_open_ids: Iterable[str] = (),
        feishu_ws_proxy: str = DEFAULT_FEISHU_WS_PROXY,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.request_timeout_seconds = float(request_timeout_seconds)
        self._handler = handler
        self._seen_messages: OrderedDict[str, float] = OrderedDict()
        self._dedup_lock = threading.Lock()
        self._message_thread_contexts: OrderedDict[str, _MessageThreadContext] = OrderedDict()
        self._message_thread_contexts_lock = threading.Lock()
        self._feishu_ws_proxy_mode = normalize_feishu_ws_proxy_mode(feishu_ws_proxy)
        self._configured_bot_open_id = str(bot_open_id or "").strip()
        self._configured_trigger_open_ids = {
            str(item).strip() for item in trigger_open_ids if str(item).strip()
        }
        self._bot_open_id_error_logged = False

        self.client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .timeout(self.request_timeout_seconds) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        self._event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_raw_message) \
            .register_p2_im_message_recalled_v1(self._on_raw_message_recalled) \
            .register_p2_im_chat_disbanded_v1(self._on_raw_chat_disbanded) \
            .register_p2_im_chat_member_bot_deleted_v1(self._on_raw_chat_member_bot_deleted) \
            .register_p2_card_action_trigger(self._on_raw_card_action) \
            .register_p2_application_bot_menu_v6(self._on_raw_bot_menu) \
            .build()

    def set_bot_open_id(self, open_id: str) -> None:
        """Install a (newly discovered) bot open_id at runtime."""
        normalized = str(open_id or "").strip()
        if normalized:
            self._configured_bot_open_id = normalized
            self._bot_open_id_error_logged = False

    @property
    def bot_open_id(self) -> str:
        """The bot's own open_id ("" until configured/discovered)."""
        return self._configured_bot_open_id

    def fetch_bot_open_id(self) -> str | None:
        """Discover this bot's open_id via ``GET /open-apis/bot/v3/info/``.

        Group mention triggering needs the bot's own open_id; discovering it
        beats configuring it (FOCUS's `_fetch_bot_open_id`, same contract:
        failure returns None and the caller keeps the fail-closed default).
        """
        try:
            req = lark.BaseRequest.builder() \
                .http_method(lark.HttpMethod.GET) \
                .uri("/open-apis/bot/v3/info/") \
                .token_types({lark.AccessTokenType.TENANT}) \
                .build()
            resp = self.client.request(req)
            if not resp.success():
                logger.warning("bot info fetch failed: code=%s msg=%s", resp.code, resp.msg)
                return None
            data = json.loads(resp.raw.content)
            open_id = (data.get("bot") or {}).get("open_id")
            if isinstance(open_id, str) and open_id.strip():
                logger.info("bot identity discovered: open_id=%s", open_id)
                return open_id.strip()
            return None
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            logger.warning("bot info fetch raised: %s", exc)
            return None

    def fetch_user_name(self, open_id: str) -> str | None:
        """Resolve one user's display name via the contact API (tenant token).

        Used by ``kite.identity_names`` for group-facing notices; failure
        returns None and the caller falls back to a shortened open_id.
        Requires the ``contact:user.base:readonly`` scope.
        """
        normalized = str(open_id or "").strip()
        if not normalized:
            return None
        try:
            req = lark.BaseRequest.builder() \
                .http_method(lark.HttpMethod.GET) \
                .uri(f"/open-apis/contact/v3/users/{normalized}?user_id_type=open_id") \
                .token_types({lark.AccessTokenType.TENANT}) \
                .build()
            resp = self.client.request(req)
            if not resp.success():
                logger.warning("user info fetch failed open_id=%s: code=%s", normalized, resp.code)
                return None
            data = json.loads(resp.raw.content)
            user = data.get("user") or {}
            # FOCUS's field order: canonical name first, nickname as fallback.
            name = user.get("name") or user.get("nickname")
            return name.strip() if isinstance(name, str) and name.strip() else None
        except Exception as exc:  # noqa: BLE001 - best-effort lookup
            logger.warning("user info fetch raised open_id=%s: %s", normalized, exc)
            return None

    # ---- inbound: dedup ----

    def _is_duplicate(self, message_id: str) -> bool:
        """Check for duplicate delivery (Feishu retries), evicting expired entries."""
        with self._dedup_lock:
            now = time.time()
            if message_id in self._seen_messages:
                return True
            while self._seen_messages:
                oldest_id, ts = next(iter(self._seen_messages.items()))
                if now - ts > _DEDUP_TTL:
                    self._seen_messages.pop(oldest_id)
                else:
                    break
            if len(self._seen_messages) >= _DEDUP_MAX_SIZE:
                self._seen_messages.popitem(last=False)
            self._seen_messages[message_id] = now
            return False

    # ---- inbound: parent thread-id cache (for reply-in-thread routing) ----

    def _remember_message_thread(self, message_id: str, *, chat_id: str, thread_id: str) -> None:
        if not message_id:
            return
        with self._message_thread_contexts_lock:
            _store_fifo_ttl_entry(
                self._message_thread_contexts,
                key=message_id,
                value=_MessageThreadContext(chat_id=chat_id, thread_id=thread_id, created_at=time.time()),
                ttl_seconds=_MESSAGE_THREAD_CACHE_TTL,
                max_size=_MESSAGE_THREAD_CACHE_MAX_SIZE,
                created_at=lambda ctx: ctx.created_at,
            )

    def _lookup_message_thread(self, message_id: str) -> str:
        with self._message_thread_contexts_lock:
            ctx = self._message_thread_contexts.get(message_id)
            return ctx.thread_id if ctx else ""

    def _forget_chat_state(self, chat_id: str) -> None:
        normalized_chat_id = str(chat_id or "").strip()
        with self._message_thread_contexts_lock:
            stale = [
                message_id
                for message_id, ctx in self._message_thread_contexts.items()
                if ctx.chat_id == normalized_chat_id
            ]
            for message_id in stale:
                self._message_thread_contexts.pop(message_id, None)

    # ---- inbound: normalization helpers ----

    @staticmethod
    def _extract_text(msg_type: str, content_dict: dict) -> str:
        """Extract plain text from a Feishu message content dict.

        - text: the ``text`` field
        - post rich text: walk the 2-D content array, keep paragraph breaks
        - anything else (sticker/image/interactive/...): empty string; card
          text projection is application-layer business and was cut here
        """
        if msg_type == "text":
            return content_dict.get("text", "").strip()

        if msg_type == "post":
            # Rich text: {"title": "...", "content": [[{"tag": "text", "text": "..."}, ...]]}
            # content may be top-level or nested per language (e.g. content.zh_cn)
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

    @staticmethod
    def _attachment_message_name(msg_type: str, content_dict: dict) -> str:
        if msg_type == "image":
            return ""
        if msg_type == "audio":
            return str(content_dict.get("file_name", "") or "").strip() or "voice"
        return str(content_dict.get("file_name", "") or "").strip()

    @staticmethod
    def _attachment_resource_key(msg_type: str, content_dict: dict) -> str:
        if msg_type == "image":
            return str(content_dict.get("image_key", "") or "").strip()
        return str(content_dict.get("file_key", "") or "").strip()

    @staticmethod
    def _mention_payload(mention: Any) -> MentionPayload:
        if isinstance(mention, dict):
            key = str(mention.get("key", "") or "").strip()
            name = str(mention.get("name", "") or "").strip()
            direct_open_id = str(mention.get("open_id", "") or "").strip()
            mention_id = mention.get("id")
        else:
            key = str(getattr(mention, "key", "") or "").strip()
            name = str(getattr(mention, "name", "") or "").strip()
            direct_open_id = str(getattr(mention, "open_id", "") or "").strip()
            mention_id = getattr(mention, "id", None)

        open_id = ""
        if isinstance(mention_id, dict):
            open_id = str(mention_id.get("open_id", "") or mention_id.get("id", "") or "").strip()
        elif isinstance(mention_id, str):
            open_id = mention_id.strip()
        elif mention_id is not None:
            open_id = str(
                getattr(mention_id, "open_id", "") or getattr(mention_id, "id", "") or ""
            ).strip()

        return {
            "key": key,
            "name": name,
            "open_id": direct_open_id or open_id,
        }

    def _mention_payloads(self, mentions: list) -> list[MentionPayload]:
        return [self._mention_payload(mention) for mention in mentions]

    def _effective_trigger_open_ids(self) -> set[str]:
        if not self._configured_bot_open_id:
            return set()
        return {self._configured_bot_open_id, *self._configured_trigger_open_ids}

    def _normalize_mentions(self, text: str, mentions: list) -> str:
        """Strip trigger mentions from group text, keep other @members readable."""
        normalized = text
        trigger_open_ids = self._effective_trigger_open_ids()
        for mention in mentions:
            payload = self._mention_payload(mention)
            key = payload["key"]
            mention_open_id = payload["open_id"]
            mention_name = str(
                payload["name"]
                or mention_open_id[:8]
            ).strip()
            if not key:
                continue
            if mention_open_id and mention_open_id in trigger_open_ids:
                normalized = normalized.replace(key, "")
            else:
                normalized = normalized.replace(key, f"@{mention_name}")
        return normalized.strip()

    @staticmethod
    def _sender_ids(sender_id: Any) -> tuple[str, str]:
        if sender_id is None:
            return "", ""
        return (
            str(getattr(sender_id, "user_id", "") or "").strip(),
            str(getattr(sender_id, "open_id", "") or "").strip(),
        )

    def _is_bot_mentioned(self, mentions: list) -> bool:
        """Whether the mentions list contains a valid trigger open_id.

        Fail-closed: without a configured bot open_id, no mention counts as a
        trigger (same contract as FOCUS).
        """
        if not mentions:
            return False
        trigger_open_ids = self._effective_trigger_open_ids()
        if not trigger_open_ids:
            if not self._bot_open_id_error_logged:
                logger.error(
                    "no bot_open_id configured; group mention triggering is disabled"
                )
                self._bot_open_id_error_logged = True
            return False
        for mention in mentions:
            if self._mention_payload(mention)["open_id"] in trigger_open_ids:
                return True
        return False

    # ---- inbound: raw event dispatch ----

    def _on_raw_message(self, data: P2ImMessageReceiveV1) -> None:
        """Parse a raw message event and dispatch by message type."""
        try:
            self._handle_raw_message(data)
        except Exception as e:
            logger.error("error handling message event: %s", e, exc_info=True)

    def _handle_raw_message(self, data: P2ImMessageReceiveV1) -> None:
        message = data.event.message
        sender = data.event.sender
        sender_type = getattr(sender, "sender_type", "") or "user"
        sender_user_id, sender_open_id = self._sender_ids(getattr(sender, "sender_id", None))
        chat_id = str(message.chat_id or "").strip()
        message_id = str(message.message_id or "").strip()
        msg_type = str(message.message_type or "").strip()
        chat_type = getattr(message, "chat_type", None) or "p2p"
        thread_id = str(getattr(message, "thread_id", "") or "").strip()
        root_id = str(getattr(message, "root_id", "") or "").strip()
        parent_id = str(getattr(message, "parent_id", "") or "").strip()
        mentions = getattr(message, "mentions", None) or []
        try:
            create_time = int(message.create_time or 0)
        except (TypeError, ValueError):
            create_time = 0

        # Dedup first: Feishu retries must not double-dispatch.
        if self._is_duplicate(message_id):
            logger.info("skipping duplicate message: message_id=%s", message_id)
            return

        # merge_forward content is the fixed string "Merged and Forwarded
        # Message" (not JSON), so it dispatches before content parsing; the
        # handler fetches and expands the bundle (kite/forward_aggregator.py).
        if msg_type == "merge_forward":
            self._handler.on_merge_forward(
                InboundMergeForward(
                    message_id=message_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    sender_open_id=sender_open_id,
                    sender_user_id=sender_user_id,
                    sender_type=sender_type,
                    thread_id=thread_id,
                    root_id=root_id,
                    parent_id=parent_id,
                    create_time=create_time,
                )
            )
            return

        try:
            content_dict = json.loads(message.content)
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            logger.warning(
                "failed to parse message content: message_id=%s, msg_type=%s, error=%s, raw_content=%r",
                message_id, msg_type, type(e).__name__, message.content,
            )
            return

        bot_mentioned = self._is_bot_mentioned(mentions)
        self._remember_message_thread(message_id, chat_id=chat_id, thread_id=thread_id)

        logger.info(
            "inbound message: open_id=%s, user_id=%s, chat_type=%s, msg_type=%s, message_id=%s",
            sender_open_id, sender_user_id, chat_type, msg_type, message_id,
        )

        if msg_type in _ATTACHMENT_MESSAGE_TYPES:
            self._handler.on_attachment(
                InboundAttachment(
                    message_id=message_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    attachment_type=msg_type,
                    resource_key=self._attachment_resource_key(msg_type, content_dict),
                    file_name=self._attachment_message_name(msg_type, content_dict),
                    sender_open_id=sender_open_id,
                    sender_user_id=sender_user_id,
                    sender_type=sender_type,
                    thread_id=thread_id,
                    root_id=root_id,
                    parent_id=parent_id,
                    create_time=create_time,
                )
            )
            return

        text = self._extract_text(msg_type, content_dict)
        if chat_type == "group" and mentions:
            text = self._normalize_mentions(text, mentions)

        self._handler.on_message(
            InboundMessage(
                message_id=message_id,
                chat_id=chat_id,
                chat_type=chat_type,
                msg_type=msg_type,
                text=text,
                sender_open_id=sender_open_id,
                sender_user_id=sender_user_id,
                sender_type=sender_type,
                bot_mentioned=bot_mentioned,
                mentions=self._mention_payloads(mentions),
                thread_id=thread_id,
                root_id=root_id,
                parent_id=parent_id,
                create_time=create_time,
            )
        )

    def _on_raw_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """Parse a card button click and dispatch to the handler."""
        try:
            user_id = data.event.operator.user_id
            operator_open_id = str(getattr(data.event.operator, "open_id", "") or "").strip()
            chat_id = data.event.context.open_chat_id
            message_id = data.event.context.open_message_id
            action_value = data.event.action.value or {}
            if operator_open_id:
                action_value["_operator_open_id"] = operator_open_id
            if user_id:
                action_value["_operator_user_id"] = str(user_id).strip()
            # Form submissions carry input values; inject for the handler.
            if data.event.action.form_value:
                action_value["_form_value"] = data.event.action.form_value
            logger.info("card action: user=%s, action=%s", user_id, action_value)
            result = self._handler.on_card_action(
                CardAction(
                    operator_open_id=operator_open_id,
                    operator_user_id=str(user_id or "").strip(),
                    chat_id=str(chat_id or "").strip(),
                    message_id=str(message_id or "").strip(),
                    value=action_value,
                )
            )
            return make_card_response(
                card=result.card,
                toast=result.toast,
                toast_type=result.toast_type,
            )
        except Exception as e:
            logger.error("error handling card action event: %s", e, exc_info=True)
            return P2CardActionTriggerResponse()

    def _on_raw_bot_menu(self, data: P2ApplicationBotMenuV6) -> None:
        try:
            operator = data.event.operator
            user_id = operator.operator_id.user_id
            open_id = operator.operator_id.open_id
            event_key = data.event.event_key
            logger.info("bot menu click: user=%s, event_key=%s", user_id, event_key)
            self._handler.on_bot_menu(open_id, event_key)
        except Exception as e:
            logger.error("error handling bot menu event: %s", e, exc_info=True)

    def _on_raw_chat_disbanded(self, data: P2ImChatDisbandedV1) -> None:
        try:
            chat_id = str(data.event.chat_id or "").strip()
            if not chat_id:
                return
            logger.info("chat disbanded: chat=%s", chat_id)
            self._forget_chat_state(chat_id)
            self._handler.on_chat_unavailable(chat_id, reason="disbanded")
        except Exception as e:
            logger.error("error handling chat disbanded event: %s", e, exc_info=True)

    def _on_raw_chat_member_bot_deleted(self, data: P2ImChatMemberBotDeletedV1) -> None:
        try:
            chat_id = str(data.event.chat_id or "").strip()
            if not chat_id:
                return
            logger.info("bot removed from chat: chat=%s", chat_id)
            self._forget_chat_state(chat_id)
            self._handler.on_chat_unavailable(chat_id, reason="bot_removed")
        except Exception as e:
            logger.error("error handling bot removed event: %s", e, exc_info=True)

    def _on_raw_message_recalled(self, data: P2ImMessageRecalledV1) -> None:
        try:
            message_id = str(data.event.message_id or "").strip()
            chat_id = str(data.event.chat_id or "").strip()
            if not message_id:
                return
            logger.info("message recalled: chat=%s message_id=%s", chat_id, message_id)
            self._handler.on_message_recalled(chat_id, message_id)
        except Exception as e:
            logger.error("error handling message recalled event: %s", e, exc_info=True)

    # ---- outbound ----

    @staticmethod
    def _detect_id_type(receive_id: str) -> str:
        """Detect receive_id_type by prefix (ou_ -> open_id, else chat_id)."""
        if receive_id.startswith("ou_"):
            return "open_id"
        return "chat_id"

    def send_message(self, chat_id: str, msg_type: str, content: str) -> None:
        """Send a message of any type."""
        self.send_message_get_id(chat_id, msg_type, content)

    def send_message_get_id(self, chat_id: str, msg_type: str, content: str) -> Optional[str]:
        """Send a message and return its message_id, None on failure."""
        id_type = self._detect_id_type(chat_id)
        request = CreateMessageRequest.builder() \
            .receive_id_type(id_type) \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()) \
            .build()
        try:
            response = self.client.im.v1.message.create(request)
        except Exception as e:
            logger.exception("send message failed (SDK exception): %s", e)
            return None
        if not response.success():
            logger.error("send message failed: code=%s, msg=%s", response.code, response.msg)
            return None
        try:
            message_id = response.data.message_id
        except AttributeError:
            return None
        logger.info("message sent: receive_id=%s, message_id=%s, msg_type=%s", chat_id, message_id, msg_type)
        return message_id

    def upload_image(self, local_path: str) -> str | None:
        normalized_path = str(local_path or "").strip()
        if not normalized_path:
            return None
        image_path = pathlib.Path(normalized_path).expanduser()
        if not image_path.exists() or not image_path.is_file():
            logger.error("image upload failed: path missing or not a file path=%s", image_path)
            return None
        try:
            with image_path.open("rb") as image_file:
                request = CreateImageRequest.builder().request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(image_file)
                    .build()
                ).build()
                response = self.client.im.v1.image.create(request)
        except Exception as e:
            logger.exception("image upload failed (SDK exception): path=%s error=%s", image_path, e)
            return None
        if not response.success():
            logger.error("image upload failed: path=%s code=%s msg=%s", image_path, response.code, response.msg)
            return None
        image_key = str(getattr(getattr(response, "data", None), "image_key", "") or "").strip()
        if not image_key:
            logger.error("image upload failed: path=%s empty image_key", image_path)
            return None
        return image_key

    def reply_local_image(
        self,
        chat_id: str,
        local_path: str,
        *,
        parent_message_id: str = "",
        reply_in_thread: bool = False,
    ) -> str | None:
        image_key = self.upload_image(local_path)
        if not image_key:
            return None
        content = json.dumps({"image_key": image_key}, ensure_ascii=False)
        normalized_parent_id = str(parent_message_id or "").strip()
        if normalized_parent_id:
            return self.reply_to_message(
                normalized_parent_id,
                "image",
                content,
                reply_in_thread=self._should_reply_in_thread(normalized_parent_id, reply_in_thread),
            )
        return self.send_image_by_key(chat_id, image_key)

    def send_image_by_key(self, chat_id: str, image_key: str) -> str | None:
        normalized_image_key = str(image_key or "").strip()
        if not normalized_image_key:
            return None
        return self.send_message_get_id(
            chat_id,
            "image",
            json.dumps({"image_key": normalized_image_key}, ensure_ascii=False),
        )

    @staticmethod
    def _patch_error_ext(response: Any) -> str:
        raw = getattr(response, "raw", None)
        if isinstance(raw, dict):
            return str(raw.get("ext", "") or "")
        return ""

    @staticmethod
    def _is_retryable_patch_exception(exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        module_name = type(exc).__module__.lower()
        class_name = type(exc).__name__.lower()
        text = str(exc).lower()
        if "timeout" in class_name or "timeout" in text:
            return True
        return "requests" in module_name and "timeout" in class_name

    def patch_message_result(self, message_id: str, content: str) -> MessagePatchResult:
        """Update (patch) a sent message's content with a structured result."""
        request = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(PatchMessageRequestBody.builder()
                .content(content)
                .build()) \
            .build()
        try:
            response = self.client.im.v1.message.patch(request)
        except Exception as e:
            if self._is_retryable_patch_exception(e):
                logger.warning(
                    "message patch failed, retry later: message_id=%s error=%s",
                    message_id,
                    e,
                )
                return MessagePatchResult.retry_later(_PATCH_MESSAGE_RETRY_SECONDS)
            logger.error("message patch failed (SDK exception): message_id=%s error=%s", message_id, e)
            return MessagePatchResult.failure()
        if not response.success():
            code = str(getattr(response, "code", "") or "").strip()
            ext = self._patch_error_ext(response)
            if code == "230020":
                logger.warning(
                    "message patch rate-limited, retry later: message_id=%s code=%s msg=%s ext=%s",
                    message_id,
                    code,
                    response.msg,
                    ext,
                )
                return MessagePatchResult.retry_later(_PATCH_MESSAGE_RETRY_SECONDS)
            logger.error(
                "message patch failed: message_id=%s code=%s msg=%s ext=%s",
                message_id,
                code,
                response.msg,
                ext,
            )
            return MessagePatchResult.failure()
        return MessagePatchResult.success()

    def patch_message(self, message_id: str, content: str) -> bool:
        """Update (patch) a sent message's content; True on success."""
        return self.patch_message_result(message_id, content).ok

    def _should_reply_in_thread(self, parent_message_id: str, explicit_reply_in_thread: bool) -> bool:
        if explicit_reply_in_thread:
            return True
        return bool(self._lookup_message_thread(parent_message_id))

    def reply(
        self,
        chat_id: str,
        text: str,
        *,
        parent_message_id: str = "",
        reply_in_thread: bool = False,
    ) -> bool:
        """Send a text message (optionally as a reply to a parent message)."""
        return bool(self.reply_get_id(
            chat_id,
            text,
            parent_message_id=parent_message_id,
            reply_in_thread=reply_in_thread,
        ))

    def reply_get_id(
        self,
        chat_id: str,
        text: str,
        *,
        parent_message_id: str = "",
        reply_in_thread: bool = False,
    ) -> str:
        content = json.dumps({"text": text})
        normalized_parent_id = str(parent_message_id or "").strip()
        if normalized_parent_id:
            return str(
                self.reply_to_message(
                    normalized_parent_id,
                    "text",
                    content,
                    reply_in_thread=self._should_reply_in_thread(normalized_parent_id, reply_in_thread),
                )
                or ""
            ).strip()
        return str(self.send_message_get_id(chat_id, "text", content) or "").strip()

    def reply_card(
        self,
        chat_id: str,
        card: dict,
        *,
        parent_message_id: str = "",
        reply_in_thread: bool = False,
    ) -> None:
        """Send an interactive card message."""
        content = json.dumps(card)
        normalized_parent_id = str(parent_message_id or "").strip()
        if normalized_parent_id:
            self.reply_to_message(
                normalized_parent_id,
                "interactive",
                content,
                reply_in_thread=self._should_reply_in_thread(normalized_parent_id, reply_in_thread),
            )
            return
        self.send_message(chat_id, "interactive", content)

    def reply_to_message(
        self,
        parent_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> Optional[str]:
        """Reply to a specific message; return the new message_id, None on failure."""
        effective_reply_in_thread = self._should_reply_in_thread(parent_id, reply_in_thread)
        request = ReplyMessageRequest.builder() \
            .message_id(parent_id) \
            .request_body(ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .reply_in_thread(effective_reply_in_thread)
                .build()) \
            .build()
        try:
            response = self.client.im.v1.message.reply(request)
        except Exception as e:
            logger.error("reply failed (SDK exception): %s", e)
            return None
        if not response.success():
            logger.error("reply failed: code=%s, msg=%s", response.code, response.msg)
            return None
        try:
            reply_message_id = response.data.message_id
        except AttributeError:
            return None
        logger.info(
            "reply sent: parent_id=%s message_id=%s msg_type=%s reply_in_thread=%s",
            parent_id,
            reply_message_id,
            msg_type,
            effective_reply_in_thread,
        )
        return reply_message_id

    def delete_message(self, message_id: str) -> bool:
        """Delete a message; True on success."""
        request = DeleteMessageRequest.builder() \
            .message_id(message_id) \
            .build()
        try:
            response = self.client.im.v1.message.delete(request)
        except Exception as e:
            logger.error("delete message failed (SDK exception): %s", e)
            return False
        if not response.success():
            logger.error("delete message failed: code=%s, msg=%s", response.code, response.msg)
            return False
        return True

    def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        *,
        resource_type: str,
    ) -> DownloadedMessageResource:
        """Download a Feishu message resource (content, file name, content type)."""
        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type(resource_type) \
            .build()
        try:
            response = self.client.im.v1.message_resource.get(request)
        except Exception as e:
            raise RuntimeError(f"resource download failed (SDK exception): {e}") from e
        if not response.success():
            raise RuntimeError(f"resource download failed: code={response.code}, msg={response.msg}")
        raw = getattr(response, "raw", None)
        headers = getattr(raw, "headers", {}) if raw is not None else {}
        content_type = str(headers.get("Content-Type", "") or "").strip()
        return DownloadedMessageResource(
            content=response.file.read(),
            file_name=str(getattr(response, "file_name", "") or "").strip(),
            content_type=content_type,
        )

    def download_file(self, message_id: str, file_key: str) -> bytes:
        """Download a file attachment; raises RuntimeError on failure."""
        return self.download_message_resource(
            message_id,
            file_key,
            resource_type="file",
        ).content

    def list_messages(
        self,
        chat_id: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        sort_type: str = "ByCreateTimeAsc",
        page_size: int = 50,
        page_token: str = "",
    ) -> ListedMessagesPage:
        """Fetch one page of a chat's message history.

        ``GET /open-apis/im/v1/messages`` with ``container_id_type=chat``
        (FOCUS ``_list_history_messages_page``, same contract): start/end are
        second-precision unix timestamps as strings; raises RuntimeError on
        failure so the assistant-mode history fetch can fail closed
        (group-chat contract §4.5).
        """
        builder = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(str(chat_id or "").strip())
            .sort_type(sort_type)
            .page_size(int(page_size))
        )
        if start_time is not None:
            builder = builder.start_time(str(start_time))
        if end_time is not None:
            builder = builder.end_time(str(end_time))
        if page_token:
            builder = builder.page_token(page_token)
        request = builder.build()
        try:
            response = self.client.im.v1.message.list(request)
        except Exception as e:
            raise RuntimeError(f"message history fetch failed (SDK exception): {e}") from e
        if not response.success():
            raise RuntimeError(
                f"message history fetch failed: code={response.code}, msg={response.msg}"
            )
        body = response.data
        return ListedMessagesPage(
            items=list(getattr(body, "items", None) or []),
            has_more=bool(getattr(body, "has_more", False)),
            page_token=str(getattr(body, "page_token", "") or "").strip(),
        )

    def fetch_merge_forward_items(self, message_id: str) -> list[Any]:
        """Fetch the flattened message list of a merge_forward bundle.

        ``GET /open-apis/im/v1/messages/{message_id}`` on a merge_forward
        message returns the bundle root plus every descendant in one flat
        list (``upper_message_id`` links children to parents); the
        application layer rebuilds the tree from it. Raises RuntimeError on
        failure (FOCUS ``get_message_items``, same contract).
        """
        normalized = str(message_id or "").strip()
        if not normalized:
            return []
        request = GetMessageRequest.builder().message_id(normalized).build()
        try:
            response = self.client.im.v1.message.get(request)
        except Exception as e:
            raise RuntimeError(f"merge_forward fetch failed (SDK exception): {e}") from e
        if not response.success():
            raise RuntimeError(
                f"merge_forward fetch failed: code={response.code}, msg={response.msg}"
            )
        return list(getattr(response.data, "items", None) or [])

    # ---- startup ----

    def start(self) -> None:
        """Start the websocket long connection (blocking)."""
        configure_feishu_ws_proxy(self._feishu_ws_proxy_mode)
        ws_client = lark.ws.Client(
            self.app_id, self.app_secret,
            event_handler=self._event_handler,
            log_level=lark.LogLevel.INFO,
        )
        logger.info("Feishu transport starting, connecting websocket ...")
        ws_client.start()
