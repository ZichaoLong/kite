"""Application layer inbound path: command routing + TransportHandler impl.

Implements the MVP inbound contract (docs/contracts/mvp-scope.md):

- identity: /init <token> registers admins (first-admin bootstrap); a
  non-admin gets /help and /init only, everything else is politely rejected
  (§5);
- plain text -> resolve the chat's binding (first use creates a session with
  cwd=default_working_dir and binds it) -> submit the prompt carrying the
  binding's permission_mode + plan_mode explicitly -> record prompt
  ownership -> minimal ack;
- attachment messages (images, docs/contracts/images.md §2): staged into
  the bound session's cwd by AttachmentDomain with a TTL'd pending record;
  the next text prompt from the same (sender, chat) consumes them as native
  kap image content parts (base64 source) plus staged paths as text
  context, with consume-once + restore-on-submit-failure;
- the loopback control plane's prompt/submit endpoint reuses that same
  submit discipline for `kitectl prompt send` (minus the Feishu ack), so
  CLI-sent prompts record ownership exactly like Feishu-originated ones
  (docs/decisions/control-plane.md);
- group chats (docs/contracts/group-chat.md): a non-activated group ignores
  everything except admin slash commands; in an activated mention_only
  group any member's @bot+text enters the same prompt path as p2p (first
  use creates+binds), slash commands stay admin-only except /abort
  (initiator-or-admin actor check), and non-@ messages are ignored entirely.
  In assistant mode every member text message is appended to the per-chat
  log (GroupLogStore, state axis 6) and @bot+text triggers with the log
  since the trigger boundary — merged with a Feishu REST history backfill
  by GroupHistoryRecovery — as the context envelope; a history fetch
  failure blocks the prompt with an explicit notice (fail-closed), and the
  boundary advances only after a successful submit. In all mode every
  member text message triggers a plain prompt directly (no @ needed, no
  log, no context injection); the mode requires an exclusive session, so
  /group-mode all and /switch|/new rebinds run the all-mode exclusivity
  preflight (kite/preflights.py) and deny with a remediation text when the
  session is or would be shared (contract §2, fail-closed §4.6); the rule
  applies in both directions (§3.8) — any chat rebinding into a session an
  all-mode group already occupies is denied the same way. Group
  activation config lives in the GroupConfigStore (state axis 5);
- merge_forward bundles: each merge_forward message's children are fetched
  and buffered per (sender, chat) by the ForwardAggregator; a short window
  later the expanded `<forwarded_messages>` transcript enters the normal
  prompt path (admin-gated like any p2p text). Group forwards dispatch per
  mode at ingress (group-chat §3.7): mention_only drops them silently (a
  forward never carries an @mention), assistant appends the flattened
  transcript to the group log as context material (never a trigger), and
  all aggregates them through the same window into a plain prompt;
- the MVP slash commands (/new /sessions /switch /detach /attach /mode
  /plan /group /group-mode /status /abort /help /init); in-flight-work-sensitive commands run
  the reason-coded preflights in kite/preflights.py (/new denies with an
  active prompt, /detach only notes it), and /new /switch rebinds fire the
  on_session_unbound seam so the outbound path can fail-close sweep the old
  session's pending approvals/questions;
- card-action dispatch: session_switch buttons are handled here;
  approval/question buttons route to no-op-with-log seam methods that the
  outbound path (E3) fills in.

All state mutations are serialized on the RuntimeLoop
(docs/architecture/kite-design.md §3); the Feishu transport thread only
round-trips through it.

Scope boundaries (what this module deliberately does NOT do):

- kap event consumption, execution/terminal card lifecycle, and the
  approval/question push lifecycle are the OUTBOUND path (E3). Queued
  prompts never create cards, and card creation for started prompts is
  event-driven (kite-design.md §6), so the submit path below only sends a
  short text ack — never a card.
- The few kap REST endpoints the adapter does not type yet (create/get
  session, submit/abort prompt) are reached through KapSessionOps, the one
  place in the application layer that knows REST paths and payload keys; it
  delegates envelope/error semantics to the adapter's KapRestClient.
"""

from __future__ import annotations

import hmac
import json
import logging
import pathlib
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from kite import cards
from kite import config as kite_config
from kite import preflights
from kite.adapters.kap_server import (
    KapError,
    KapTransportError,
    PromptQueueState,
    SessionSummary,
)
from kite.attachment_domain import AttachmentDomain, AttachmentPorts
from kite.card_text_projection import (
    project_interactive_card_text,
    verify_terminal_result_checksum,
)
from kite.command_surface import (
    SlashCommand,
    build_help_text,
    build_usage_text,
    parse_permission_mode_arg,
    parse_plan_mode_arg,
    parse_slash_command,
)
from kite.control_plane import ControlError
from kite.feishu_card_markdown import sanitize_runtime_markdown_for_feishu_card
from kite.feishu_transport import (
    CardAction,
    CardActionResponse,
    DownloadedMessageResource,
    FeishuTransport,
    InboundAttachment,
    InboundMergeForward,
    InboundMessage,
    TransportHandler,
)
from kite.forward_aggregator import ForwardAggregator, MergedForwardBatch
from kite.group_history import GroupHistoryRecovery
from kite.identity_names import IdentityNames
from kite.prompt_ownership import (
    CERTAINTY_BEST_EFFORT,
    PromptOwnership,
    PromptOwnershipEntry,
)
from kite.runtime_loop import RuntimeLoop, RuntimeLoopClosedError
from kite.stores.binding_store import (
    DEFAULT_ATTACHED,
    DEFAULT_PERMISSION_MODE,
    DEFAULT_PLAN_MODE,
    PERMISSION_MODE_YOLO,
    VALID_PERMISSION_MODES,
    BindingStore,
    StoredBinding,
)
from kite.stores.group_config_store import (
    GROUP_MODE_ALL,
    GROUP_MODE_ASSISTANT,
    GROUP_MODE_MENTION_ONLY,
    VALID_GROUP_MODES,
    GroupConfigStore,
)
from kite.stores.group_log_store import GroupLogStore
from kite.stores.pending_attachment_store import PendingAttachmentStore

logger = logging.getLogger("kite.app")

# kap business error codes this path depends on (upstream
# packages/kap-server/src/protocol/error-codes.ts; spike S2 observed the
# 40402 re-abort behavior).
KAP_ERROR_SESSION_NOT_FOUND = 40401
KAP_ERROR_PROMPT_NOT_PENDING = 40402

# Card-action names owned by this module (the /sessions switch buttons).
ACTION_SESSION_SWITCH = "session_switch"

# E3 seam: card-action names owned by the outbound path. The approval and
# question names are defined next to their card builders in kite/cards.py.
ACTION_QUESTION_ANSWER = cards.ACTION_QUESTION_ANSWER
APPROVAL_CARD_ACTIONS = frozenset(
    {cards.ACTION_APPROVAL_RESOLVE, cards.ACTION_APPROVAL_REJECT_WITH_FEEDBACK}
)
QUESTION_CARD_ACTIONS = frozenset({ACTION_QUESTION_ANSWER})

# /sessions renders one page (mvp-scope aligned item 3); switch buttons are
# capped separately because a Feishu action row should stay short.
_SESSIONS_LIST_CAP = 20
_SESSIONS_BUTTON_CAP = 10

_SESSION_TITLE_MAX = 30

_KAP_UNREACHABLE_TEXT = (
    "无法连接 kap-server，本次操作未完成。请稍后再试；"
    "若持续失败，请运行 `kitectl service status` 排查。"
)
_NOT_BOUND_TEXT = (
    "尚未绑定会话。直接发送文字即可自动创建并绑定会话；"
    "或发送 /sessions 查看已有会话后用 /switch 〈id〉 切换。"
)
_NON_ADMIN_TEXT = "抱歉，KITE 目前仅对管理员开放。发送 /help 查看说明。"
_GROUP_NOT_ACTIVATED_MEMBER_TEXT = "本群尚未激活，@我 发送的消息不会被处理。请联系管理员发送 /group activate 激活。"
_GROUP_NOT_ACTIVATED_ADMIN_TEXT = "本群尚未激活，@我 发送的消息不会被处理。发送 /group activate 激活后，群成员即可 @我 使用。"
_GROUP_COMMAND_ADMIN_ONLY_TEXT = "群聊中命令仅管理员可用。"
_ABORT_DENIED_TEXT = "只有该 prompt 的发起者或管理员可以中止它。"
_LAST_TEXT_CAP = 15000
# /last history fallback: how many of the newest chat messages are scanned
# for a verifiable terminal card when the local store has no record.
_LAST_HISTORY_SCAN_LIMIT = 20
_LAST_HISTORY_FETCH_FAILED_TEXT = "读取聊天记录失败，请稍后重试。"
_GROUP_PROMPT_HINT_TEXT = "群聊中请 @我 并发送文字来提交 prompt。"
# Fail-closed §4.5: never answer without the context, say so explicitly.
_GROUP_HISTORY_FETCH_FAILED_TEXT = (
    "获取群聊历史上下文失败，本次消息未提交——我不会在缺少上下文的情况下回答。"
    "请稍后重试；若持续失败请联系管理员排查。"
)
_GROUP_ASSISTANT_NOT_WIRED_TEXT = "群聊 assistant 模式未正确配置，消息未提交。请联系管理员排查。"


@dataclass(frozen=True, slots=True)
class SubmitPromptResult:
    """The two fields the inbound path needs from a prompt submission."""

    prompt_id: str
    status: str  # upstream prompt status: running | queued | blocked


class KapSessionOps:
    """Typed view of the kap session/prompt REST surface the inbound path needs.

    The adapter's typed slice (list_sessions / get_prompts) is delegated to
    directly; the endpoints it does not type yet (create/get session,
    submit/abort prompt) go through KapRestClient's generic envelope
    handling, so envelope unwrapping and KapError/KapTransportError
    semantics stay in the adapter. Wire paths and payload keys live ONLY in
    this class on the application side.
    """

    def __init__(self, rest: Any, *, model: str | None = None) -> None:
        self._rest = rest
        # Carried explicitly on every prompt: REST-created sessions inherit
        # neither the KIMI_MODEL_* overlay nor config.toml's default_model
        # (spike-results §0 + live finding 2026-07-22).
        self._model = model

    def create_session(self, *, cwd: str, title: str) -> SessionSummary:
        data = self._rest.call("POST", "/sessions", {"title": title, "metadata": {"cwd": cwd}})
        return self._parse_session(data, context="create session")

    def get_session(self, session_id: str) -> SessionSummary:
        data = self._rest.get(f"/sessions/{_quote(session_id)}")
        return self._parse_session(data, context="get session")

    def submit_prompt(
        self,
        session_id: str,
        text: str,
        *,
        permission_mode: str,
        plan_mode: bool,
    ) -> SubmitPromptResult:
        return self.submit_prompt_content(
            session_id,
            [{"type": "text", "text": text}],
            permission_mode=permission_mode,
            plan_mode=plan_mode,
        )

    def submit_prompt_content(
        self,
        session_id: str,
        content: list[dict[str, Any]],
        *,
        permission_mode: str,
        plan_mode: bool,
    ) -> SubmitPromptResult:
        # permission_mode / plan_mode / model are carried explicitly on every
        # prompt (kite-design.md §7; spike-results §0 for the model part).
        # The content-part wire shape is the upstream promptSubmissionSchema:
        # text parts and native image parts with a base64 source
        # (packages/protocol/src/rest/prompt.ts + message.ts
        # imageSourceSchema; kap-server converts base64 to a data-URI core
        # part and compresses inline, so no upload step exists or is needed).
        payload: dict[str, Any] = {
            "content": content,
            "permission_mode": permission_mode,
            "plan_mode": plan_mode,
        }
        if self._model:
            payload["model"] = self._model
        data = self._rest.call(
            "POST",
            f"/sessions/{_quote(session_id)}/prompts",
            payload,
        )
        if not isinstance(data, dict):
            raise KapTransportError("submit prompt: unexpected data shape")
        prompt_id = data.get("prompt_id")
        status = data.get("status")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise KapTransportError("submit prompt: unexpected data shape")
        if not isinstance(status, str) or not status:
            raise KapTransportError("submit prompt: unexpected data shape")
        return SubmitPromptResult(prompt_id=prompt_id, status=status)

    def abort_prompt(self, session_id: str, prompt_id: str) -> None:
        # A re-abort of a finished prompt surfaces as KapError(40402).
        self._rest.call(
            "POST",
            f"/sessions/{_quote(session_id)}/prompts/{_quote(prompt_id)}:abort",
        )

    def list_sessions(self) -> list[SessionSummary]:
        return self._rest.list_sessions()

    def get_prompts(self, session_id: str) -> PromptQueueState:
        return self._rest.get_prompts(session_id)

    @staticmethod
    def _parse_session(data: Any, *, context: str) -> SessionSummary:
        if not isinstance(data, dict):
            raise KapTransportError(f"{context}: unexpected data shape")
        session_id = str(data.get("id") or "").strip()
        if not session_id:
            raise KapTransportError(f"{context}: unexpected data shape")
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        cwd = metadata.get("cwd")
        pending_interaction = data.get("pending_interaction")
        return SessionSummary(
            session_id=session_id,
            title=str(data.get("title") or ""),
            cwd=str(cwd) if isinstance(cwd, str) and cwd else None,
            busy=bool(data.get("busy")),
            pending_interaction=(
                str(pending_interaction)
                if isinstance(pending_interaction, str) and pending_interaction
                else None
            ),
            archived=bool(data.get("archived")),
        )


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _persist_admins_to_config(admin_open_ids: set[str]) -> None:
    """Persist the admin set into system.yaml (mvp-scope §5)."""
    kite_config.save_system_config_updates({"admin_open_ids": sorted(admin_open_ids)})


class AppHandler(TransportHandler):
    """The MVP inbound handler (single chat only).

    Constructor dependencies are injected so tests can substitute fakes and
    E3/kited wiring can attach the WS subscribe hook and the ownership map:

    - ``rest``: a KapRestClient (or compatible fake);
    - ``on_session_bound``: called whenever a binding starts pointing at a
      session (first-use create, /new, /switch) so the outbound side can
      subscribe its event stream live (kited subscribes persisted bindings
      at startup);
    - ``persist_admins``: how /init persists the admin set (default: into
      system.yaml via kite.config).
    """

    def __init__(
        self,
        *,
        transport: FeishuTransport,
        rest: Any,
        binding_store: BindingStore,
        attachment_store: PendingAttachmentStore,
        group_config_store: GroupConfigStore,
        runtime_loop: RuntimeLoop,
        config: Mapping[str, Any],
        init_token: str,
        prompt_model: str | None = None,
        prompt_ownership: Optional[PromptOwnership] = None,
        on_session_bound: Optional[Callable[[str], None]] = None,
        persist_admins: Optional[Callable[[set[str]], None]] = None,
        terminal_store: Any = None,
        names: Optional[IdentityNames] = None,
        forward_timer_factory: Any = None,
        group_log_store: Optional[GroupLogStore] = None,
        group_history: Optional[GroupHistoryRecovery] = None,
    ) -> None:
        self._transport = transport
        self._ops = KapSessionOps(rest, model=prompt_model)
        self._binding_store = binding_store
        self._group_config_store = group_config_store
        self._group_log_store = group_log_store
        self._group_history = group_history
        self._terminal_store = terminal_store
        self._loop = runtime_loop
        self._default_working_dir = kite_config.default_working_dir(config)
        self._admins: set[str] = kite_config.admin_open_ids(config)
        self._attachment_max_bytes = kite_config.attachment_max_bytes(config)
        # Inbound image staging (docs/contracts/images.md §2): on_attachment
        # stages into the session cwd; the next text prompt consumes.
        self._attachment_domain = AttachmentDomain(
            ports=AttachmentPorts(
                download=self._download_image_resource,
                reply=self._reply,
                resolve_cwd=self._resolve_attachment_cwd,
            ),
            store=attachment_store,
            ttl_seconds=kite_config.attachment_ttl_seconds(config),
            max_bytes=self._attachment_max_bytes,
        )
        # Merge-forward aggregation (FOCUS forward_aggregator port; p2p and
        # all-mode groups): merge_forward children buffer for a short window,
        # then the merged transcript enters the prompt path as one message.
        # Test doubles can inject a fake timer factory; production kited
        # injects the shared IdentityNames cache.
        if names is None:
            names = IdentityNames(getattr(transport, "fetch_user_name", lambda _open_id: None))
        self._names = names
        self._forward_aggregator = ForwardAggregator(
            on_batch=self._on_forward_batch,
            name_of=self._names.name_of,
            timer_factory=forward_timer_factory,
        )
        self._init_token = str(init_token or "").strip()
        if not self._init_token:
            logger.error("init token is empty; /init can never succeed")
        # NB: PromptOwnership defines __len__, so `or` would treat an empty
        # shared map as falsy and silently split ownership into two maps
        # (observed live 2026-07-22: approvals expired as "unattributable").
        self._ownership = (
            prompt_ownership if prompt_ownership is not None else PromptOwnership()
        )
        self._on_session_bound = on_session_bound
        self._persist_admins = persist_admins or _persist_admins_to_config
        self._commands: dict[str, Callable[[InboundMessage, str], None]] = {
            "/new": self._cmd_new,
            "/sessions": self._cmd_sessions,
            "/switch": self._cmd_switch,
            "/detach": self._cmd_detach,
            "/attach": self._cmd_attach,
            "/mode": self._cmd_mode,
            "/plan": self._cmd_plan,
            "/group": self._cmd_group,
            "/group-mode": self._cmd_group_mode,
            "/status": self._cmd_status,
            "/last": self._cmd_last,
            "/abort": self._cmd_abort,
            "/help": self._cmd_help,
            "/init": self._cmd_init,
            "/whoami": self._cmd_whoami,
        }

    @property
    def prompt_ownership(self) -> PromptOwnership:
        """The in-memory ownership map (shared with the outbound path)."""
        return self._ownership

    # ------------------------------------------------------------------
    # TransportHandler entry points (transport thread; serialize via loop)
    # ------------------------------------------------------------------

    def on_message(self, message: InboundMessage) -> None:
        try:
            self._loop.call(self._on_message_impl, message)
        except Exception:
            logger.exception(
                "failed to handle inbound message chat=%s message_id=%s",
                message.chat_id,
                message.message_id,
            )
            self._safe_reply(
                message.chat_id,
                "处理这条消息时出现内部错误，已记录日志。",
                parent_message_id=message.message_id,
            )

    def on_attachment(self, attachment: InboundAttachment) -> None:
        try:
            self._loop.call(self._on_attachment_impl, attachment)
        except Exception:
            logger.exception(
                "failed to handle attachment chat=%s message_id=%s",
                attachment.chat_id,
                attachment.message_id,
            )

    def on_merge_forward(self, message: InboundMergeForward) -> None:
        try:
            self._loop.call(self._on_merge_forward_impl, message)
        except Exception:
            logger.exception(
                "failed to handle merge_forward chat=%s message_id=%s",
                message.chat_id,
                message.message_id,
            )

    def on_card_action(self, action: CardAction) -> CardActionResponse:
        try:
            return self._loop.call(self._on_card_action_impl, action)
        except Exception:
            logger.exception(
                "failed to handle card action chat=%s value=%s", action.chat_id, action.value
            )
            return CardActionResponse(toast="处理失败，请稍后重试。", toast_type="error")

    def on_message_recalled(self, chat_id: str, message_id: str) -> None:
        # MVP: recall does not pull back submitted prompts (no such contract).
        logger.info("message recalled: chat=%s message_id=%s (ignored)", chat_id, message_id)

    def on_chat_unavailable(self, chat_id: str, *, reason: str = "") -> None:
        # MVP: the binding is kept; a p2p chat cannot come back under the
        # same id, and silently dropping the bookmark would lose history.
        logger.info("chat unavailable: chat=%s reason=%s (binding kept)", chat_id, reason)

    def on_bot_menu(self, open_id: str, event_key: str) -> None:
        logger.info("bot menu click: open_id=%s event_key=%s (ignored)", open_id, event_key)

    # ------------------------------------------------------------------
    # Message routing (RuntimeLoop thread)
    # ------------------------------------------------------------------

    def _on_message_impl(self, message: InboundMessage) -> None:
        text = message.text.strip()
        command = parse_slash_command(text)
        if message.chat_type == "group":
            # Group chats follow the group-chat contract ingress matrix
            # (docs/contracts/group-chat.md §3.2), not the p2p identity gate.
            self._on_group_message_impl(message, text, command)
            return
        if not self._is_admin(message.sender_open_id):
            # Identity gate (mvp-scope §5): non-admins get /help, /init and
            # /whoami only.
            if command is not None and command.name == "/help":
                self._reply_to(message, build_help_text())
            elif command is not None and command.name == "/init":
                self._cmd_init(message, command.arg)
            elif command is not None and command.name == "/whoami":
                self._cmd_whoami(message, command.arg)
            else:
                self._reply_to(message, _NON_ADMIN_TEXT)
            return
        if command is not None:
            self._dispatch_command(message, command)
            return
        if not text:
            self._reply_to(message, "KITE 目前只处理文字消息。发送 /help 查看命令导航。")
            return
        # Plain text: E3's pending interactions (question numbered replies,
        # approval feedback text) get first claim; the rest becomes a prompt.
        if self.try_handle_interaction_reply(message):
            return
        self._handle_prompt(message)

    def _on_group_message_impl(
        self, message: InboundMessage, text: str, command: SlashCommand | None
    ) -> None:
        """Group ingress matrix (group-chat contract §3.2, fail-closed §4.1).

        - non-activated group: everything is ignored except admin slash
          commands (member @/slash gets one denial hint, never spam);
        - activated group: slash commands stay admin-only, except /abort
          which uses the initiator-or-admin actor check (§3.4). In
          mention_only mode non-@ messages are ignored entirely and any
          member's @bot+text enters the prompt path (first use creates+binds
          like p2p). In assistant mode every member text message is appended
          to the per-chat log (the bot's own and identity-less messages
          never enter) and @bot+text triggers with the log since the trigger
          boundary as context. In all mode every member text message
          triggers a plain prompt (no @ needed, no log, no context). A
          missing sender identity is treated as a non-member (§4.4) and
          never prompts.
        """
        activated = self._group_config_store.is_activated(message.chat_id)
        is_admin = self._is_admin(message.sender_open_id)
        if command is not None:
            if is_admin:
                self._dispatch_command(message, command)
                return
            if activated and command.name == "/abort":
                # /abort is the one member-reachable command in groups; the
                # actor check inside decides initiator vs bystander (§3.4).
                self._cmd_abort(message, command.arg)
                return
            self._reply_to(message, _GROUP_COMMAND_ADMIN_ONLY_TEXT)
            return
        if not activated:
            if message.bot_mentioned:
                self._reply_to(
                    message,
                    _GROUP_NOT_ACTIVATED_ADMIN_TEXT if is_admin else _GROUP_NOT_ACTIVATED_MEMBER_TEXT,
                )
            return
        group_config = self._group_config_store.load(message.chat_id)
        mode = group_config["mode"] if group_config is not None else GROUP_MODE_MENTION_ONLY
        if mode == GROUP_MODE_ASSISTANT:
            self._on_assistant_group_message(message, text)
            return
        if mode == GROUP_MODE_ALL:
            self._on_all_group_message(message, text)
            return
        if not message.bot_mentioned:
            # Non-@ group chatter is ignored entirely: no prompt, no
            # interaction claim, no context (§3.2).
            return
        if not text:
            self._reply_to(message, _GROUP_PROMPT_HINT_TEXT)
            return
        if not message.sender_open_id.strip():
            # Missing identity -> non-member (§4.4); never prompts.
            self._reply_to(message, "无法识别你的身份（缺少 open_id），消息未提交。")
            return
        if self.try_handle_interaction_reply(message):
            return
        self._handle_prompt(message)

    def _on_assistant_group_message(self, message: InboundMessage, text: str) -> None:
        """Assistant-mode group ingress (group-chat §3.2/§3.3).

        Every member text message is appended to the per-chat log — including
        the trigger message itself, which the context merge then excludes by
        seq (it is rendered as the current turn). @bot+text composes the
        envelope via the history recovery port and submits; the boundary
        advances to the trigger point only after a successful submit, so a
        failed submit (or a blocked fetch) never loses context silently.
        """
        if self._group_log_store is None or self._group_history is None:
            # Assistant mode cannot work without the log/history wiring:
            # fail closed (never degrade to answering without context).
            logger.error(
                "assistant mode active but group log/history not wired: chat=%s",
                message.chat_id,
            )
            if message.bot_mentioned and text:
                self._reply_to(message, _GROUP_ASSISTANT_NOT_WIRED_TEXT)
            return
        chat_id = message.chat_id
        sender = message.sender_open_id.strip()
        is_self = message.sender_type == "app" or (
            bool(sender) and sender == self._bot_open_id()
        )
        current_seq = 0
        if sender and not is_self and text:
            current_seq = self._group_log_store.append(
                chat_id,
                {
                    "message_id": message.message_id,
                    "created_at": max(int(message.create_time or 0), 0),
                    "sender_open_id": sender,
                    "sender_type": message.sender_type.strip() or "user",
                    "sender_name": self._names.name_of(
                        sender, sender_type=message.sender_type
                    ),
                    "msg_type": message.msg_type.strip() or "text",
                    "text": text,
                },
            )
        if not message.bot_mentioned:
            return
        if not text:
            self._reply_to(message, _GROUP_PROMPT_HINT_TEXT)
            return
        if not sender:
            # Missing identity -> non-member (§4.4); never prompts.
            self._reply_to(message, "无法识别你的身份（缺少 open_id），消息未提交。")
            return
        if is_self:
            # The bot's own messages never enter the log nor trigger (§3.2).
            return
        if self.try_handle_interaction_reply(message):
            return
        # Compose the context envelope. A history fetch failure blocks the
        # prompt with an explicit notice (fail-closed §4.5): the log entry
        # and the boundary stay, so the next trigger still sees this context.
        try:
            context_entries = self._group_history.collect_context_entries(
                chat_id=chat_id,
                current_message_id=message.message_id,
                current_create_time=message.create_time,
                current_seq=current_seq,
            )
        except Exception as exc:
            logger.warning("group history fetch failed chat=%s: %s", chat_id, exc)
            self._reply_to(message, _GROUP_HISTORY_FETCH_FAILED_TEXT)
            return
        envelope = self._group_history.build_envelope(
            text,
            sender_name=self._names.name_of(sender, sender_type=message.sender_type),
            context_entries=context_entries,
            log_path=self._group_log_store.log_path(chat_id),
        )
        result = self._handle_prompt(message, submit_text=envelope)
        if result is None or not current_seq:
            return
        # Boundary discipline: advance to the trigger point only after a
        # successful submit. The id set covers every message sharing the
        # trigger millisecond, so the REST backfill dedups exactly.
        boundary_ids = self._group_history.collect_boundary_message_ids(
            current_message_id=message.message_id,
            current_created_at=message.create_time,
            context_entries=context_entries,
        )
        self._group_log_store.set_boundary(
            chat_id,
            {
                "seq": current_seq,
                "created_at": max(int(message.create_time or 0), 0),
                "message_ids": boundary_ids,
            },
        )

    def _on_all_group_message(self, message: InboundMessage, text: str) -> None:
        """All-mode group ingress (group-chat §2): every member text message
        triggers a plain prompt — no @mention needed, no log, no context
        injection; the prompt path is the ordinary one (binding, preflight,
        ownership). The bot's own and identity-less messages never trigger
        (§3.2/§4.4), and every non-trigger cell is silent by design: a mode
        whose point is chatter cannot reply per dropped message.
        """
        sender = message.sender_open_id.strip()
        if message.sender_type == "app" or (sender and sender == self._bot_open_id()):
            # The bot's own messages never trigger (§3.2) — silently, or the
            # bot's replies would retrigger itself.
            return
        if not sender:
            # Missing identity -> non-member (§4.4); never prompts.
            return
        if not text:
            # Non-text content carries no prompt; slash commands were
            # already handled at the command gate.
            return
        if self.try_handle_interaction_reply(message):
            return
        self._handle_prompt(message)

    def _dispatch_command(self, message: InboundMessage, command: SlashCommand) -> None:
        handler = self._commands.get(command.name)
        if handler is None:
            self._reply_to(message, f"未知命令 `{command.name}`。发送 /help 查看命令导航。")
            return
        handler(message, command.arg)

    def _on_attachment_impl(self, attachment: InboundAttachment) -> None:
        if attachment.chat_type == "group":
            # Group attachments follow the ingress matrix (§3.2): only an
            # activated group processes anything, and inbound image staging
            # stays admin-only in this cut. Everything else is ignored
            # silently (no spam, fail-closed).
            if not self._group_config_store.is_activated(attachment.chat_id):
                return
            if not self._is_admin(attachment.sender_open_id):
                return
        elif not self._is_admin(attachment.sender_open_id):
            self._reply(attachment.chat_id, _NON_ADMIN_TEXT, parent_message_id=attachment.message_id)
            return
        self._attachment_domain.handle_attachment(attachment)

    def _on_merge_forward_impl(self, message: InboundMergeForward) -> None:
        if message.chat_type == "group":
            self._on_group_merge_forward(message)
            return
        if not self._is_admin(message.sender_open_id):
            self._reply(message.chat_id, _NON_ADMIN_TEXT, parent_message_id=message.message_id)
            return
        self._buffer_merge_forward(message)

    def _on_group_merge_forward(self, message: InboundMergeForward) -> None:
        """Group merge_forward dispatch (group-chat §3.7), per mode:

        - mention_only (and non-activated groups): dropped silently — a
          forward never carries an @mention, so no fetch, no buffer, no reply;
        - assistant: the flattened transcript joins the group log as context
          material, never a trigger, never a Feishu reply;
        - all: buffered through the shared aggregation window and flushed
          into the normal prompt path like a member text message.
        """
        group_config = self._group_config_store.load(message.chat_id)
        activated = group_config is not None and group_config["activated"]
        mode = group_config["mode"] if activated else GROUP_MODE_MENTION_ONLY
        if mode == GROUP_MODE_ASSISTANT:
            self._log_group_merge_forward(message)
            return
        if mode == GROUP_MODE_ALL:
            sender = message.sender_open_id.strip()
            if message.sender_type == "app" or (sender and sender == self._bot_open_id()):
                # The bot's own forwards never trigger (§3.2) — silently.
                return
            if not sender:
                # Missing identity -> non-member (§4.4); never triggers.
                return
            self._buffer_merge_forward(message)
            return
        logger.info(
            "merge_forward dropped in group chat=%s message_id=%s",
            message.chat_id,
            message.message_id,
        )

    def _log_group_merge_forward(self, message: InboundMergeForward) -> None:
        """Assistant-mode cell (§3.7): log the flattened bundle, never trigger.

        Same log entry shape as member messages (sender display name + text),
        with ``msg_type="merge_forward"`` marking it as forwarded content. A
        fetch failure or an unrenderable bundle is dropped with a log line —
        never a Feishu reply.
        """
        if self._group_log_store is None:
            # Same fail-closed as the text path: assistant mode cannot work
            # without the log axis; drop and log, never reply.
            logger.error(
                "assistant mode active but group log not wired: chat=%s",
                message.chat_id,
            )
            return
        sender = message.sender_open_id.strip()
        if message.sender_type == "app" or (sender and sender == self._bot_open_id()):
            # The bot's own forwards never enter the log (§3.2).
            return
        if not sender:
            # Missing identity -> non-member (§4.4); never enters the log.
            return
        try:
            items = self._transport.fetch_merge_forward_items(message.message_id)
        except Exception as exc:
            logger.warning(
                "merge_forward fetch failed in assistant group chat=%s message_id=%s: %s",
                message.chat_id,
                message.message_id,
                exc,
            )
            return
        text = self._forward_aggregator.render_transcript(message.message_id, items)
        if not text:
            logger.info(
                "merge_forward had no renderable content in assistant group chat=%s message_id=%s",
                message.chat_id,
                message.message_id,
            )
            return
        self._group_log_store.append(
            message.chat_id,
            {
                "message_id": message.message_id,
                "created_at": max(int(message.create_time or 0), 0),
                "sender_open_id": sender,
                "sender_type": message.sender_type.strip() or "user",
                "sender_name": self._names.name_of(
                    sender, sender_type=message.sender_type
                ),
                "msg_type": "merge_forward",
                "text": text,
            },
        )

    def _buffer_merge_forward(self, message: InboundMergeForward) -> None:
        """Fetch one bundle's children and buffer it into the aggregation
        window (shared by the p2p and all-mode group paths, §3.7)."""
        try:
            items = self._transport.fetch_merge_forward_items(message.message_id)
        except Exception as exc:
            logger.warning(
                "merge_forward fetch failed chat=%s message_id=%s: %s",
                message.chat_id,
                message.message_id,
                exc,
            )
            self._reply(
                message.chat_id,
                "获取合并转发内容失败，请稍后重试。",
                parent_message_id=message.message_id,
            )
            return
        if not items:
            self._reply(
                message.chat_id,
                "合并转发的消息中未包含可识别的内容。",
                parent_message_id=message.message_id,
            )
            return
        self._forward_aggregator.buffer(
            sender_open_id=message.sender_open_id,
            chat_id=message.chat_id,
            message_id=message.message_id,
            items=items,
            chat_type=message.chat_type,
        )

    def _on_forward_batch(self, batch: MergedForwardBatch) -> None:
        """Aggregator flush callback (timer thread): re-enter the RuntimeLoop."""
        try:
            self._loop.call(self._submit_merged_forward, batch)
        except RuntimeLoopClosedError:
            logger.info(
                "runtime loop closed, merged forward dropped: chat=%s", batch.chat_id
            )
        except Exception:
            logger.exception(
                "failed to submit merged forward chat=%s message_id=%s",
                batch.chat_id,
                batch.message_id,
            )

    def _submit_merged_forward(self, batch: MergedForwardBatch) -> None:
        """The flushed transcript enters the normal prompt path.

        p2p batches ride the p2p path (admin-gated); group batches ride the
        all-mode group path (group-chat §3.7).
        """
        if batch.chat_type == "group":
            self._submit_group_merged_forward(batch)
            return
        if not self._is_admin(batch.sender_open_id):
            # Defense in depth: the admin gate ran at ingress; the admin set
            # cannot shrink at runtime, so this never fires in practice.
            logger.info(
                "merged forward from non-admin dropped: chat=%s", batch.chat_id
            )
            return
        message = InboundMessage(
            message_id=batch.message_id,
            chat_id=batch.chat_id,
            chat_type="p2p",
            msg_type="merge_forward",
            text=batch.text,
            sender_open_id=batch.sender_open_id,
            sender_user_id="",
            sender_type="user",
            bot_mentioned=False,
            mentions=[],
            thread_id="",
            root_id="",
            parent_id="",
            create_time=0,
        )
        if self.try_handle_interaction_reply(message):
            return
        self._handle_prompt(message)

    def _submit_group_merged_forward(self, batch: MergedForwardBatch) -> None:
        """All-mode group cell (§3.7): the merged transcript triggers like a
        member text message — a plain prompt via the normal path, with
        ownership recording the forwarder so actor rules (§3.4) still work.
        """
        group_config = self._group_config_store.load(batch.chat_id)
        if (
            group_config is None
            or not group_config["activated"]
            or group_config["mode"] != GROUP_MODE_ALL
        ):
            # The group was buffered in all mode but the activation/mode
            # flipped inside the window: fail closed, never prompt on stale
            # state (mirror of the p2p admin re-check above).
            logger.info(
                "merged group forward dropped (no longer all-mode): chat=%s",
                batch.chat_id,
            )
            return
        message = InboundMessage(
            message_id=batch.message_id,
            chat_id=batch.chat_id,
            chat_type="group",
            msg_type="merge_forward",
            text=batch.text,
            sender_open_id=batch.sender_open_id,
            sender_user_id="",
            sender_type="user",
            bot_mentioned=False,
            mentions=[],
            thread_id="",
            root_id="",
            parent_id="",
            create_time=0,
        )
        if self.try_handle_interaction_reply(message):
            return
        self._handle_prompt(message)

    def _download_image_resource(
        self, message_id: str, resource_key: str
    ) -> DownloadedMessageResource:
        return self._transport.download_message_resource(
            message_id, resource_key, resource_type="image"
        )

    def _resolve_attachment_cwd(self, chat_id: str) -> str:
        """The bound session's cwd for staging; "" when the chat is unbound."""
        binding = self._binding_store.load(chat_id)
        if binding is None:
            return ""
        info = self._ops.get_session(binding["session_id"])
        return str(info.cwd or "")

    def _on_card_action_impl(self, action: CardAction) -> CardActionResponse:
        name = str(action.value.get("action") or "").strip()
        if name == ACTION_SESSION_SWITCH:
            return self._handle_session_switch_action(action)
        if name in APPROVAL_CARD_ACTIONS:
            return self.handle_approval_action(action)
        if name in QUESTION_CARD_ACTIONS:
            return self.handle_question_action(action)
        if name == cards.ACTION_PROMPT_ABORT:
            return self.handle_abort_action(action)
        logger.info("ignoring unknown card action: %r", name)
        return CardActionResponse()

    # ------------------------------------------------------------------
    # Prompt submission
    # ------------------------------------------------------------------

    def _handle_prompt(
        self, message: InboundMessage, *, submit_text: str | None = None
    ) -> Optional[SubmitPromptResult]:
        """Submit the prompt for one inbound text message.

        ``submit_text`` overrides the submitted text (assistant-mode group
        triggers submit the context envelope while the session title still
        comes from the raw message). Returns the SubmitPromptResult on a
        successful submit, None on every failure path — the assistant-mode
        trigger uses this to advance the boundary only after a real submit.
        """
        chat_id = message.chat_id
        text = message.text.strip()
        prompt_text = (
            submit_text.strip() if submit_text is not None else text
        )
        binding = self._binding_store.load(chat_id)
        created = False
        if binding is None:
            # First use: auto-create a session with the instance default cwd
            # and bind it (mvp-scope §2).
            binding = self._create_and_bind(chat_id, text)
            if binding is None:
                return None
            created = True
        if not binding["attached"]:
            # A detached chat must not silently run invisible work: refuse
            # with a pointer to /attach (fail-closed).
            self._reply_to(
                message,
                "当前会话已暂停推送（/detach 状态），消息未提交。发送 /attach 恢复后再继续。",
            )
            return None
        session_id = binding["session_id"]
        # Pre-flight: an archived (or vanished) session errors and points to
        # /sessions; KITE never auto-recreates one (mvp-scope §4.7). Upstream
        # resume() would quietly resurrect an archived session, so the check
        # must happen here, before submit.
        try:
            info = self._ops.get_session(session_id)
        except KapTransportError:
            self._reply_to(message, _KAP_UNREACHABLE_TEXT)
            return None
        except KapError as exc:
            if exc.code == KAP_ERROR_SESSION_NOT_FOUND:
                self._reply_to(
                    message,
                    f"绑定的会话 `{session_id}` 在 kap-server 上已不存在。"
                    "发送 /sessions 查看可用会话并切换；KITE 不会自动新建会话。",
                )
            else:
                self._reply_to(message, f"查询会话状态失败：{exc.msg}")
            return None
        if info.archived:
            self._reply_to(
                message,
                f"绑定的会话 `{session_id}` 已被归档。"
                "发送 /sessions 查看可用会话并切换；KITE 不会自动新建会话。",
            )
            return None
        # Pending staged images are consumed by this prompt (images contract
        # §2.3): an expired/missing/stale-cwd record blocks the prompt
        # fail-closed; otherwise the prompt carries the composed text plus
        # native base64 image parts.
        prepared = self._attachment_domain.prepare_prompt(
            sender_open_id=message.sender_open_id,
            chat_id=chat_id,
            text=prompt_text,
            cwd=info.cwd or "",
        )
        if prepared.blocking_text:
            self._reply_to(message, prepared.blocking_text)
            return None
        content: list[dict[str, Any]] = [{"type": "text", "text": prepared.text}]
        content.extend(
            {
                "type": "image",
                "source": {
                    "kind": "base64",
                    "media_type": image.media_type,
                    "data": image.data_base64,
                },
            }
            for image in prepared.images
        )
        try:
            result = self._ops.submit_prompt_content(
                session_id,
                content,
                permission_mode=binding["permission_mode"],
                plan_mode=binding["plan_mode"],
            )
        except KapTransportError:
            # Submit failed: restore the consumed records so a retry still
            # has them (contract §5.1); staged files are kept.
            self._attachment_domain.restore_consumed(prepared.consumed)
            self._reply_to(message, _KAP_UNREACHABLE_TEXT)
            return None
        except KapError as exc:
            # Business error on submit (mvp-scope §4.5): report the upstream
            # msg; no prompt started, so no card exists to transition.
            self._attachment_domain.restore_consumed(prepared.consumed)
            self._reply_to(message, f"提交失败：{exc.msg}")
            return None
        if result.status == "blocked":
            # Upstream rejects blocked submissions before a turn launches.
            self._attachment_domain.restore_consumed(prepared.consumed)
            self._reply_to(message, "提交被 kap-server 拒绝（blocked），该 prompt 未执行。")
            return None
        if prepared.consumed:
            # Successful submit: consumption deletes the staged files
            # (contract §2.5); the image bytes live in the kap message.
            self._attachment_domain.discard_consumed_files(prepared.consumed)
        self._ownership.record(
            result.prompt_id, chat_id, sender_open_id=message.sender_open_id
        )
        logger.info(
            "prompt submitted chat_id=%s session_id=%s prompt_id=%s status=%s attachments=%d",
            chat_id,
            session_id,
            result.prompt_id,
            result.status,
            len(prepared.consumed),
        )
        prefix = f"已创建并绑定新会话 `{session_id}`。\n" if created else ""
        suffix = f"（附带 {len(prepared.consumed)} 张图片）" if prepared.consumed else ""
        if result.status == "queued":
            self._reply_to(message, f"{prefix}已加入队列，等待执行。{suffix}")
        else:
            self._reply_to(message, f"{prefix}已提交，正在执行。{suffix}")
        return result

    def _create_and_bind(self, chat_id: str, text: str) -> Optional[StoredBinding]:
        try:
            info = self._ops.create_session(
                cwd=self._default_working_dir,
                title=_session_title_from_text(text),
            )
        except KapTransportError:
            self._reply(chat_id, _KAP_UNREACHABLE_TEXT)
            return None
        except KapError as exc:
            self._reply(chat_id, f"创建会话失败：{exc.msg}")
            return None
        # Reverse all-mode exclusivity (group-chat §3.8): plain first-use
        # creates a FRESH session, which no all-mode group can occupy — the
        # probe is the fail-closed assertion of that invariant (the same
        # discipline as the /new forward probe). Binding into an EXISTING
        # session only ever happens in _switch_to_session.
        occupied = preflights.all_mode_session_occupied(
            chat_id,
            self._binding_store,
            self._group_config_store,
            session_id=info.session_id,
        )
        if not occupied.allowed:
            logger.info(
                "first-use bind denied chat_id=%s session_id=%s reason_code=%s",
                chat_id,
                info.session_id,
                occupied.reason_code,
            )
            self._reply(chat_id, occupied.reason_text)
            return None
        binding: StoredBinding = {
            "session_id": info.session_id,
            "attached": DEFAULT_ATTACHED,
            "permission_mode": DEFAULT_PERMISSION_MODE,
            "plan_mode": DEFAULT_PLAN_MODE,
        }
        self._binding_store.save(chat_id, binding)
        self._notify_session_bound(info.session_id)
        logger.info(
            "first-use session created chat_id=%s session_id=%s cwd=%s",
            chat_id,
            info.session_id,
            self._default_working_dir,
        )
        return binding

    # ------------------------------------------------------------------
    # Control-plane prompt submission (kitectl -> kited)
    # ------------------------------------------------------------------

    def submit_prompt_control(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Control-plane entry for `kitectl prompt send`.

        Called on control-plane server threads; serialized onto the
        RuntimeLoop like every other state mutation. Raises ControlError
        (code/msg) for business failures, which the control plane returns as
        a structured error response.
        """
        return self._loop.call(self._submit_prompt_control_impl, params)

    def _submit_prompt_control_impl(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """The _handle_prompt submit discipline minus the Feishu surface:
        same pre-flight, same explicit modes + model, same certain ownership
        record — but no text ack (the response travels back over the control
        channel; the execution card still arrives event-driven). The optional
        ``display`` param (scheduled-prompts contract §4.2): ``announce``
        sends one short trigger notice to the target chat before submitting;
        ``silent`` (default) keeps the no-extra-message behavior.
        """
        text = str(params.get("text") or "").strip()
        chat_id = str(params.get("chat_id") or "").strip()
        session_id = str(params.get("session_id") or "").strip()
        if not text:
            raise ControlError("prompt text must not be empty", code="invalid_params")
        if bool(chat_id) == bool(session_id):
            raise ControlError(
                "exactly one of chat_id / session_id must be given", code="invalid_params"
            )
        permission_mode = params.get("permission_mode")
        if permission_mode is not None and (
            not isinstance(permission_mode, str)
            or permission_mode not in VALID_PERMISSION_MODES
        ):
            raise ControlError(
                f"permission_mode must be one of {sorted(VALID_PERMISSION_MODES)}",
                code="invalid_params",
            )
        plan_mode = params.get("plan_mode")
        if plan_mode is not None and not isinstance(plan_mode, bool):
            raise ControlError("plan_mode must be a boolean", code="invalid_params")
        display = params.get("display")
        if display is not None and display not in ("silent", "announce"):
            raise ControlError(
                "display must be one of ['announce', 'silent']", code="invalid_params"
            )
        if display == "announce" and not chat_id:
            # The trigger notice needs a target chat; a bare session has none
            # (scheduled-prompts contract §4.2).
            raise ControlError("display=announce requires chat_id", code="invalid_params")

        owner_chat_id = ""
        if chat_id:
            binding = self._binding_store.load(chat_id)
            if binding is None:
                raise ControlError(
                    f"no binding for chat {chat_id}; bind the chat from Feishu first",
                    code="no_binding",
                )
            if not binding["attached"]:
                # Mirrors the Feishu path: a detached chat must not silently
                # run work whose cards/approvals it would never see.
                raise ControlError(
                    f"chat {chat_id} is detached; send /attach in Feishu to resume first",
                    code="chat_detached",
                )
            session_id = binding["session_id"]
            # Modes are chat-level settings: the binding's values win unless
            # explicitly overridden (kite-design.md §7).
            if permission_mode is None:
                permission_mode = binding["permission_mode"]
            if plan_mode is None:
                plan_mode = binding["plan_mode"]
            owner_chat_id = chat_id
        else:
            # No binding to inherit from and no owner to record: approvals
            # from such prompts expire explicitly by design (fail-closed).
            if permission_mode is None:
                permission_mode = DEFAULT_PERMISSION_MODE
            if plan_mode is None:
                plan_mode = DEFAULT_PLAN_MODE

        try:
            info = self._ops.get_session(session_id)
        except KapTransportError as exc:
            raise ControlError(f"cannot reach kap-server: {exc}", code="kap_unreachable") from exc
        except KapError as exc:
            raise ControlError(exc.msg, code=str(exc.code)) from exc
        if info.archived:
            # KITE never auto-recreates an archived session (mvp-scope §4.7);
            # an upstream submit would quietly resurrect it, so refuse here.
            raise ControlError(
                f"session {session_id} is archived; switch sessions from Feishu first",
                code="session_archived",
            )
        if display == "announce":
            # Scheduled-prompts contract §4.2: one short trigger notice to
            # the target chat before submitting. Best-effort: a Feishu
            # hiccup must not block the scheduled prompt itself.
            self._safe_reply(owner_chat_id, _scheduled_trigger_notice(text))
        try:
            result = self._ops.submit_prompt(
                session_id,
                text,
                permission_mode=permission_mode,
                plan_mode=plan_mode,
            )
        except KapTransportError as exc:
            raise ControlError(f"cannot reach kap-server: {exc}", code="kap_unreachable") from exc
        except KapError as exc:
            raise ControlError(exc.msg, code=str(exc.code)) from exc
        if result.status == "blocked":
            # Upstream rejects blocked submissions before a turn launches.
            raise ControlError(
                "kap-server rejected the prompt as blocked; it did not run",
                code="submit_blocked",
            )
        owner_recorded = False
        if owner_chat_id:
            # The same certain-ownership record the Feishu path writes, into
            # the same map the outbound pipeline reads (single-writer
            # discipline on state axis 4, docs/decisions/control-plane.md).
            self._ownership.record(result.prompt_id, owner_chat_id)
            owner_recorded = True
        logger.info(
            "control-plane prompt submitted session_id=%s prompt_id=%s status=%s owner=%s",
            session_id,
            result.prompt_id,
            result.status,
            owner_chat_id or "<none>",
        )
        return {
            "prompt_id": result.prompt_id,
            "session_id": session_id,
            "status": result.status,
            "owner_recorded": owner_recorded,
        }

    # ------------------------------------------------------------------
    # Control-plane image delivery (kitectl -> kited)
    # ------------------------------------------------------------------

    def send_image_control(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Control-plane entry for `kitectl image send` (images contract §3).

        Uploads the local image once and sends an image message to every
        attached chat bound to the same session as ``chat_id``; a per-chat
        failure is isolated in the result, never raised. Raises ControlError
        (code/msg) for validation/upload failures.
        """
        return self._loop.call(self._send_image_control_impl, params)

    def _send_image_control_impl(self, params: Mapping[str, Any]) -> dict[str, Any]:
        chat_id = str(params.get("chat_id") or "").strip()
        raw_path = str(params.get("path") or "").strip()
        if not chat_id:
            raise ControlError("chat_id must not be empty", code="invalid_params")
        if not raw_path:
            raise ControlError("path must not be empty", code="invalid_params")
        binding = self._binding_store.load(chat_id)
        if binding is None:
            raise ControlError(
                f"no binding for chat {chat_id}; bind the chat from Feishu first",
                code="no_binding",
            )
        image_path = pathlib.Path(raw_path).expanduser()
        if not image_path.is_file():
            raise ControlError(
                f"image path does not exist or is not a file: {image_path}",
                code="invalid_path",
            )
        size = image_path.stat().st_size
        if size > self._attachment_max_bytes:
            raise ControlError(
                f"image is {size} bytes, over the {self._attachment_max_bytes} byte cap",
                code="image_too_large",
            )
        # Fan-out: every attached chat bound to the same session (contract
        # §3.1, FOCUS thread_image_delivery upload-once discipline).
        targets = sorted(
            candidate
            for candidate, candidate_binding in self._binding_store.load_all().items()
            if candidate_binding["session_id"] == binding["session_id"]
            and candidate_binding["attached"]
        )
        if not targets:
            raise ControlError(
                f"no attached chats bound to session {binding['session_id']}",
                code="no_targets",
            )
        image_key = str(self._transport.upload_image(str(image_path)) or "").strip()
        if not image_key:
            raise ControlError(f"image upload failed: {image_path}", code="upload_failed")
        delivered: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        for target_chat_id in targets:
            try:
                message_id = str(
                    self._transport.send_image_by_key(target_chat_id, image_key) or ""
                ).strip()
            except Exception as exc:  # noqa: BLE001 - per-chat failure isolation
                logger.exception("image send failed chat=%s", target_chat_id)
                failed.append({"chat_id": target_chat_id, "error": str(exc) or "send_failed"})
                continue
            if message_id:
                delivered.append({"chat_id": target_chat_id, "message_id": message_id})
            else:
                failed.append({"chat_id": target_chat_id, "error": "send_failed"})
        logger.info(
            "control-plane image sent session_id=%s image_key=%s delivered=%d failed=%d",
            binding["session_id"],
            image_key,
            len(delivered),
            len(failed),
        )
        return {
            "session_id": binding["session_id"],
            "image_key": image_key,
            "delivered": delivered,
            "failed": failed,
        }

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    def _cmd_help(self, message: InboundMessage, arg: str) -> None:
        self._reply_to(message, build_help_text())

    def _cmd_whoami(self, message: InboundMessage, arg: str) -> None:
        """/whoami: sender identity + chat/binding state (non-admin allowed)."""
        sender = message.sender_open_id.strip()
        name = self._names.name_of(sender) if sender else "unknown"
        lines = [
            f"open_id：`{sender or '-'}`",
            f"显示名：{name}",
            f"身份：{'管理员' if self._is_admin(sender) else '非管理员'}",
            f"chat：`{message.chat_id}`（{'群聊' if message.chat_type == 'group' else '单聊'}）",
        ]
        binding = self._binding_store.load(message.chat_id)
        if binding is not None:
            lines.append(f"绑定会话：`{binding['session_id']}`")
            lines.append(
                f"推送：{'开启' if binding['attached'] else '暂停'}；"
                f"权限模式：{binding['permission_mode']}；"
                f"plan 模式：{'开' if binding['plan_mode'] else '关'}"
            )
        else:
            lines.append("绑定会话：无")
        if message.chat_type == "group":
            config = self._group_config_store.load(message.chat_id)
            if config is not None and config.get("activated"):
                lines.append(f"群聊状态：已激活（{config['mode']}）")
            else:
                lines.append("群聊状态：未激活")
        self._reply_to(message, "\n".join(lines))

    def _cmd_init(self, message: InboundMessage, arg: str) -> None:
        sender = message.sender_open_id.strip()
        if not arg.strip():
            self._reply_to(message, build_usage_text("/init"))
            return
        if not sender:
            # Admin identity is the Feishu open_id; without it there is
            # nothing to register (fail-closed).
            self._reply_to(message, "无法识别你的身份（缺少 open_id），注册被拒绝。")
            return
        if not self._init_token or not hmac.compare_digest(arg.strip(), self._init_token):
            logger.warning("failed /init attempt chat=%s", message.chat_id)
            self._reply_to(message, "token 不正确。")
            return
        if sender in self._admins:
            self._reply_to(message, "你已经是管理员了。发送 /help 查看命令导航。")
            return
        # Persist first, mutate memory second: a disk failure must not leave
        # an unpersisted admin (fail-closed).
        new_admins = {*self._admins, sender}
        self._persist_admins(new_admins)
        self._admins = new_admins
        logger.info("admin registered via /init open_id=%s", sender)
        self._reply_to(message, "管理员注册成功。发送 /help 查看命令导航。")

    def _cmd_new(self, message: InboundMessage, arg: str) -> None:
        if arg.strip():
            self._reply_to(message, build_usage_text("/new"))
            return
        chat_id = message.chat_id
        current = self._binding_store.load(chat_id)
        if current is not None:
            # Preflight (fail-closed, mvp-scope aligned item 7): refuse the
            # rebind while the bound session has an active prompt — its
            # execution card, terminal result and approval routing would lose
            # visibility. Unverifiable queue state also refuses.
            try:
                queue = self._ops.get_prompts(current["session_id"])
            except KapTransportError:
                self._reply_to(message, _KAP_UNREACHABLE_TEXT)
                return
            except KapError as exc:
                self._reply_to(message, f"查询会话状态失败：{exc.msg}")
                return
            check = preflights.check_new(queue)
            if not check.allowed:
                logger.info(
                    "/new denied chat_id=%s reason_code=%s", chat_id, check.reason_code
                )
                self._reply_to(message, check.reason_text)
                return
        old_session_id = current["session_id"] if current else ""
        # Create first, rebind second: on failure the old binding is intact.
        try:
            info = self._ops.create_session(cwd=self._default_working_dir, title="")
        except KapTransportError:
            self._reply_to(message, _KAP_UNREACHABLE_TEXT)
            return
        except KapError as exc:
            self._reply_to(message, f"创建会话失败：{exc.msg}")
            return
        if message.chat_type == "group":
            # All-mode exclusivity (group-chat §2, fail-closed §4.6): a
            # freshly created session is exclusive by construction, so this
            # probe is the fail-closed assertion of that invariant — an
            # all-mode group never rebinds into a shared session.
            exclusive = preflights.all_mode_session_exclusive(
                chat_id,
                self._binding_store,
                self._group_config_store,
                session_id=info.session_id,
            )
            if not exclusive.allowed:
                logger.info(
                    "/new denied chat_id=%s reason_code=%s",
                    chat_id,
                    exclusive.reason_code,
                )
                self._reply_to(message, exclusive.reason_text)
                return
        # Permission/plan modes are chat-level settings: they carry over.
        binding: StoredBinding = {
            "session_id": info.session_id,
            "attached": DEFAULT_ATTACHED,
            "permission_mode": (
                current["permission_mode"] if current else DEFAULT_PERMISSION_MODE
            ),
            "plan_mode": current["plan_mode"] if current else DEFAULT_PLAN_MODE,
        }
        self._binding_store.save(chat_id, binding)
        self._notify_session_bound(info.session_id)
        if old_session_id and old_session_id != info.session_id:
            # Fail-close sweep of the old session's pending approvals/questions
            # routed to this chat (E3; no-op until the outbound path is wired).
            self.on_session_unbound(chat_id, old_session_id)
        logger.info("binding renewed chat_id=%s session_id=%s", chat_id, info.session_id)
        self._reply_to(
            message,
            f"已创建并绑定新会话 `{info.session_id}`；旧会话保留在 kap-server 上。",
        )

    def _cmd_sessions(self, message: InboundMessage, arg: str) -> None:
        if arg.strip():
            self._reply_to(message, build_usage_text("/sessions"))
            return
        try:
            # Most recent activity first (mvp-scope aligned item 3), by the
            # upstream updated_at the adapter now normalizes.
            sessions = sorted(
                (s for s in self._ops.list_sessions() if not s.archived),
                key=lambda s: s.updated_at,
                reverse=True,
            )
        except KapTransportError:
            self._reply_to(message, _KAP_UNREACHABLE_TEXT)
            return
        except KapError as exc:
            self._reply_to(message, f"获取会话列表失败：{exc.msg}")
            return
        if not sessions:
            self._reply_to(message, "kap-server 上当前没有可用会话。")
            return
        binding = self._binding_store.load(message.chat_id)
        bound_id = binding["session_id"] if binding else ""
        card = _build_sessions_card(sessions, bound_id=bound_id)
        self._transport.reply_card(
            message.chat_id, card, parent_message_id=message.message_id
        )

    def _cmd_switch(self, message: InboundMessage, arg: str) -> None:
        parts = arg.split()
        if not parts:
            self._reply_to(message, build_usage_text("/switch"))
            return
        _ok, text = self._switch_to_session(message.chat_id, parts[0])
        self._reply_to(message, text)

    def _cmd_detach(self, message: InboundMessage, arg: str) -> None:
        if arg.strip():
            self._reply_to(message, build_usage_text("/detach"))
            return
        binding = self._load_binding_or_reply(message)
        if binding is None:
            return
        if not binding["attached"]:
            self._reply_to(message, "当前会话已是暂停推送状态。")
            return
        # Preflight note: /detach only flips a local push flag, so it is never
        # denied — but an active prompt keeps running upstream unseen, which
        # the reply must say. The note is best-effort: detach stays available
        # when kap is unreachable (that is exactly when users want it).
        note = ""
        try:
            note = preflights.check_detach(
                self._ops.get_prompts(binding["session_id"])
            ).note
        except (KapError, KapTransportError) as exc:
            logger.debug("detach preflight skipped chat=%s: %s", message.chat_id, exc)
        binding["attached"] = False
        self._binding_store.save(message.chat_id, binding)
        text = "已暂停当前会话的飞书推送；绑定保留。发送 /attach 恢复。"
        if note:
            text += f"\n{note}"
        self._reply_to(message, text)

    def _cmd_attach(self, message: InboundMessage, arg: str) -> None:
        if arg.strip():
            self._reply_to(message, build_usage_text("/attach"))
            return
        binding = self._load_binding_or_reply(message)
        if binding is None:
            return
        if binding["attached"]:
            self._reply_to(message, "当前会话已在接收推送。")
            return
        binding["attached"] = True
        self._binding_store.save(message.chat_id, binding)
        self._reply_to(message, "已恢复当前会话的飞书推送。")

    def _cmd_mode(self, message: InboundMessage, arg: str) -> None:
        binding = self._load_binding_or_reply(message)
        if binding is None:
            return
        if not arg.strip():
            self._reply_to(
                message,
                f"当前权限模式：{binding['permission_mode']}。可选：auto / manual / yolo。",
            )
            return
        mode = parse_permission_mode_arg(arg)
        if mode is None:
            self._reply_to(message, build_usage_text("/mode"))
            return
        if mode == binding["permission_mode"]:
            self._reply_to(message, f"权限模式已是 {mode}。")
            return
        if mode == PERMISSION_MODE_YOLO and not self._is_admin(message.sender_open_id):
            # Defense in depth: the identity gate already limits commands to
            # admins, but yolo is the one mode the contract calls out as
            # admin-only (mvp-scope §5).
            self._reply_to(message, "yolo 模式需要管理员开启。")
            return
        binding["permission_mode"] = mode
        self._binding_store.save(message.chat_id, binding)
        logger.info(
            "permission mode set chat_id=%s mode=%s operator=%s",
            message.chat_id,
            mode,
            message.sender_open_id,
        )
        if mode == PERMISSION_MODE_YOLO:
            self._reply_to(
                message,
                "已开启 yolo 模式：本聊天的后续操作将自动批准，不再逐条确认。请谨慎使用。",
            )
        else:
            self._reply_to(message, f"权限模式已切换为 {mode}。")

    def _cmd_plan(self, message: InboundMessage, arg: str) -> None:
        binding = self._load_binding_or_reply(message)
        if binding is None:
            return
        if not arg.strip():
            new_plan_mode = not binding["plan_mode"]
        else:
            parsed = parse_plan_mode_arg(arg)
            if parsed is None:
                self._reply_to(message, build_usage_text("/plan"))
                return
            new_plan_mode = parsed
        binding["plan_mode"] = new_plan_mode
        self._binding_store.save(message.chat_id, binding)
        state = "开启" if new_plan_mode else "关闭"
        self._reply_to(message, f"计划模式已{state}。")

    def _cmd_group(self, message: InboundMessage, arg: str) -> None:
        if message.chat_type != "group":
            self._reply_to(message, "`/group` 仅在群聊中可用。")
            return
        if not self._is_admin(message.sender_open_id):
            # Defense in depth: the group ingress gate already limits slash
            # commands to admins, but activation is the group access switch
            # itself and stays explicitly admin-only (contract §3.1).
            self._reply_to(message, _GROUP_COMMAND_ADMIN_ONLY_TEXT)
            return
        subcommand = arg.strip().lower()
        if subcommand == "activate":
            # Activation requires a binding (contract §3.1): an unbound
            # group's first activation creates+binds a session with the
            # instance default cwd, same first-use rule as p2p.
            if self._binding_store.load(message.chat_id) is None:
                if self._create_and_bind(message.chat_id, "群聊会话") is None:
                    return
            config = self._group_config_store.activate(
                message.chat_id, activated_by=message.sender_open_id
            )
            logger.info(
                "group activated chat_id=%s by=%s mode=%s",
                message.chat_id,
                config["activated_by"],
                config["mode"],
            )
            self._reply_to(
                message,
                f"已激活当前群聊（模式：{config['mode']}）。群成员 @我 并发送文字即可提交 prompt；"
                "审批与问题仅发起者或管理员可处理。发送 /group deactivate 停用。",
            )
            return
        if subcommand == "deactivate":
            self._group_config_store.deactivate(message.chat_id)
            logger.info("group deactivated chat_id=%s", message.chat_id)
            self._reply_to(
                message,
                "已停用当前群聊；成员消息将被忽略，群聊命令仍仅管理员可用。"
                "发送 /group activate 重新激活。",
            )
            return
        self._reply_to(message, build_usage_text("/group"))

    def _cmd_group_mode(self, message: InboundMessage, arg: str) -> None:
        """/group-mode 〈mention_only|assistant|all〉: switch the group mode."""
        if message.chat_type != "group":
            self._reply_to(message, "`/group-mode` 仅在群聊中可用。")
            return
        if not self._is_admin(message.sender_open_id):
            # Defense in depth (same convention as /group): the group ingress
            # gate already limits slash commands to admins, but the mode
            # switch changes what member messages do and stays explicitly
            # admin-only (contract §2).
            self._reply_to(message, _GROUP_COMMAND_ADMIN_ONLY_TEXT)
            return
        group_config = self._group_config_store.load(message.chat_id)
        if group_config is None or not group_config["activated"]:
            # Mode switching only makes sense on an activated group; a
            # corrupt record reads as non-activated here too (§4.3).
            self._reply_to(message, "本群尚未激活，请先发送 /group activate 激活。")
            return
        mode = arg.strip().lower()
        if not mode:
            self._reply_to(
                message,
                f"当前群聊模式：{group_config['mode']}。可选：mention_only / assistant / all。",
            )
            return
        if mode not in VALID_GROUP_MODES:
            self._reply_to(message, build_usage_text("/group-mode"))
            return
        if mode == group_config["mode"]:
            self._reply_to(message, f"群聊模式已是 {mode}。")
            return
        if mode == GROUP_MODE_ALL:
            # Exclusivity (contract §2, fail-closed §4.6): an all-mode
            # group's session may not be bound to any other attached chat;
            # deny with the remediation text, never switch silently.
            exclusive = preflights.all_mode_session_exclusive(
                message.chat_id,
                self._binding_store,
                self._group_config_store,
                current_chat_mode=GROUP_MODE_ALL,
            )
            if not exclusive.allowed:
                logger.info(
                    "/group-mode all denied chat_id=%s reason_code=%s",
                    message.chat_id,
                    exclusive.reason_code,
                )
                self._reply_to(message, exclusive.reason_text)
                return
        self._group_config_store.set_mode(message.chat_id, mode)
        logger.info(
            "group mode set chat_id=%s mode=%s operator=%s",
            message.chat_id,
            mode,
            message.sender_open_id,
        )
        if mode == GROUP_MODE_ALL:
            self._reply_to(
                message,
                "已切换为 all 模式：群成员的每条文字消息都会直接提交 prompt"
                "（不携带群聊上下文）；该模式下本群独占当前会话。"
                "发送 /group-mode mention_only 切回。",
            )
        elif mode == GROUP_MODE_ASSISTANT:
            self._reply_to(
                message,
                "已切换为 assistant 模式：群成员的文字消息会记录到群聊日志；"
                "@我 时会携带自上次触发以来的群聊上下文。"
                "发送 /group-mode mention_only 切回。",
            )
        else:
            self._reply_to(
                message,
                "已切换为 mention_only 模式：仅 @我 的文字会触发，其他消息不再记录。",
            )

    def _cmd_status(self, message: InboundMessage, arg: str) -> None:
        if arg.strip():
            self._reply_to(message, build_usage_text("/status"))
            return
        binding = self._load_binding_or_reply(message)
        if binding is None:
            return
        lines = [
            f"绑定会话：`{binding['session_id']}`",
            f"推送：{'已开启' if binding['attached'] else '已暂停（发送 /attach 恢复）'}",
            f"权限模式：{binding['permission_mode']}；"
            f"计划模式：{'开启' if binding['plan_mode'] else '关闭'}",
        ]
        if message.chat_type == "group":
            # Group activation state (group-chat contract §3.1).
            group_config = self._group_config_store.load(message.chat_id)
            if group_config is not None and group_config["activated"]:
                lines.append(f"群聊：已激活（{group_config['mode']}）")
            else:
                lines.append("群聊：未激活（发送 /group activate 激活）")
        try:
            info = self._ops.get_session(binding["session_id"])
            queue = self._ops.get_prompts(binding["session_id"])
        except KapTransportError:
            lines.append("⚠️ 无法连接 kap-server，会话工作状态未知。")
        except KapError as exc:
            lines.append(f"⚠️ 查询会话状态失败：{exc.msg}")
        else:
            state = "忙碌" if info.busy else "空闲"
            if info.archived:
                state += "；已归档"
            lines.append(f"会话：{info.title or '（无标题）'}（{state}）")
            lines.append(f"待处理交互：{info.pending_interaction or '无'}")
            active = "1 条执行中" if queue.active_prompt_id else "无执行中 prompt"
            lines.append(f"队列：{active}，排队 {queue.queue_depth} 条")
        self._reply_to(message, "\n".join(lines))

    def _cmd_last(self, message: InboundMessage, arg: str) -> None:
        """/last: reply with the bound session's latest terminal result text.

        Primary source is the local terminal result store (design §6:
        terminal text is persisted for /last-style reads); no upstream call,
        no state axis. When the store has no record for the session (store
        lost/wiped), the fallback re-reads the newest verifiable terminal
        card from the Feishu chat history and projects its text back
        (kite/card_text_projection.py marker contract); kap REST is not
        involved in the fallback.
        """
        if arg.strip():
            self._reply_to(message, build_usage_text("/last"))
            return
        binding = self._load_binding_or_reply(message)
        if binding is None:
            return
        if self._terminal_store is None:
            self._reply_to(message, "终态记录不可用。")
            return
        text = self._terminal_store.latest_for_session(binding["session_id"])
        if not text:
            text = self._last_text_from_history(message.chat_id)
            if text is None:
                self._reply_to(message, _LAST_HISTORY_FETCH_FAILED_TEXT)
                return
        if not text:
            self._reply_to(message, "该会话暂无终态答复记录。")
            return
        if len(text) > _LAST_TEXT_CAP:
            text = text[:_LAST_TEXT_CAP] + "\n\n（内容过长，已截断）"
        self._reply_to(message, text)

    def _last_text_from_history(self, chat_id: str) -> str | None:
        """Project the newest checksum-verified terminal card from chat history.

        Returns the projected terminal text, "" when no verifiable terminal
        card is found in the scanned window, or None when the history fetch
        itself failed (the caller then answers with an explicit error notice
        instead of the no-record one). Only checksum-verified projections
        are exported (fail-closed): marker-only legacy cards and cards whose
        text no longer matches the stamped element id are skipped, so a
        forged marker can never poison /last.
        """
        list_messages = getattr(self._transport, "list_messages", None)
        if not callable(list_messages):
            return ""
        try:
            page = list_messages(
                chat_id,
                sort_type="ByCreateTimeDesc",
                page_size=_LAST_HISTORY_SCAN_LIMIT,
                card_msg_content_type="user_card_content",
            )
        except Exception:
            logger.warning(
                "/last history fallback fetch failed chat=%s", chat_id, exc_info=True
            )
            return None
        app_id = str(getattr(self._transport, "app_id", "") or "").strip()
        for item in list(getattr(page, "items", None) or []):
            if str(getattr(item, "msg_type", "") or "").strip() != "interactive":
                continue
            if app_id:
                sender = getattr(item, "sender", None)
                sender_type = str(getattr(sender, "sender_type", "") or "").strip()
                sender_id = str(getattr(sender, "id", "") or "").strip()
                # Only this bot's own cards are candidates (other apps'
                # cards are not KITE terminal cards).
                if sender_type != "app" or sender_id != app_id:
                    continue
            body = getattr(item, "body", None)
            raw_content = str(getattr(body, "content", "") or "").strip()
            if not raw_content:
                continue
            try:
                content_dict = json.loads(raw_content)
            except Exception:
                continue
            if not isinstance(content_dict, dict):
                continue
            projection = project_interactive_card_text(content_dict)
            if not projection.final_reply_text:
                continue
            if not verify_terminal_result_checksum(projection):
                continue
            return projection.final_reply_text
        return ""

    def _cmd_abort(self, message: InboundMessage, arg: str) -> None:
        if arg.strip():
            self._reply_to(message, build_usage_text("/abort"))
            return
        binding = self._load_binding_or_reply(message)
        if binding is None:
            return
        try:
            queue = self._ops.get_prompts(binding["session_id"])
        except KapTransportError:
            self._reply_to(message, _KAP_UNREACHABLE_TEXT)
            return
        except KapError as exc:
            self._reply_to(message, f"查询队列失败：{exc.msg}")
            return
        active_id = queue.active_prompt_id
        if not active_id:
            self._reply_to(message, "当前没有正在执行的 prompt。")
            return
        # /abort is gated to the active prompt's initiator and admins
        # (mvp-scope §3; group-chat contract §3.4). In p2p the initiating
        # chat's user counts as the initiator (single-user chat); in a group
        # the initiator is the recorded sender_open_id.
        if not self._may_abort(message, active_id):
            self._reply_to(message, _ABORT_DENIED_TEXT)
            return
        try:
            self._ops.abort_prompt(binding["session_id"], active_id)
        except KapError as exc:
            if exc.code == KAP_ERROR_PROMPT_NOT_PENDING:
                # Re-abort of a finished prompt (spike S2): report, do not
                # treat as failure.
                self._reply_to(message, "该 prompt 已结束，无需中止。")
            else:
                self._reply_to(message, f"中止失败：{exc.msg}")
            return
        except KapTransportError:
            self._reply_to(message, _KAP_UNREACHABLE_TEXT)
            return
        logger.info(
            "prompt aborted chat_id=%s session_id=%s prompt_id=%s operator=%s",
            message.chat_id,
            binding["session_id"],
            active_id,
            message.sender_open_id,
        )
        self._reply_to(message, "已中止当前 prompt。")

    # ------------------------------------------------------------------
    # Card actions
    # ------------------------------------------------------------------

    def _handle_session_switch_action(self, action: CardAction) -> CardActionResponse:
        if not self._is_admin(action.operator_open_id):
            return CardActionResponse(toast="仅管理员可以切换会话。", toast_type="error")
        session_id = str(action.value.get("session_id") or "").strip()
        if not session_id:
            logger.warning("session_switch action without session_id: %s", action.value)
            return CardActionResponse(toast="该按钮已失效。", toast_type="error")
        ok, text = self._switch_to_session(action.chat_id, session_id)
        return CardActionResponse(toast=text, toast_type="info" if ok else "error")

    def _switch_to_session(self, chat_id: str, session_id: str) -> tuple[bool, str]:
        """Rebind a chat to an existing session (auto-attached). Shared by
        /switch and the /sessions card buttons."""
        current = self._binding_store.load(chat_id)
        if current is not None and current["session_id"] == session_id:
            if current["attached"]:
                return True, f"当前已绑定该会话（`{session_id}`）。"
            current["attached"] = True
            self._binding_store.save(chat_id, current)
            return True, "当前已绑定该会话，推送已恢复。"
        old_session_id = current["session_id"] if current else ""
        try:
            info = self._ops.get_session(session_id)
        except KapTransportError:
            return False, _KAP_UNREACHABLE_TEXT
        except KapError as exc:
            if exc.code == KAP_ERROR_SESSION_NOT_FOUND:
                return (
                    False,
                    f"kap-server 上不存在会话 `{session_id}`。发送 /sessions 查看可用会话。",
                )
            return False, f"查询会话失败：{exc.msg}"
        if info.archived:
            # Never rebind to an archived session (fail-closed, §4.7 spirit).
            return (
                False,
                f"会话 `{session_id}` 已归档，不能切换。发送 /sessions 查看可用会话。",
            )
        # All-mode exclusivity (group-chat §2, fail-closed §4.6): an
        # all-mode group may only rebind to a session no other attached
        # chat is bound to. A no-op for every non-all-mode chat.
        exclusive = preflights.all_mode_session_exclusive(
            chat_id,
            self._binding_store,
            self._group_config_store,
            session_id=session_id,
        )
        if not exclusive.allowed:
            logger.info(
                "session switch denied chat_id=%s session_id=%s reason_code=%s",
                chat_id,
                session_id,
                exclusive.reason_code,
            )
            return False, exclusive.reason_text
        # Reverse exclusivity (group-chat §3.8): no chat — p2p or group —
        # may rebind into a session a DIFFERENT chat already occupies as an
        # attached all-mode group. The requester's own mode is irrelevant.
        occupied = preflights.all_mode_session_occupied(
            chat_id,
            self._binding_store,
            self._group_config_store,
            session_id=session_id,
        )
        if not occupied.allowed:
            logger.info(
                "session switch denied chat_id=%s session_id=%s reason_code=%s",
                chat_id,
                session_id,
                occupied.reason_code,
            )
            return False, occupied.reason_text
        binding: StoredBinding = {
            "session_id": session_id,
            "attached": True,
            "permission_mode": (
                current["permission_mode"] if current else DEFAULT_PERMISSION_MODE
            ),
            "plan_mode": current["plan_mode"] if current else DEFAULT_PLAN_MODE,
        }
        self._binding_store.save(chat_id, binding)
        self._notify_session_bound(session_id)
        if old_session_id:
            # Fail-close sweep of the old session's pending approvals/questions
            # routed to this chat (E3; no-op until the outbound path is wired).
            self.on_session_unbound(chat_id, old_session_id)
        logger.info("binding switched chat_id=%s session_id=%s", chat_id, session_id)
        return True, f"已切换到会话 {info.title or '（无标题）'}（`{session_id}`），推送已开启。"

    # ------------------------------------------------------------------
    # E3 seams (the outbound path fills these in; defaults are deliberate
    # no-ops so the inbound path is complete and testable on its own)
    # ------------------------------------------------------------------

    def handle_approval_action(self, action: CardAction) -> CardActionResponse:
        """E3 seam: approval card buttons (approve / reject / reject-with-feedback).

        Default is a no-op-with-log: until the outbound approval lifecycle is
        wired, clicking an approval button changes nothing and is logged.
        """
        logger.info(
            "approval card action ignored (outbound path not wired): action=%s approval_id=%s prompt_id=%s",
            action.value.get("action"),
            action.value.get("approval_id"),
            action.value.get("prompt_id"),
        )
        return CardActionResponse()

    def handle_question_action(self, action: CardAction) -> CardActionResponse:
        """E3 seam: question option buttons. Default is a no-op-with-log."""
        logger.info(
            "question card action ignored (outbound path not wired): action=%s value=%s",
            action.value.get("action"),
            action.value,
        )
        return CardActionResponse()

    def handle_abort_action(self, action: CardAction) -> CardActionResponse:
        """E3 seam: execution-card cancel button. Default is a no-op-with-log."""
        logger.info(
            "abort card action ignored (outbound path not wired): value=%s",
            action.value,
        )
        return CardActionResponse()

    def try_handle_interaction_reply(self, message: InboundMessage) -> bool:
        """E3 seam: first claim on plain text that may answer a pending
        interaction (question numbered reply, approval feedback text).

        Returns True when the text was consumed as an interaction reply.
        Default consumes nothing, so all plain text becomes a prompt.
        """
        return False

    def on_session_unbound(self, chat_id: str, old_session_id: str) -> None:
        """E3 seam: the chat rebound away from ``old_session_id`` (/new,
        /switch); the outbound path fail-close sweeps that session's pending
        approvals/questions routed to this chat. Default is a no-op-with-log.
        """
        logger.info(
            "session unbound (outbound path not wired): chat=%s session=%s",
            chat_id,
            old_session_id,
        )

    def rebuild_prompt_ownership(self) -> None:
        """Best-effort ownership rebuild after a restart (mvp-scope §4.6).

        Attributes every bound session's active/queued prompts to the chat
        bound to that session, marked best-effort. Sessions whose queue
        cannot be read are skipped (a warning is logged) — never guessed.
        """
        entries: list[PromptOwnershipEntry] = []
        for chat_id, binding in self._binding_store.load_all().items():
            try:
                queue = self._ops.get_prompts(binding["session_id"])
            except (KapError, KapTransportError) as exc:
                logger.warning(
                    "ownership rebuild: prompts unavailable session=%s: %s",
                    binding["session_id"],
                    exc,
                )
                continue
            prompt_ids = (
                [queue.active_prompt_id] if queue.active_prompt_id else []
            ) + list(queue.queued_prompt_ids)
            entries.extend(
                PromptOwnershipEntry(
                    prompt_id=prompt_id,
                    chat_id=chat_id,
                    certainty=CERTAINTY_BEST_EFFORT,
                )
                for prompt_id in prompt_ids
            )
        self._ownership.rebuild(entries)
        logger.info("prompt ownership rebuilt best-effort: %d entries", len(entries))

    def close(self) -> None:
        """kited shutdown: cancel pending forward-aggregation timers.

        Buffered-but-unflushed forwards are dropped (fail-closed): a prompt
        submitted mid-shutdown could not deliver its cards anyway.
        """
        self._forward_aggregator.close()

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _is_admin(self, open_id: str) -> bool:
        normalized = str(open_id or "").strip()
        return bool(normalized) and normalized in self._admins

    def _bot_open_id(self) -> str:
        """The bot's own open_id, read live (discovered after construction)."""
        return str(getattr(self._transport, "bot_open_id", "") or "").strip()

    def _may_abort(self, message: InboundMessage, prompt_id: str) -> bool:
        """The /abort actor rule: initiator or admin (group-chat §3.4).

        p2p is unchanged: the initiating chat is a single-user chat, so an
        abort from the owner chat counts as the initiator. In a group the
        initiator is the recorded ``sender_open_id``; an unknown initiator
        (e.g. after a restart rebuild) fails closed to admin-only.
        """
        if self._is_admin(message.sender_open_id):
            return True
        owner_chat = self._ownership.owner_of(prompt_id)
        if owner_chat != message.chat_id:
            return False
        if message.chat_type != "group":
            return True
        entry = self._ownership.entry_of(prompt_id)
        initiator = entry.sender_open_id if entry is not None else ""
        sender = message.sender_open_id.strip()
        return bool(initiator) and bool(sender) and sender == initiator

    def _load_binding_or_reply(self, message: InboundMessage) -> Optional[StoredBinding]:
        binding = self._binding_store.load(message.chat_id)
        if binding is None:
            self._reply_to(message, _NOT_BOUND_TEXT)
        return binding

    def _notify_session_bound(self, session_id: str) -> None:
        """Live-subscribe hook for kited/E3 wiring (WS subscribe for bindings
        created at runtime). Best-effort: a hook failure must not break the
        command; kited also subscribes persisted bindings at startup."""
        if self._on_session_bound is None:
            return
        try:
            self._on_session_bound(session_id)
        except Exception:
            logger.exception("on_session_bound hook failed session=%s", session_id)

    def _reply(self, chat_id: str, text: str, *, parent_message_id: str = "") -> None:
        self._transport.reply(chat_id, text, parent_message_id=parent_message_id)

    def _reply_to(self, message: InboundMessage, text: str) -> None:
        self._reply(message.chat_id, text, parent_message_id=message.message_id)

    def _safe_reply(self, chat_id: str, text: str, *, parent_message_id: str = "") -> None:
        try:
            self._transport.reply(chat_id, text, parent_message_id=parent_message_id)
        except Exception:
            logger.exception("failed to send reply chat=%s", chat_id)


def _session_title_from_text(text: str) -> str:
    """First-use session title: the first line of the message, truncated."""
    first_line = str(text or "").splitlines()[0].strip()
    if len(first_line) > _SESSION_TITLE_MAX:
        return first_line[: _SESSION_TITLE_MAX - 1] + "…"
    return first_line


_SCHEDULED_TRIGGER_SNIPPET_MAX = 50


def _scheduled_trigger_notice(text: str) -> str:
    """The announce-mode trigger notice (scheduled-prompts contract §4.2):
    one short line with a whitespace-collapsed snippet of the fired prompt."""
    snippet = " ".join(str(text).split())
    if len(snippet) > _SCHEDULED_TRIGGER_SNIPPET_MAX:
        snippet = snippet[: _SCHEDULED_TRIGGER_SNIPPET_MAX - 1] + "…"
    return f"⏰ 定时任务触发：{snippet}"


def _build_sessions_card(sessions: list[SessionSummary], *, bound_id: str) -> dict:
    """The /sessions card: one page of sessions + switch buttons."""
    shown = sessions[:_SESSIONS_LIST_CAP]
    lines: list[str] = []
    for index, session in enumerate(shown, start=1):
        marker = "（当前绑定）" if session.session_id == bound_id else ""
        state = "忙碌" if session.busy else "空闲"
        if session.pending_interaction:
            state += f"；待处理：{session.pending_interaction}"
        title = session.title or "（无标题）"
        lines.append(f"{index}. {title}{marker}\n`{session.session_id}` — {state}")
    if len(sessions) > len(shown):
        lines.append(f"（仅显示前 {len(shown)} 个，共 {len(sessions)} 个）")
    if len(shown) > _SESSIONS_BUTTON_CAP:
        lines.append("更多会话请发送 /switch 〈id〉 切换。")

    buttons = []
    for session in shown[:_SESSIONS_BUTTON_CAP]:
        label = session.title or session.session_id
        if len(label) > 14:
            label = label[:13] + "…"
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"切换到 {label}"},
                "type": "default",
                "value": {
                    "action": ACTION_SESSION_SWITCH,
                    "session_id": session.session_id,
                },
            }
        )

    elements: list[dict] = [
        {
            "tag": "markdown",
            "content": sanitize_runtime_markdown_for_feishu_card("\n".join(lines)),
        },
    ]
    if buttons:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": buttons})

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "kap-server 会话列表"},
            "template": "blue",
        },
        "elements": elements,
    }
