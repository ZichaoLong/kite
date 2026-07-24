"""Application layer inbound path: command routing + TransportHandler impl.

Implements the MVP inbound contract (docs/contracts/mvp-scope.md):

- identity: /init <token> registers admins (first-admin bootstrap); a
  non-admin gets /help and /init only, everything else is politely rejected
  (§5);
- plain text -> resolve the chat's binding (first use creates a session with
  cwd=default_working_dir and binds it) -> submit the prompt carrying the
  binding's permission_mode + plan_mode explicitly -> record prompt
  ownership -> minimal ack;
- the loopback control plane's prompt/submit endpoint reuses that same
  submit discipline for `kitectl prompt send` (minus the Feishu ack), so
  CLI-sent prompts record ownership exactly like Feishu-originated ones
  (docs/decisions/control-plane.md);
- the MVP slash commands (/new /sessions /switch /detach /attach /mode
  /plan /status /abort /help /init); in-flight-work-sensitive commands run
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
import logging
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
from kite.command_surface import (
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
    FeishuTransport,
    InboundAttachment,
    InboundMessage,
    TransportHandler,
)
from kite.prompt_ownership import (
    CERTAINTY_BEST_EFFORT,
    PromptOwnership,
    PromptOwnershipEntry,
)
from kite.runtime_loop import RuntimeLoop
from kite.stores.binding_store import (
    DEFAULT_ATTACHED,
    DEFAULT_PERMISSION_MODE,
    DEFAULT_PLAN_MODE,
    PERMISSION_MODE_YOLO,
    VALID_PERMISSION_MODES,
    BindingStore,
    StoredBinding,
)

logger = logging.getLogger("kite.app")

# kap business error codes this path depends on (upstream
# packages/kap-server/src/protocol/error-codes.ts; spike S2 observed the
# 40402 re-abort behavior).
KAP_ERROR_SESSION_NOT_FOUND = 40401
KAP_ERROR_PROMPT_NOT_PENDING = 40402

# Card-action names owned by this module (the /sessions switch buttons).
ACTION_SESSION_SWITCH = "session_switch"

# E3 seam: card-action names owned by the outbound path. The approval names
# are defined next to the approval card builders in kite/cards.py; the
# question option-button name is defined here ahead of the rich question
# card (the MVP question surface is text, kite-design.md §6).
ACTION_QUESTION_ANSWER = "question_answer"
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
        # permission_mode / plan_mode / model are carried explicitly on every
        # prompt (kite-design.md §7; spike-results §0 for the model part).
        payload: dict[str, Any] = {
            "content": [{"type": "text", "text": text}],
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
        runtime_loop: RuntimeLoop,
        config: Mapping[str, Any],
        init_token: str,
        prompt_model: str | None = None,
        prompt_ownership: Optional[PromptOwnership] = None,
        on_session_bound: Optional[Callable[[str], None]] = None,
        persist_admins: Optional[Callable[[set[str]], None]] = None,
    ) -> None:
        self._transport = transport
        self._ops = KapSessionOps(rest, model=prompt_model)
        self._binding_store = binding_store
        self._loop = runtime_loop
        self._default_working_dir = kite_config.default_working_dir(config)
        self._admins: set[str] = kite_config.admin_open_ids(config)
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
            "/status": self._cmd_status,
            "/abort": self._cmd_abort,
            "/help": self._cmd_help,
            "/init": self._cmd_init,
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
        if not self._is_admin(message.sender_open_id):
            # Identity gate (mvp-scope §5): non-admins get /help and /init only.
            if command is not None and command.name == "/help":
                self._reply_to(message, build_help_text())
            elif command is not None and command.name == "/init":
                self._cmd_init(message, command.arg)
            else:
                self._reply_to(message, _NON_ADMIN_TEXT)
            return
        if command is not None:
            handler = self._commands.get(command.name)
            if handler is None:
                self._reply_to(message, f"未知命令 `{command.name}`。发送 /help 查看命令导航。")
                return
            handler(message, command.arg)
            return
        if not text:
            self._reply_to(message, "KITE 目前只处理文字消息。发送 /help 查看命令导航。")
            return
        # Plain text: E3's pending interactions (question numbered replies,
        # approval feedback text) get first claim; the rest becomes a prompt.
        if self.try_handle_interaction_reply(message):
            return
        self._handle_prompt(message)

    def _on_attachment_impl(self, attachment: InboundAttachment) -> None:
        if not self._is_admin(attachment.sender_open_id):
            self._reply(attachment.chat_id, _NON_ADMIN_TEXT, parent_message_id=attachment.message_id)
            return
        self._reply(
            attachment.chat_id,
            "KITE 暂不支持图片、文件等附件消息，请直接发送文字。",
            parent_message_id=attachment.message_id,
        )

    def _on_card_action_impl(self, action: CardAction) -> CardActionResponse:
        name = str(action.value.get("action") or "").strip()
        if name == ACTION_SESSION_SWITCH:
            return self._handle_session_switch_action(action)
        if name in APPROVAL_CARD_ACTIONS:
            return self.handle_approval_action(action)
        if name in QUESTION_CARD_ACTIONS:
            return self.handle_question_action(action)
        logger.info("ignoring unknown card action: %r", name)
        return CardActionResponse()

    # ------------------------------------------------------------------
    # Prompt submission
    # ------------------------------------------------------------------

    def _handle_prompt(self, message: InboundMessage) -> None:
        chat_id = message.chat_id
        text = message.text.strip()
        binding = self._binding_store.load(chat_id)
        created = False
        if binding is None:
            # First use: auto-create a session with the instance default cwd
            # and bind it (mvp-scope §2).
            binding = self._create_and_bind(chat_id, text)
            if binding is None:
                return
            created = True
        if not binding["attached"]:
            # A detached chat must not silently run invisible work: refuse
            # with a pointer to /attach (fail-closed).
            self._reply_to(
                message,
                "当前会话已暂停推送（/detach 状态），消息未提交。发送 /attach 恢复后再继续。",
            )
            return
        session_id = binding["session_id"]
        # Pre-flight: an archived (or vanished) session errors and points to
        # /sessions; KITE never auto-recreates one (mvp-scope §4.7). Upstream
        # resume() would quietly resurrect an archived session, so the check
        # must happen here, before submit.
        try:
            info = self._ops.get_session(session_id)
        except KapTransportError:
            self._reply_to(message, _KAP_UNREACHABLE_TEXT)
            return
        except KapError as exc:
            if exc.code == KAP_ERROR_SESSION_NOT_FOUND:
                self._reply_to(
                    message,
                    f"绑定的会话 `{session_id}` 在 kap-server 上已不存在。"
                    "发送 /sessions 查看可用会话并切换；KITE 不会自动新建会话。",
                )
            else:
                self._reply_to(message, f"查询会话状态失败：{exc.msg}")
            return
        if info.archived:
            self._reply_to(
                message,
                f"绑定的会话 `{session_id}` 已被归档。"
                "发送 /sessions 查看可用会话并切换；KITE 不会自动新建会话。",
            )
            return
        try:
            result = self._ops.submit_prompt(
                session_id,
                text,
                permission_mode=binding["permission_mode"],
                plan_mode=binding["plan_mode"],
            )
        except KapTransportError:
            self._reply_to(message, _KAP_UNREACHABLE_TEXT)
            return
        except KapError as exc:
            # Business error on submit (mvp-scope §4.5): report the upstream
            # msg; no prompt started, so no card exists to transition.
            self._reply_to(message, f"提交失败：{exc.msg}")
            return
        if result.status == "blocked":
            # Upstream rejects blocked submissions before a turn launches.
            self._reply_to(message, "提交被 kap-server 拒绝（blocked），该 prompt 未执行。")
            return
        self._ownership.record(result.prompt_id, chat_id)
        logger.info(
            "prompt submitted chat_id=%s session_id=%s prompt_id=%s status=%s",
            chat_id,
            session_id,
            result.prompt_id,
            result.status,
        )
        prefix = f"已创建并绑定新会话 `{session_id}`。\n" if created else ""
        if result.status == "queued":
            self._reply_to(message, f"{prefix}已加入队列，等待执行。")
        else:
            self._reply_to(message, f"{prefix}已提交，正在执行。")

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
        channel; the execution card still arrives event-driven).
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
    # Slash commands
    # ------------------------------------------------------------------

    def _cmd_help(self, message: InboundMessage, arg: str) -> None:
        self._reply_to(message, build_help_text())

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
            sessions = [s for s in self._ops.list_sessions() if not s.archived]
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
        # (mvp-scope §3). The identity gate already limits commands to
        # admins; the ownership check stays explicit for the multi-chat
        # bound-to-one-session case.
        owner_chat = self._ownership.owner_of(active_id)
        if owner_chat != message.chat_id and not self._is_admin(message.sender_open_id):
            self._reply_to(message, "只有该 prompt 的发起者或管理员可以中止它。")
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

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _is_admin(self, open_id: str) -> bool:
        normalized = str(open_id or "").strip()
        return bool(normalized) and normalized in self._admins

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
