"""App handler (inbound path) contract tests.

Fakes stand in for the Feishu transport (records replies/cards) and the
KapRestClient (scriptable sessions/prompts/errors). A real BindingStore in a
temp dir and a real RuntimeLoop are used, so the serialization discipline
(transport thread -> RuntimeLoop -> impl) is exercised for real.
"""

from __future__ import annotations

import os
import pathlib
import re
import tempfile
import unittest

import yaml

from kite import config as kite_config
from kite.adapters.kap_server import KapError, KapTransportError, PromptQueueState, SessionSummary
from kite.app_handler import (
    ACTION_SESSION_SWITCH,
    AppHandler,
)
from kite.feishu_transport import CardAction, InboundAttachment, InboundMessage
from kite.prompt_ownership import CERTAINTY_BEST_EFFORT, PromptOwnership
from kite.runtime_loop import RuntimeLoop
from kite.stores.binding_store import BindingStore

ADMIN_OPEN_ID = "ou_admin"
CHAT_ID = "oc_chat"
DEFAULT_CWD = "/work/kite"
INIT_TOKEN = "test-init-token"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTransport:
    def __init__(self) -> None:
        self.replies: list[dict] = []
        self.cards: list[dict] = []

    def reply(self, chat_id: str, text: str, *, parent_message_id: str = "", reply_in_thread: bool = False) -> bool:
        self.replies.append(
            {"chat_id": chat_id, "text": text, "parent_message_id": parent_message_id}
        )
        return True

    def reply_card(self, chat_id: str, card: dict, *, parent_message_id: str = "", reply_in_thread: bool = False) -> None:
        self.cards.append(
            {"chat_id": chat_id, "card": card, "parent_message_id": parent_message_id}
        )

    def last_text(self) -> str:
        assert self.replies, "expected at least one text reply"
        return self.replies[-1]["text"]


class FakeKapRestClient:
    """Scriptable stand-in for KapRestClient (call/get/post + typed slice)."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.calls: list[tuple[str, str, object]] = []
        self.submissions: list[dict] = []
        self.aborts: list[tuple[str, str]] = []
        self.prompt_states: dict[str, PromptQueueState] = {}
        self.submit_status = "running"
        self.create_error: Exception | None = None
        self.get_session_error: Exception | None = None
        self.submit_error: Exception | None = None
        self.abort_error: Exception | None = None
        self.list_error: Exception | None = None
        self.prompts_error: Exception | None = None
        self._prompt_counter = 0

    # -- scripting helpers ---------------------------------------------------

    def add_session(
        self,
        session_id: str,
        *,
        title: str = "",
        cwd: str | None = DEFAULT_CWD,
        busy: bool = False,
        pending_interaction: str | None = None,
        archived: bool = False,
    ) -> dict:
        session = {
            "id": session_id,
            "title": title,
            "busy": busy,
            "pending_interaction": pending_interaction,
            "archived": archived,
            "metadata": {"cwd": cwd} if cwd else {},
        }
        self.sessions[session_id] = session
        return session

    def set_prompts(
        self,
        session_id: str,
        *,
        active: str | None = None,
        queued: tuple[str, ...] = (),
    ) -> None:
        self.prompt_states[session_id] = PromptQueueState(
            active_prompt_id=active, queued_prompt_ids=queued
        )

    # -- KapRestClient surface ------------------------------------------------

    def call(self, method: str, path: str, body: object = None) -> object:
        self.calls.append((method, path, body))
        if method == "POST" and path == "/sessions":
            if self.create_error is not None:
                raise self.create_error
            payload = body if isinstance(body, dict) else {}
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            session_id = f"s-{len(self.sessions) + 1}"
            return self.add_session(
                session_id,
                title=str(payload.get("title") or ""),
                cwd=metadata.get("cwd"),
            )
        match = re.fullmatch(r"/sessions/([^/]+)", path)
        if method == "GET" and match:
            if self.get_session_error is not None:
                raise self.get_session_error
            session = self.sessions.get(match.group(1))
            if session is None:
                raise KapError(40401, f"session {match.group(1)} does not exist")
            return session
        match = re.fullmatch(r"/sessions/([^/]+)/prompts", path)
        if method == "POST" and match:
            if self.submit_error is not None:
                raise self.submit_error
            session = self.sessions.get(match.group(1))
            if session is None:
                raise KapError(40401, f"session {match.group(1)} does not exist")
            self._prompt_counter += 1
            prompt_id = f"p-{self._prompt_counter}"
            self.submissions.append(
                {"session_id": match.group(1), "body": body, "prompt_id": prompt_id}
            )
            return {
                "prompt_id": prompt_id,
                "user_message_id": f"um-{prompt_id}",
                "status": self.submit_status,
                "content": [],
                "created_at": "2026-01-01T00:00:00Z",
            }
        match = re.fullmatch(r"/sessions/([^/]+)/prompts/([^/]+):abort", path)
        if method == "POST" and match:
            if self.abort_error is not None:
                raise self.abort_error
            self.aborts.append((match.group(1), match.group(2)))
            return {"aborted": True, "at_seq": 1}
        raise AssertionError(f"unexpected kap call: {method} {path}")

    def get(self, path: str) -> object:
        return self.call("GET", path)

    def post(self, path: str, body: object = None) -> object:
        return self.call("POST", path, body)

    def list_sessions(self) -> list[SessionSummary]:
        if self.list_error is not None:
            raise self.list_error
        return [
            SessionSummary(
                session_id=session["id"],
                title=session["title"],
                cwd=session["metadata"].get("cwd"),
                busy=session["busy"],
                pending_interaction=session["pending_interaction"],
                archived=session["archived"],
            )
            for session in self.sessions.values()
        ]

    def get_prompts(self, session_id: str) -> PromptQueueState:
        if self.prompts_error is not None:
            raise self.prompts_error
        return self.prompt_states.get(session_id, PromptQueueState(None, ()))


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


def make_message(
    text: str,
    *,
    sender: str = ADMIN_OPEN_ID,
    chat_id: str = CHAT_ID,
    message_id: str = "om_1",
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_type="p2p",
        msg_type="text",
        text=text,
        sender_open_id=sender,
        sender_user_id="u_1",
        sender_type="user",
        bot_mentioned=False,
        mentions=[],
        thread_id="",
        root_id="",
        parent_id="",
        create_time=0,
    )


def make_attachment(*, sender: str = ADMIN_OPEN_ID, chat_id: str = CHAT_ID) -> InboundAttachment:
    return InboundAttachment(
        message_id="om_att",
        chat_id=chat_id,
        chat_type="p2p",
        attachment_type="image",
        resource_key="img_key",
        file_name="",
        sender_open_id=sender,
        sender_user_id="u_1",
        sender_type="user",
        thread_id="",
        root_id="",
        parent_id="",
        create_time=0,
    )


def make_card_action(
    value: dict,
    *,
    operator: str = ADMIN_OPEN_ID,
    chat_id: str = CHAT_ID,
) -> CardAction:
    return CardAction(
        operator_open_id=operator,
        operator_user_id="u_1",
        chat_id=chat_id,
        message_id="om_card",
        value=value,
    )


class AppHandlerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_dir = pathlib.Path(self.tempdir.name)
        self.store = BindingStore(self.data_dir)
        self.transport = FakeTransport()
        self.rest = FakeKapRestClient()
        self.loop = RuntimeLoop(name="test-loop")
        self.addCleanup(self.loop.stop)
        self.persisted_admins: list[set[str]] = []
        self.bound_sessions: list[str] = []
        self.handler = self._make_handler()

    def _make_handler(self, *, admins: set[str] | None = None, **overrides) -> AppHandler:
        kwargs = dict(
            transport=self.transport,
            rest=self.rest,
            binding_store=self.store,
            runtime_loop=self.loop,
            config={
                "admin_open_ids": sorted(admins if admins is not None else {ADMIN_OPEN_ID}),
                "default_working_dir": DEFAULT_CWD,
            },
            init_token=INIT_TOKEN,
            on_session_bound=self.bound_sessions.append,
            persist_admins=lambda ids: self.persisted_admins.append(set(ids)),
        )
        kwargs.update(overrides)
        return AppHandler(**kwargs)

    def send(self, text: str, **kwargs) -> None:
        self.handler.on_message(make_message(text, **kwargs))

    def bind(
        self,
        session_id: str = "s-1",
        *,
        attached: bool = True,
        permission_mode: str = "auto",
        plan_mode: bool = False,
        chat_id: str = CHAT_ID,
    ) -> None:
        self.store.save(
            chat_id,
            {
                "session_id": session_id,
                "attached": attached,
                "permission_mode": permission_mode,
                "plan_mode": plan_mode,
            },
        )


# ---------------------------------------------------------------------------
# Identity and /init
# ---------------------------------------------------------------------------


class IdentityTests(AppHandlerTestCase):
    def test_non_admin_plain_text_is_politely_rejected(self) -> None:
        self.send("你好", sender="ou_stranger")
        self.assertIn("仅对管理员开放", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_non_admin_command_is_rejected(self) -> None:
        self.send("/status", sender="ou_stranger")
        self.assertIn("仅对管理员开放", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_non_admin_gets_help(self) -> None:
        self.send("/help", sender="ou_stranger")
        self.assertIn("KITE 命令导航", self.transport.last_text())

    def test_init_with_wrong_token_is_rejected(self) -> None:
        handler = self._make_handler(admins=set())
        handler.on_message(make_message("/init wrong-token", sender="ou_new"))
        self.assertIn("不正确", self.transport.last_text())
        self.assertEqual(self.persisted_admins, [])
        # Still not an admin afterwards.
        self.send("/status", sender="ou_new")
        self.assertIn("仅对管理员开放", self.transport.last_text())

    def test_init_registers_first_admin_and_persists(self) -> None:
        handler = self._make_handler(admins=set())
        handler.on_message(make_message(f"/init {INIT_TOKEN}", sender="ou_new"))
        self.assertIn("管理员注册成功", self.transport.last_text())
        self.assertEqual(self.persisted_admins, [{"ou_new"}])
        # The new admin can now use the bot.
        handler.on_message(make_message("hello", sender="ou_new"))
        self.assertIn("已提交", self.transport.last_text())

    def test_init_without_token_shows_usage(self) -> None:
        self.send("/init", sender="ou_stranger")
        self.assertIn("用法", self.transport.last_text())
        self.assertEqual(self.persisted_admins, [])

    def test_init_for_existing_admin_is_idempotent(self) -> None:
        self.send(f"/init {INIT_TOKEN}")
        self.assertIn("已经是管理员", self.transport.last_text())
        self.assertEqual(self.persisted_admins, [])

    def test_init_persists_into_system_config_by_default(self) -> None:
        config_dir = self.data_dir / "config"
        os.environ["KITE_CONFIG_DIR"] = str(config_dir)
        self.addCleanup(lambda: os.environ.pop("KITE_CONFIG_DIR", None))
        # No persist_admins injection: the production default writes system.yaml.
        handler = AppHandler(
            transport=self.transport,
            rest=self.rest,
            binding_store=self.store,
            runtime_loop=self.loop,
            config={"admin_open_ids": [], "default_working_dir": DEFAULT_CWD},
            init_token=INIT_TOKEN,
        )
        handler.on_message(make_message(f"/init {INIT_TOKEN}", sender="ou_new"))
        raw = kite_config.load_system_config_raw()
        self.assertEqual(raw.get("admin_open_ids"), ["ou_new"])
        on_disk = yaml.safe_load((config_dir / "system.yaml").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["admin_open_ids"], ["ou_new"])


# ---------------------------------------------------------------------------
# Plain-text prompt path
# ---------------------------------------------------------------------------


class PromptSubmissionTests(AppHandlerTestCase):
    def test_first_message_creates_session_binds_and_submits(self) -> None:
        self.send("帮我看看这段代码")
        # create + pre-flight get + submit
        methods = [(method, path) for method, path, _ in self.rest.calls]
        self.assertEqual(
            methods,
            [("POST", "/sessions"), ("GET", "/sessions/s-1"), ("POST", "/sessions/s-1/prompts")],
        )
        create_body = self.rest.calls[0][2]
        self.assertEqual(create_body["metadata"]["cwd"], DEFAULT_CWD)
        self.assertEqual(create_body["title"], "帮我看看这段代码")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-1")
        self.assertTrue(binding["attached"])
        self.assertEqual(binding["permission_mode"], "auto")
        self.assertFalse(binding["plan_mode"])
        submission = self.rest.submissions[0]
        self.assertEqual(
            submission["body"]["content"],
            [{"type": "text", "text": "帮我看看这段代码"}],
        )
        self.assertEqual(submission["body"]["permission_mode"], "auto")
        self.assertIs(submission["body"]["plan_mode"], False)
        self.assertEqual(self.handler.prompt_ownership.owner_of("p-1"), CHAT_ID)
        reply = self.transport.last_text()
        self.assertIn("已创建并绑定新会话", reply)
        self.assertIn("已提交", reply)
        self.assertEqual(self.transport.replies[-1]["parent_message_id"], "om_1")
        self.assertEqual(self.bound_sessions, ["s-1"])

    def test_second_message_reuses_existing_binding(self) -> None:
        self.send("first")
        self.send("second")
        creates = [c for c in self.rest.calls if c[0] == "POST" and c[1] == "/sessions"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(self.rest.submissions), 2)
        self.assertEqual(self.rest.submissions[1]["session_id"], "s-1")

    def test_queued_submission_gets_queue_ack(self) -> None:
        self.rest.submit_status = "queued"
        self.send("排队吧")
        self.assertIn("已加入队列", self.transport.last_text())
        self.assertEqual(self.handler.prompt_ownership.owner_of("p-1"), CHAT_ID)

    def test_blocked_submission_is_reported_and_not_owned(self) -> None:
        self.rest.submit_status = "blocked"
        self.send(" blocked one ")
        self.assertIn("拒绝", self.transport.last_text())
        self.assertIsNone(self.handler.prompt_ownership.owner_of("p-1"))

    def test_kap_unreachable_on_create_is_fail_closed(self) -> None:
        self.rest.create_error = KapTransportError("connection refused")
        self.send("hello")
        self.assertIn("无法连接 kap-server", self.transport.last_text())
        self.assertIsNone(self.store.load(CHAT_ID))
        self.assertEqual(self.rest.submissions, [])
        self.assertEqual(self.transport.cards, [])

    def test_kap_business_error_on_create_is_fail_closed(self) -> None:
        self.rest.create_error = KapError(50001, "disk full")
        self.send("hello")
        self.assertIn("创建会话失败：disk full", self.transport.last_text())
        self.assertIsNone(self.store.load(CHAT_ID))

    def test_kap_unreachable_on_submit_reports_error_keeps_binding(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.submit_error = KapTransportError("timeout")
        self.send("hello")
        self.assertIn("无法连接 kap-server", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-1")
        self.assertIsNone(self.handler.prompt_ownership.owner_of("p-1"))

    def test_kap_business_error_on_submit_shows_upstream_msg(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.submit_error = KapError(40901, "session is busy")
        self.send("hello")
        self.assertIn("提交失败：session is busy", self.transport.last_text())
        self.assertEqual(self.transport.cards, [])

    def test_archived_session_errors_and_suggests_sessions(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1", archived=True)
        self.send("hello")
        reply = self.transport.last_text()
        self.assertIn("已被归档", reply)
        self.assertIn("/sessions", reply)
        self.assertEqual(self.rest.submissions, [])
        # Never auto-recreates: no create call.
        self.assertEqual([c for c in self.rest.calls if c[1] == "/sessions" and c[0] == "POST"], [])

    def test_missing_session_errors_and_suggests_sessions(self) -> None:
        self.bind("s-gone")
        self.send("hello")
        reply = self.transport.last_text()
        self.assertIn("已不存在", reply)
        self.assertIn("/sessions", reply)
        self.assertEqual(self.rest.submissions, [])
        # The binding is kept for the user to act on.
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-gone")

    def test_detached_chat_refuses_prompt_with_notice(self) -> None:
        self.bind("s-1", attached=False)
        self.rest.add_session("s-1")
        self.send("hello")
        self.assertIn("/attach", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_empty_text_gets_notice_not_prompt(self) -> None:
        self.send("   ")
        self.assertIn("只处理文字消息", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_permission_and_plan_modes_are_carried_per_prompt(self) -> None:
        self.bind("s-1", permission_mode="manual", plan_mode=True)
        self.rest.add_session("s-1")
        self.send("hello")
        body = self.rest.submissions[0]["body"]
        self.assertEqual(body["permission_mode"], "manual")
        self.assertIs(body["plan_mode"], True)

    def test_model_is_carried_per_prompt_when_configured(self) -> None:
        self.handler = self._make_handler(prompt_model="kimi-code/k3")
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("hello")
        self.assertEqual(self.rest.submissions[0]["body"]["model"], "kimi-code/k3")

    def test_model_omitted_when_not_configured(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("hello")
        self.assertNotIn("model", self.rest.submissions[0]["body"])

    def test_shared_empty_ownership_is_not_split(self) -> None:
        # Regression (live 2026-07-22): PromptOwnership defines __len__, so an
        # empty map is falsy and `or` used to silently swap in a private map —
        # approvals then routed as "unattributable" and expired.
        shared = PromptOwnership()
        self.handler = self._make_handler(prompt_ownership=shared)
        self.assertIs(self.handler.prompt_ownership, shared)
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("hello")
        self.assertEqual(len(shared), 1)

    def test_unknown_slash_command_points_to_help(self) -> None:
        self.send("/bogus stuff")
        self.assertIn("未知命令", self.transport.last_text())
        self.assertIn("/help", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])


# ---------------------------------------------------------------------------
# /new, /switch, /detach, /attach
# ---------------------------------------------------------------------------


class BindingCommandTests(AppHandlerTestCase):
    def test_new_creates_and_rebinds_keeping_modes(self) -> None:
        self.bind("s-old", permission_mode="manual", plan_mode=True)
        self.send("/new")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-1")
        self.assertEqual(binding["permission_mode"], "manual")
        self.assertTrue(binding["plan_mode"])
        self.assertIn("s-1", self.transport.last_text())
        self.assertEqual(self.bound_sessions, ["s-1"])

    def test_new_keeps_old_binding_when_create_fails(self) -> None:
        self.bind("s-old")
        self.rest.create_error = KapTransportError("down")
        self.send("/new")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-old")
        self.assertIn("无法连接 kap-server", self.transport.last_text())

    def test_new_with_arg_shows_usage(self) -> None:
        self.send("/new please")
        self.assertIn("用法", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_switch_rebinds_and_announces(self) -> None:
        self.rest.add_session("s-2", title="Beta")
        self.send("/switch s-2")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-2")
        self.assertTrue(binding["attached"])
        self.assertIn("已切换到会话 Beta", self.transport.last_text())
        self.assertEqual(self.bound_sessions, ["s-2"])

    def test_switch_to_missing_session_fails(self) -> None:
        self.bind("s-1")
        self.send("/switch s-nope")
        self.assertIn("不存在", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-1")

    def test_switch_to_archived_session_is_rejected(self) -> None:
        self.rest.add_session("s-2", archived=True)
        self.send("/switch s-2")
        self.assertIn("已归档", self.transport.last_text())
        self.assertIsNone(self.store.load(CHAT_ID))

    def test_switch_without_arg_shows_usage(self) -> None:
        self.send("/switch")
        self.assertIn("用法", self.transport.last_text())

    def test_switch_to_current_session_is_idempotent(self) -> None:
        self.bind("s-1")
        self.send("/switch s-1")
        self.assertIn("当前已绑定该会话", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_switch_to_current_detached_session_reattaches(self) -> None:
        self.bind("s-1", attached=False)
        self.send("/switch s-1")
        self.assertIn("推送已恢复", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertTrue(binding["attached"])

    def test_detach_pauses_push_and_keeps_binding(self) -> None:
        self.bind("s-1")
        self.send("/detach")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertFalse(binding["attached"])
        self.assertEqual(binding["session_id"], "s-1")
        self.assertIn("已暂停", self.transport.last_text())

    def test_detach_when_already_detached(self) -> None:
        self.bind("s-1", attached=False)
        self.send("/detach")
        self.assertIn("已是暂停推送状态", self.transport.last_text())

    def test_attach_resumes_push(self) -> None:
        self.bind("s-1", attached=False)
        self.send("/attach")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertTrue(binding["attached"])
        self.assertIn("已恢复", self.transport.last_text())

    def test_attach_when_already_attached(self) -> None:
        self.bind("s-1")
        self.send("/attach")
        self.assertIn("已在接收推送", self.transport.last_text())

    def test_detach_without_binding_shows_hint(self) -> None:
        self.send("/detach")
        self.assertIn("尚未绑定会话", self.transport.last_text())

    def test_attach_with_arg_shows_usage(self) -> None:
        self.bind("s-1", attached=False)
        self.send("/attach now")
        self.assertIn("用法", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertFalse(binding["attached"])


# ---------------------------------------------------------------------------
# /mode, /plan, /status, /abort
# ---------------------------------------------------------------------------


class ModeAndPlanTests(AppHandlerTestCase):
    def test_mode_without_arg_shows_current(self) -> None:
        self.bind("s-1", permission_mode="manual")
        self.send("/mode")
        self.assertIn("当前权限模式：manual", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_mode_set_persists(self) -> None:
        self.bind("s-1")
        self.send("/mode manual")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["permission_mode"], "manual")
        self.assertIn("已切换为 manual", self.transport.last_text())

    def test_mode_yolo_announces_auto_approval(self) -> None:
        self.bind("s-1")
        self.send("/mode yolo")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["permission_mode"], "yolo")
        self.assertIn("自动批准", self.transport.last_text())

    def test_mode_invalid_arg_shows_usage(self) -> None:
        self.bind("s-1")
        self.send("/mode turbo")
        self.assertIn("用法", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["permission_mode"], "auto")

    def test_mode_same_value_is_idempotent(self) -> None:
        self.bind("s-1", permission_mode="manual")
        self.send("/mode manual")
        self.assertIn("已是 manual", self.transport.last_text())

    def test_mode_without_binding_shows_hint(self) -> None:
        self.send("/mode yolo")
        self.assertIn("尚未绑定会话", self.transport.last_text())

    def test_plan_toggles_and_announces(self) -> None:
        self.bind("s-1")
        self.send("/plan")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertTrue(binding["plan_mode"])
        self.assertIn("已开启", self.transport.last_text())
        self.send("/plan")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertFalse(binding["plan_mode"])
        self.assertIn("已关闭", self.transport.last_text())

    def test_plan_explicit_on_off(self) -> None:
        self.bind("s-1")
        self.send("/plan on")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertTrue(binding["plan_mode"])
        self.send("/plan off")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertFalse(binding["plan_mode"])

    def test_plan_invalid_arg_shows_usage(self) -> None:
        self.bind("s-1")
        self.send("/plan maybe")
        self.assertIn("用法", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertFalse(binding["plan_mode"])

    def test_mode_and_plan_flow_into_next_prompt(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("/mode yolo")
        self.send("/plan on")
        self.send("干活")
        body = self.rest.submissions[0]["body"]
        self.assertEqual(body["permission_mode"], "yolo")
        self.assertIs(body["plan_mode"], True)


class StatusTests(AppHandlerTestCase):
    def test_status_renders_binding_session_and_queue(self) -> None:
        self.bind("s-1", permission_mode="manual", plan_mode=True)
        self.rest.add_session("s-1", title="Alpha", busy=True, pending_interaction="approval")
        self.rest.set_prompts("s-1", active="p-1", queued=("p-2", "p-3"))
        self.send("/status")
        reply = self.transport.last_text()
        self.assertIn("s-1", reply)
        self.assertIn("已开启", reply)  # push state
        self.assertIn("manual", reply)
        self.assertIn("计划模式：开启", reply)
        self.assertIn("Alpha", reply)
        self.assertIn("忙碌", reply)
        self.assertIn("approval", reply)
        self.assertIn("1 条执行中", reply)
        self.assertIn("排队 2 条", reply)

    def test_status_without_binding_shows_hint(self) -> None:
        self.send("/status")
        self.assertIn("尚未绑定会话", self.transport.last_text())

    def test_status_when_kap_down_shows_binding_and_error(self) -> None:
        self.bind("s-1")
        self.rest.get_session_error = KapTransportError("down")
        self.send("/status")
        reply = self.transport.last_text()
        self.assertIn("s-1", reply)
        self.assertIn("无法连接 kap-server", reply)

    def test_status_marks_archived_session(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1", archived=True)
        self.send("/status")
        self.assertIn("已归档", self.transport.last_text())


class AbortTests(AppHandlerTestCase):
    def test_abort_by_initiator_succeeds(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("干活")  # p-1, ownership recorded for this chat
        self.rest.set_prompts("s-1", active="p-1")
        self.send("/abort")
        self.assertEqual(self.rest.aborts, [("s-1", "p-1")])
        self.assertIn("已中止", self.transport.last_text())

    def test_abort_by_admin_when_initiated_elsewhere(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.handler.prompt_ownership.record("p-9", "oc_elsewhere")
        self.rest.set_prompts("s-1", active="p-9")
        self.send("/abort")
        self.assertEqual(self.rest.aborts, [("s-1", "p-9")])

    def test_abort_without_active_prompt(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("/abort")
        self.assertIn("没有正在执行", self.transport.last_text())
        self.assertEqual(self.rest.aborts, [])

    def test_abort_40402_reports_already_finished(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.set_prompts("s-1", active="p-1")
        self.handler.prompt_ownership.record("p-1", CHAT_ID)
        self.rest.abort_error = KapError(40402, "one or more prompts are not pending")
        self.send("/abort")
        self.assertIn("已结束", self.transport.last_text())

    def test_abort_when_kap_down(self) -> None:
        self.bind("s-1")
        self.rest.prompts_error = KapTransportError("down")
        self.send("/abort")
        self.assertIn("无法连接 kap-server", self.transport.last_text())

    def test_abort_without_binding_shows_hint(self) -> None:
        self.send("/abort")
        self.assertIn("尚未绑定会话", self.transport.last_text())


# ---------------------------------------------------------------------------
# /sessions and card actions
# ---------------------------------------------------------------------------


def _card_elements(card: dict) -> list[dict]:
    return card["elements"]


def _action_buttons(card: dict) -> list[dict]:
    for element in _card_elements(card):
        if element.get("tag") == "action":
            return element["actions"]
    return []


class SessionsTests(AppHandlerTestCase):
    def test_sessions_renders_card_with_switch_buttons(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1", title="Alpha", busy=True, pending_interaction="approval")
        self.rest.add_session("s-2", title="Beta")
        self.rest.add_session("s-3", title="Archived", archived=True)
        self.send("/sessions")
        self.assertEqual(len(self.transport.cards), 1)
        card = self.transport.cards[0]["card"]
        markdown = _card_elements(card)[0]["content"]
        self.assertIn("Alpha", markdown)
        self.assertIn("s-1", markdown)
        self.assertIn("当前绑定", markdown)
        self.assertIn("Beta", markdown)
        self.assertNotIn("Archived", markdown)
        self.assertNotIn("s-3", markdown)
        buttons = _action_buttons(card)
        values = [button["value"] for button in buttons]
        self.assertEqual(
            values,
            [
                {"action": ACTION_SESSION_SWITCH, "session_id": "s-1"},
                {"action": ACTION_SESSION_SWITCH, "session_id": "s-2"},
            ],
        )

    def test_sessions_empty_list(self) -> None:
        self.send("/sessions")
        self.assertIn("没有可用会话", self.transport.last_text())
        self.assertEqual(self.transport.cards, [])

    def test_sessions_when_kap_down(self) -> None:
        self.rest.list_error = KapTransportError("down")
        self.send("/sessions")
        self.assertIn("无法连接 kap-server", self.transport.last_text())

    def test_sessions_buttons_are_capped(self) -> None:
        for index in range(12):
            self.rest.add_session(f"s-{index}", title=f"S{index}")
        self.send("/sessions")
        card = self.transport.cards[0]["card"]
        self.assertEqual(len(_action_buttons(card)), 10)
        markdown = _card_elements(card)[0]["content"]
        self.assertIn("/switch", markdown)

    def test_card_action_session_switch_rebinds(self) -> None:
        self.rest.add_session("s-2", title="Beta")
        response = self.handler.on_card_action(
            make_card_action({"action": ACTION_SESSION_SWITCH, "session_id": "s-2"})
        )
        assert response.toast is not None
        self.assertIn("已切换到会话 Beta", response.toast)
        self.assertEqual(response.toast_type, "info")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-2")

    def test_card_action_session_switch_denied_for_non_admin(self) -> None:
        self.rest.add_session("s-2")
        response = self.handler.on_card_action(
            make_card_action(
                {"action": ACTION_SESSION_SWITCH, "session_id": "s-2"},
                operator="ou_stranger",
            )
        )
        self.assertEqual(response.toast_type, "error")
        self.assertIsNone(self.store.load(CHAT_ID))

    def test_card_action_session_switch_missing_id(self) -> None:
        response = self.handler.on_card_action(
            make_card_action({"action": ACTION_SESSION_SWITCH})
        )
        self.assertEqual(response.toast_type, "error")

    def test_card_action_unknown_action_is_ignored(self) -> None:
        response = self.handler.on_card_action(make_card_action({"action": "mystery"}))
        self.assertIsNone(response.card)
        self.assertIsNone(response.toast)


class E3SeamTests(AppHandlerTestCase):
    def test_approval_and_question_actions_route_to_seams(self) -> None:
        received: list[tuple[str, CardAction]] = []

        class RecordingHandler(AppHandler):
            def handle_approval_action(self, action: CardAction):
                received.append(("approval", action))
                return super().handle_approval_action(action)

            def handle_question_action(self, action: CardAction):
                received.append(("question", action))
                return super().handle_question_action(action)

        handler = RecordingHandler(
            transport=self.transport,
            rest=self.rest,
            binding_store=self.store,
            runtime_loop=self.loop,
            config={"admin_open_ids": [ADMIN_OPEN_ID], "default_working_dir": DEFAULT_CWD},
            init_token=INIT_TOKEN,
        )
        response = handler.on_card_action(
            make_card_action({"action": "approval_resolve", "approval_id": "a-1", "prompt_id": "p-1"})
        )
        self.assertIsNone(response.card)
        self.assertIsNone(response.toast)
        self.assertEqual([kind for kind, _ in received], ["approval"])
        handler.on_card_action(make_card_action({"action": "question_answer"}))
        self.assertEqual([kind for kind, _ in received], ["approval", "question"])

    def test_interaction_reply_seam_default_consumes_nothing(self) -> None:
        message = make_message("1")
        self.assertFalse(self.handler.try_handle_interaction_reply(message))

    def test_interaction_reply_seam_can_claim_text(self) -> None:
        claimed: list[str] = []

        class ClaimingHandler(AppHandler):
            def try_handle_interaction_reply(self, message: InboundMessage) -> bool:
                claimed.append(message.text)
                return True

        handler = ClaimingHandler(
            transport=self.transport,
            rest=self.rest,
            binding_store=self.store,
            runtime_loop=self.loop,
            config={"admin_open_ids": [ADMIN_OPEN_ID], "default_working_dir": DEFAULT_CWD},
            init_token=INIT_TOKEN,
        )
        handler.on_message(make_message("1"))
        self.assertEqual(claimed, ["1"])
        self.assertEqual(self.rest.calls, [])  # never became a prompt

    def test_rebuild_prompt_ownership_marks_best_effort(self) -> None:
        self.bind("s-1", chat_id="oc_a")
        self.bind("s-2", chat_id="oc_b")
        self.bind("s-broken", chat_id="oc_c")
        self.rest.set_prompts("s-1", active="p-1", queued=("p-2",))
        # s-2 has no prompt state (empty); s-broken errors.
        self.handler.prompt_ownership.record("p-stale", "oc_a")

        def failing_get_prompts(session_id: str) -> PromptQueueState:
            if session_id == "s-broken":
                raise KapTransportError("down")
            return self.rest.prompt_states.get(session_id, PromptQueueState(None, ()))

        self.rest.get_prompts = failing_get_prompts  # type: ignore[method-assign]
        self.handler.rebuild_prompt_ownership()
        ownership = self.handler.prompt_ownership
        self.assertEqual(ownership.owner_of("p-1"), "oc_a")
        self.assertEqual(ownership.owner_of("p-2"), "oc_a")
        self.assertEqual(ownership.certainty_of("p-1"), CERTAINTY_BEST_EFFORT)
        self.assertIsNone(ownership.owner_of("p-stale"))  # wholesale replace
        self.assertEqual(len(ownership), 2)


# ---------------------------------------------------------------------------
# Attachment handling
# ---------------------------------------------------------------------------


class AttachmentTests(AppHandlerTestCase):
    def test_attachment_gets_polite_not_supported(self) -> None:
        self.handler.on_attachment(make_attachment())
        self.assertIn("暂不支持", self.transport.last_text())
        self.assertEqual(self.transport.replies[-1]["parent_message_id"], "om_att")
        self.assertEqual(self.rest.calls, [])

    def test_attachment_from_non_admin_is_rejected(self) -> None:
        self.handler.on_attachment(make_attachment(sender="ou_stranger"))
        self.assertIn("仅对管理员开放", self.transport.last_text())


if __name__ == "__main__":
    unittest.main()
