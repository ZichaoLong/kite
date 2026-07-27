"""App handler (inbound path) contract tests.

Fakes stand in for the Feishu transport (records replies/cards) and the
KapRestClient (scriptable sessions/prompts/errors). A real BindingStore in a
temp dir and a real RuntimeLoop are used, so the serialization discipline
(transport thread -> RuntimeLoop -> impl) is exercised for real.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import pathlib
import re
import tempfile
import time
import unittest
from types import SimpleNamespace

import yaml

from kite import config as kite_config
from kite.adapters.kap_server import KapError, KapTransportError, PromptQueueState, SessionSummary
from kite.app_handler import (
    ACTION_SESSION_SWITCH,
    AppHandler,
)
from kite.card_text_projection import TERMINAL_RESULT_CARD_MARKER
from kite.cards import build_terminal_card
from kite.feishu_transport import (
    CardAction,
    DownloadedMessageResource,
    InboundAttachment,
    InboundMergeForward,
    InboundMessage,
)
from kite.identity_names import IdentityNames
from kite.prompt_ownership import CERTAINTY_BEST_EFFORT, PromptOwnership
from kite.runtime_loop import RuntimeLoop
from kite.stores.binding_store import BindingStore
from kite.stores.group_config_store import GROUP_MODE_ASSISTANT, GroupConfigStore
from kite.stores.group_log_store import GroupLogStore
from kite.stores.pending_attachment_store import PendingAttachmentStore
from kite.stores.terminal_result_store import TerminalResultRecord, TerminalResultStore
from test_forward_aggregator import FakeTimer

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
        self.downloads: list[tuple[str, str, str]] = []
        self.download_result = DownloadedMessageResource(
            content=b"\x89PNG-fake-image-bytes",
            file_name="photo.png",
            content_type="image/png",
        )
        self.download_error: Exception | None = None
        self.uploads: list[str] = []
        self.upload_result: str | None = "img_key_fake"
        self.sent_images: list[tuple[str, str]] = []
        self.fail_image_chats: set[str] = set()
        self.merge_forward_items: list = []
        self.merge_forward_error: Exception | None = None
        self.merge_forward_fetches: list[str] = []
        # /last history fallback scripting (Feishu message history page).
        self.history_items: list = []
        self.history_error: Exception | None = None
        self.list_messages_calls: list[dict] = []

    def reply(self, chat_id: str, text: str, *, parent_message_id: str = "", reply_in_thread: bool = False) -> bool:
        self.replies.append(
            {"chat_id": chat_id, "text": text, "parent_message_id": parent_message_id}
        )
        return True

    def reply_card(self, chat_id: str, card: dict, *, parent_message_id: str = "", reply_in_thread: bool = False) -> None:
        self.cards.append(
            {"chat_id": chat_id, "card": card, "parent_message_id": parent_message_id}
        )

    def list_messages(
        self,
        chat_id: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        sort_type: str = "ByCreateTimeAsc",
        page_size: int = 50,
        page_token: str = "",
        card_msg_content_type: str = "",
    ):
        self.list_messages_calls.append(
            {
                "chat_id": chat_id,
                "start_time": start_time,
                "end_time": end_time,
                "sort_type": sort_type,
                "page_size": page_size,
                "page_token": page_token,
                "card_msg_content_type": card_msg_content_type,
            }
        )
        if self.history_error is not None:
            raise self.history_error
        return SimpleNamespace(items=list(self.history_items), has_more=False, page_token="")

    def download_message_resource(
        self, message_id: str, file_key: str, *, resource_type: str
    ) -> DownloadedMessageResource:
        self.downloads.append((message_id, file_key, resource_type))
        if self.download_error is not None:
            raise self.download_error
        return self.download_result

    def upload_image(self, local_path: str) -> str | None:
        self.uploads.append(local_path)
        return self.upload_result

    def send_image_by_key(self, chat_id: str, image_key: str) -> str | None:
        if chat_id in self.fail_image_chats:
            return None
        self.sent_images.append((chat_id, image_key))
        return f"om_img_{len(self.sent_images)}"

    def fetch_merge_forward_items(self, message_id: str) -> list:
        self.merge_forward_fetches.append(message_id)
        if self.merge_forward_error is not None:
            raise self.merge_forward_error
        return list(self.merge_forward_items)

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
        # Session-lifecycle scripting (:compact/:archive/:restore, /profile):
        # (session_id, action, body) and (session_id, body) records.
        self.session_actions: list[tuple[str, str, object]] = []
        self.profile_updates: list[tuple[str, object]] = []
        self.prompt_states: dict[str, PromptQueueState] = {}
        self.submit_status = "running"
        self.create_error: Exception | None = None
        self.get_session_error: Exception | None = None
        self.submit_error: Exception | None = None
        self.abort_error: Exception | None = None
        self.action_error: Exception | None = None
        self.profile_error: Exception | None = None
        self.list_error: Exception | None = None
        self.prompts_error: Exception | None = None
        # Upstream goal state per session (profile agent_config goal_objective
        # / goal_control semantics).
        self.goals: dict[str, dict] = {}
        # /btw scripting: agent ids handed out per :btw start (empty = the
        # legacy deterministic id), and agent ids whose prompt submit must
        # fail with agent.not_found (upstream maps it onto the 40401
        # envelope, routes/prompts.ts sendMappedError).
        self.btw_agent_ids: list[str] = []
        self.dead_btw_agents: set[str] = set()
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
        updated_at: str = "",
    ) -> dict:
        session = {
            "id": session_id,
            "title": title,
            "busy": busy,
            "pending_interaction": pending_interaction,
            "archived": archived,
            "updated_at": updated_at,
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
        match = re.fullmatch(r"/sessions/([^/]+)/goal", path)
        if method == "GET" and match:
            session = self.sessions.get(match.group(1))
            if session is None:
                raise KapError(40401, f"session {match.group(1)} does not exist")
            return self.goals.get(match.group(1))
        match = re.fullmatch(r"/sessions/([^/]+)/prompts", path)
        if method == "POST" and match:
            if self.submit_error is not None:
                raise self.submit_error
            session = self.sessions.get(match.group(1))
            if session is None:
                raise KapError(40401, f"session {match.group(1)} does not exist")
            agent_id = body.get("agent_id") if isinstance(body, dict) else None
            if agent_id and agent_id in self.dead_btw_agents:
                raise KapError(40401, f"agent {agent_id} does not exist")
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
        match = re.fullmatch(r"/sessions/([^/]+)/profile", path)
        if method == "POST" and match:
            if self.profile_error is not None:
                raise self.profile_error
            session = self.sessions.get(match.group(1))
            if session is None:
                raise KapError(40401, f"session {match.group(1)} does not exist")
            self.profile_updates.append((match.group(1), body))
            if isinstance(body, dict) and body.get("title") is not None:
                session["title"] = str(body["title"])
            if isinstance(body, dict) and isinstance(body.get("agent_config"), dict):
                agent_config = body["agent_config"]
                objective = agent_config.get("goal_objective")
                if isinstance(objective, str) and objective:
                    self.goals[match.group(1)] = {
                        "objective": objective,
                        "status": "active",
                    }
                control = agent_config.get("goal_control")
                if control == "cancel":
                    self.goals.pop(match.group(1), None)
                elif control == "pause" and match.group(1) in self.goals:
                    self.goals[match.group(1)]["status"] = "paused"
                elif control == "resume" and match.group(1) in self.goals:
                    self.goals[match.group(1)]["status"] = "active"
            return session
        match = re.fullmatch(r"/sessions/([^/]+):btw", path)
        if method == "POST" and match:
            if self.action_error is not None:
                raise self.action_error
            session = self.sessions.get(match.group(1))
            if session is None:
                raise KapError(40401, f"session {match.group(1)} does not exist")
            self.session_actions.append((match.group(1), "btw", body))
            agent_id = (
                self.btw_agent_ids.pop(0) if self.btw_agent_ids else f"btw-{match.group(1)}"
            )
            return {"agent_id": agent_id}
        match = re.fullmatch(r"/sessions/([^/]+):(compact|archive|restore)", path)
        if method == "POST" and match:
            if self.action_error is not None:
                raise self.action_error
            session = self.sessions.get(match.group(1))
            if session is None:
                raise KapError(40401, f"session {match.group(1)} does not exist")
            action = match.group(2)
            self.session_actions.append((match.group(1), action, body))
            if action == "compact":
                # compactSessionResponseSchema: an empty object.
                return {}
            if action == "archive":
                # archiveSessionResponseSchema: {archived: true}.
                session["archived"] = True
                return {"archived": True}
            # restore: restoreSessionResponseSchema is a full session.
            session["archived"] = False
            return session
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
                updated_at=session.get("updated_at", ""),
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


def make_attachment(
    *,
    sender: str = ADMIN_OPEN_ID,
    chat_id: str = CHAT_ID,
    message_id: str = "om_att",
    attachment_type: str = "image",
    resource_key: str = "img_key",
) -> InboundAttachment:
    return InboundAttachment(
        message_id=message_id,
        chat_id=chat_id,
        chat_type="p2p",
        attachment_type=attachment_type,
        resource_key=resource_key,
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


def make_merge_forward(
    *,
    sender: str = ADMIN_OPEN_ID,
    chat_id: str = CHAT_ID,
    chat_type: str = "p2p",
    message_id: str = "om_fwd",
) -> InboundMergeForward:
    return InboundMergeForward(
        message_id=message_id,
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender,
        sender_user_id="u_1",
        sender_type="user",
        thread_id="",
        root_id="",
        parent_id="",
        create_time=0,
    )


def make_forward_item(
    message_id: str,
    *,
    msg_type: str = "text",
    text: str = "",
    sender_id: str = "ou_alice",
    sender_type: str = "user",
    create_time: int = 1712476800000,
    upper_message_id: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        msg_type=msg_type,
        upper_message_id=upper_message_id,
        sender=SimpleNamespace(id=sender_id, sender_type=sender_type),
        create_time=create_time,
        body=SimpleNamespace(content=json.dumps({"text": text}, ensure_ascii=False)),
    )


class AppHandlerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_dir = pathlib.Path(self.tempdir.name)
        self.store = BindingStore(self.data_dir)
        self.attachment_store = PendingAttachmentStore(self.data_dir)
        self.group_config_store = GroupConfigStore(self.data_dir)
        self.transport = FakeTransport()
        self.rest = FakeKapRestClient()
        self.loop = RuntimeLoop(name="test-loop")
        self.addCleanup(self.loop.stop)
        self.persisted_admins: list[set[str]] = []
        self.bound_sessions: list[str] = []
        self.forward_timers: list[FakeTimer] = []
        self.handler = self._make_handler()

    def _forward_timer_factory(self, timeout: float, callback, args: list[str]) -> FakeTimer:
        timer = FakeTimer(timeout, callback, args)
        self.forward_timers.append(timer)
        return timer

    def _make_handler(self, *, admins: set[str] | None = None, **overrides) -> AppHandler:
        kwargs = dict(
            transport=self.transport,
            rest=self.rest,
            binding_store=self.store,
            attachment_store=self.attachment_store,
            group_config_store=self.group_config_store,
            runtime_loop=self.loop,
            config={
                "admin_open_ids": sorted(admins if admins is not None else {ADMIN_OPEN_ID}),
                "default_working_dir": DEFAULT_CWD,
            },
            init_token=INIT_TOKEN,
            on_session_bound=self.bound_sessions.append,
            persist_admins=lambda ids: self.persisted_admins.append(set(ids)),
            forward_timer_factory=self._forward_timer_factory,
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
        effort: str = "",
        chat_id: str = CHAT_ID,
    ) -> None:
        self.store.save(
            chat_id,
            {
                "session_id": session_id,
                "attached": attached,
                "permission_mode": permission_mode,
                "plan_mode": plan_mode,
                "effort": effort,
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
            attachment_store=PendingAttachmentStore(self.data_dir),
            group_config_store=self.group_config_store,
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


class WhoamiTests(AppHandlerTestCase):
    def test_whoami_admin_p2p_shows_identity_and_binding(self) -> None:
        self.bind("s-1")

        self.send("/whoami")

        text = self.transport.last_text()
        self.assertIn(ADMIN_OPEN_ID, text)
        self.assertIn("管理员", text)
        self.assertIn("s-1", text)
        self.assertIn("单聊", text)

    def test_whoami_non_admin_allowed(self) -> None:
        self.handler = self._make_handler(admins=set())

        self.send("/whoami", sender="ou_stranger")

        text = self.transport.last_text()
        self.assertIn("ou_stranger", text)
        self.assertIn("非管理员", text)

    def test_whoami_unbound_shows_no_binding(self) -> None:
        self.send("/whoami")

        self.assertIn("绑定会话：无", self.transport.last_text())


class LastCommandTests(AppHandlerTestCase):
    def _seed_terminal(self, text: str = "最终答复文本", session_id: str = "s-1") -> TerminalResultStore:
        store = TerminalResultStore(self.data_dir)
        store.upsert(
            TerminalResultRecord(
                message_id="om_t1",
                execution_message_id="om_e1",
                final_reply_text=text,
                recorded_at=1000.0,
                terminal_result_id="p-1",
                session_id=session_id,
            )
        )
        return store

    def test_last_replies_with_terminal_text(self) -> None:
        store = self._seed_terminal()
        self.handler = self._make_handler(terminal_store=store)
        self.bind("s-1")

        self.send("/last")

        self.assertIn("最终答复文本", self.transport.last_text())

    def test_last_without_record(self) -> None:
        self.handler = self._make_handler(terminal_store=TerminalResultStore(self.data_dir))
        self.bind("s-1")

        self.send("/last")

        self.assertIn("暂无终态答复记录", self.transport.last_text())

    def test_last_truncates_long_text(self) -> None:
        store = self._seed_terminal(text="长" * 16000)
        self.handler = self._make_handler(terminal_store=store)
        self.bind("s-1")

        self.send("/last")

        self.assertIn("已截断", self.transport.last_text())
        self.assertLess(len(self.transport.last_text()), 16000)


class LastHistoryFallbackTests(AppHandlerTestCase):
    """Store miss -> project the newest verifiable terminal card from history."""

    def _empty_store(self) -> TerminalResultStore:
        return TerminalResultStore(self.data_dir)

    def _history_card(self, card: dict, *, message_id: str = "om_t1"):
        return SimpleNamespace(
            message_id=message_id,
            msg_type="interactive",
            sender=SimpleNamespace(sender_type="app", id="cli_kite"),
            body=SimpleNamespace(content=json.dumps(card, ensure_ascii=False)),
        )

    def test_last_store_miss_projects_terminal_card_from_history(self) -> None:
        self.handler = self._make_handler(terminal_store=self._empty_store())
        self.bind("s-1")
        self.transport.history_items = [
            self._history_card(
                build_terminal_card(
                    outcome="completed", text="历史终态", terminal_result_id="p-1"
                )
            )
        ]

        self.send("/last")

        self.assertEqual(self.transport.last_text(), "历史终态")
        self.assertEqual(len(self.transport.list_messages_calls), 1)
        call = self.transport.list_messages_calls[0]
        self.assertEqual(call["chat_id"], CHAT_ID)
        self.assertEqual(call["sort_type"], "ByCreateTimeDesc")
        # Without this, Feishu returns flattened re-renders (no element ids)
        # and every projection is unverifiable (audit H1).
        self.assertEqual(call["card_msg_content_type"], "user_card_content")

    def test_last_store_hit_wins_over_history(self) -> None:
        store = TerminalResultStore(self.data_dir)
        store.upsert(
            TerminalResultRecord(
                message_id="om_t0",
                execution_message_id="",
                final_reply_text="本地终态",
                recorded_at=1.0,
                terminal_result_id="p-0",
                session_id="s-1",
            )
        )
        self.handler = self._make_handler(terminal_store=store)
        self.bind("s-1")
        self.transport.history_items = [
            self._history_card(
                build_terminal_card(
                    outcome="completed", text="历史终态", terminal_result_id="p-1"
                )
            )
        ]

        self.send("/last")

        self.assertEqual(self.transport.last_text(), "本地终态")
        # The store hit never touches the Feishu history API.
        self.assertEqual(self.transport.list_messages_calls, [])

    def test_last_history_tampered_card_is_skipped_for_older_verifiable(self) -> None:
        self.handler = self._make_handler(terminal_store=self._empty_store())
        self.bind("s-1")
        tampered = build_terminal_card(
            outcome="completed", text="权威原文", terminal_result_id="p-2"
        )
        # Text changed after the element id was stamped: checksum mismatch.
        tampered["body"]["elements"][0]["content"] = (
            f"被篡改的文本{TERMINAL_RESULT_CARD_MARKER}"
        )
        older = build_terminal_card(
            outcome="completed", text="较早终态", terminal_result_id="p-1"
        )
        self.transport.history_items = [
            self._history_card(tampered, message_id="om_new"),
            self._history_card(older, message_id="om_old"),
        ]

        self.send("/last")

        self.assertEqual(self.transport.last_text(), "较早终态")

    def test_last_history_marker_only_card_is_not_exported(self) -> None:
        self.handler = self._make_handler(terminal_store=self._empty_store())
        self.bind("s-1")
        # Feishu history re-rendered shape: marker survives but the element
        # id is gone, so the projection is unverifiable (fail-closed).
        history_rendered = {
            "title": "Kimi 执行结果",
            "elements": [
                [
                    {"tag": "text", "text": "## 结论"},
                    {"tag": "text", "text": f"第一条{TERMINAL_RESULT_CARD_MARKER}"},
                ]
            ],
        }
        self.transport.history_items = [
            SimpleNamespace(
                message_id="om_legacy",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id="cli_kite"),
                body=SimpleNamespace(content=json.dumps(history_rendered, ensure_ascii=False)),
            )
        ]

        self.send("/last")

        self.assertIn("暂无终态答复记录", self.transport.last_text())

    def test_last_history_without_terminal_card_gives_notice(self) -> None:
        self.handler = self._make_handler(terminal_store=self._empty_store())
        self.bind("s-1")
        self.transport.history_items = [
            SimpleNamespace(
                message_id="om_m1",
                msg_type="text",
                sender=SimpleNamespace(sender_type="user", id="ou_user"),
                body=SimpleNamespace(content=json.dumps({"text": "普通消息"})),
            )
        ]

        self.send("/last")

        self.assertIn("暂无终态答复记录", self.transport.last_text())

    def test_last_history_fetch_failure_gives_error_notice(self) -> None:
        self.handler = self._make_handler(terminal_store=self._empty_store())
        self.bind("s-1")
        self.transport.history_error = RuntimeError("code=500, msg=boom")

        self.send("/last")

        self.assertIn("读取聊天记录失败", self.transport.last_text())


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

    def test_new_denied_while_prompt_active(self) -> None:
        self.bind("s-old")
        self.rest.set_prompts("s-old", active="p-1")
        self.send("/new")
        self.assertIn("请先 /abort 或等待完成", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-old")
        # No new session was created upstream.
        self.assertNotIn(("POST", "/sessions"), [(m, p) for m, p, _ in self.rest.calls])

    def test_new_allowed_when_only_queued_prompts(self) -> None:
        self.bind("s-old")
        self.rest.set_prompts("s-old", active=None, queued=("p-2",))
        self.send("/new")
        self.assertIn("已创建并绑定新会话", self.transport.last_text())

    def test_new_preflight_unverifiable_is_fail_closed(self) -> None:
        self.bind("s-old")
        self.rest.prompts_error = KapTransportError("down")
        self.send("/new")
        self.assertIn("无法连接 kap-server", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-old")
        self.assertNotIn(("POST", "/sessions"), [(m, p) for m, p, _ in self.rest.calls])

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

    def test_switch_same_session_shortcut_denied_when_session_shared(self) -> None:
        # Audit R-2 path C: G (all-mode group) /detach -> another chat binds
        # the session (detached does not count as sharing, by design) ->
        # G /switch S. The detached->re-attach shortcut must run the same
        # probes as /attach; flipping attached back on would silently share
        # the session between the all-mode group and the other chat.
        self.bind("s-1", attached=False, chat_id="oc_group")
        self.group_config_store.activate("oc_group", activated_by=ADMIN_OPEN_ID)
        self.group_config_store.set_mode("oc_group", "all")
        self.bind("s-1")  # another attached chat bound the session meanwhile

        self.send("/switch s-1", chat_id="oc_group")

        text = self.transport.last_text()
        self.assertIn("all 模式", text)
        self.assertIn("被拒绝", text)
        binding = self.store.load("oc_group")
        assert binding is not None
        self.assertFalse(binding["attached"])

    def test_switch_same_session_shortcut_denied_when_occupied(self) -> None:
        # Audit R-2, reverse direction (§3.8): while this chat was detached,
        # an all-mode group bound the session — re-switching must not
        # silently join the occupied session.
        self.bind("s-1", attached=False)
        self.bind("s-1", chat_id="oc_group")
        self.group_config_store.activate("oc_group", activated_by=ADMIN_OPEN_ID)
        self.group_config_store.set_mode("oc_group", "all")

        self.send("/switch s-1")

        text = self.transport.last_text()
        self.assertIn("独占", text)
        self.assertIn("被拒绝", text)
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertFalse(binding["attached"])

    def test_switch_card_button_same_session_shortcut_runs_probes(self) -> None:
        # Audit R-2: the /sessions card button reaches the same shortcut —
        # same denial (error toast), and the binding stays detached.
        self.bind("s-1", attached=False, chat_id="oc_group")
        self.group_config_store.activate("oc_group", activated_by=ADMIN_OPEN_ID)
        self.group_config_store.set_mode("oc_group", "all")
        self.bind("s-1")  # another attached chat bound the session meanwhile

        response = self.handler.on_card_action(
            make_card_action(
                {"action": ACTION_SESSION_SWITCH, "session_id": "s-1"},
                chat_id="oc_group",
            )
        )

        self.assertEqual(response.toast_type, "error")
        assert response.toast is not None
        self.assertIn("被拒绝", response.toast)
        binding = self.store.load("oc_group")
        assert binding is not None
        self.assertFalse(binding["attached"])

    def test_switch_denied_while_prompt_active(self) -> None:
        # mvp-scope aligned item 11 (audit M9): same denial as /new —
        # rebinding would strand the in-flight prompt's execution card,
        # terminal result and approval routing.
        self.bind("s-1")
        self.rest.add_session("s-2", title="Beta")
        self.rest.set_prompts("s-1", active="p-1")
        self.send("/switch s-2")
        self.assertIn("请先 /abort 或等待完成", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-1")
        self.assertEqual(self.bound_sessions, [])

    def test_switch_preflight_unverifiable_is_fail_closed(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-2")
        self.rest.prompts_error = KapTransportError("down")
        self.send("/switch s-2")
        self.assertIn("无法连接 kap-server", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-1")

    def test_switch_card_button_denied_while_prompt_active(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-2", title="Beta")
        self.rest.set_prompts("s-1", active="p-1")
        response = self.handler.on_card_action(
            make_card_action({"action": ACTION_SESSION_SWITCH, "session_id": "s-2"})
        )
        self.assertEqual(response.toast_type, "error")
        assert response.toast is not None
        self.assertIn("请先 /abort 或等待完成", response.toast)
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-1")

    def test_detach_pauses_push_and_keeps_binding(self) -> None:
        self.bind("s-1")
        self.send("/detach")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertFalse(binding["attached"])
        self.assertEqual(binding["session_id"], "s-1")
        self.assertIn("已暂停", self.transport.last_text())
        self.assertNotIn("仍在继续", self.transport.last_text())

    def test_detach_notes_active_prompt_continues(self) -> None:
        self.bind("s-1")
        self.rest.set_prompts("s-1", active="p-1")
        self.send("/detach")
        text = self.transport.last_text()
        self.assertIn("已暂停", text)
        self.assertIn("执行中的 prompt 仍在继续，推送已暂停", text)
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertFalse(binding["attached"])

    def test_detach_when_prompts_unverifiable_still_detaches(self) -> None:
        self.bind("s-1")
        self.rest.prompts_error = KapTransportError("down")
        self.send("/detach")
        self.assertIn("已暂停", self.transport.last_text())
        self.assertNotIn("仍在继续", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertFalse(binding["attached"])

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

    def test_attach_denied_when_session_occupied_by_all_mode_group(self) -> None:
        # Audit M10 path A: this chat detached, then an all-mode group bound
        # the same session (detached does not count, by design) — re-attaching
        # must not silently share the occupied session (§3.8 reverse probe).
        self.bind("s-1", attached=False)
        self.bind("s-1", chat_id="oc_group")
        self.group_config_store.activate("oc_group", activated_by=ADMIN_OPEN_ID)
        self.group_config_store.set_mode("oc_group", "all")

        self.send("/attach")

        text = self.transport.last_text()
        self.assertIn("独占", text)
        self.assertIn("被拒绝", text)
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertFalse(binding["attached"])

    def test_attach_denied_for_all_mode_group_when_session_shared(self) -> None:
        # Audit M10: an all-mode group re-attaching while another chat is
        # attached to its session hits the forward probe (§2).
        self.bind("s-1")  # another attached chat shares the session
        self.bind("s-1", attached=False, chat_id="oc_group")
        self.group_config_store.activate("oc_group", activated_by=ADMIN_OPEN_ID)
        self.group_config_store.set_mode("oc_group", "all")

        self.send("/attach", chat_id="oc_group")

        text = self.transport.last_text()
        self.assertIn("all 模式", text)
        self.assertIn("被拒绝", text)
        binding = self.store.load("oc_group")
        assert binding is not None
        self.assertFalse(binding["attached"])

    def test_attach_allowed_when_occupier_deactivated(self) -> None:
        # A deactivated group is inert (§3.8 edge rule): it does not occupy.
        self.bind("s-1", attached=False)
        self.bind("s-1", chat_id="oc_group")
        self.group_config_store.activate("oc_group", activated_by=ADMIN_OPEN_ID)
        self.group_config_store.set_mode("oc_group", "all")
        self.group_config_store.deactivate("oc_group")

        self.send("/attach")

        self.assertIn("已恢复", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertTrue(binding["attached"])


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


class EffortCommandTests(AppHandlerTestCase):
    def test_effort_without_arg_shows_current_unset(self) -> None:
        self.bind("s-1")
        self.send("/effort")
        self.assertIn("当前思考强度：未设置", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_effort_without_arg_shows_current_value(self) -> None:
        self.bind("s-1", effort="low")
        self.send("/effort")
        self.assertIn("当前思考强度：low", self.transport.last_text())

    def test_effort_set_persists(self) -> None:
        self.bind("s-1")
        self.send("/effort xhigh")
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["effort"], "xhigh")
        self.assertIn("已切换为 xhigh", self.transport.last_text())

    def test_effort_invalid_arg_shows_usage(self) -> None:
        self.bind("s-1")
        self.send("/effort turbo")
        self.assertIn("用法", self.transport.last_text())
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["effort"], "")

    def test_effort_same_value_is_idempotent(self) -> None:
        self.bind("s-1", effort="high")
        self.send("/effort high")
        self.assertIn("已是 high", self.transport.last_text())

    def test_effort_without_binding_shows_hint(self) -> None:
        self.send("/effort high")
        self.assertIn("尚未绑定会话", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])

    def test_effort_flows_into_next_prompt(self) -> None:
        self.bind("s-1", effort="max")
        self.rest.add_session("s-1")
        self.send("干活")
        self.assertEqual(self.rest.submissions[0]["body"]["thinking"], "max")

    def test_effort_unset_omits_thinking_on_submit(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("hello")
        self.assertNotIn("thinking", self.rest.submissions[0]["body"])


class GoalCommandTests(AppHandlerTestCase):
    def test_goal_without_arg_shows_unset(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")

        self.send("/goal")

        self.assertIn("当前没有进行中的目标", self.transport.last_text())

    def test_goal_set_posts_profile_and_show_reads_back(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")

        self.send("/goal 修复登录页的崩溃")

        self.assertEqual(
            self.rest.profile_updates,
            [("s-1", {"agent_config": {"goal_objective": "修复登录页的崩溃"}})],
        )
        self.assertIn("已设置目标：修复登录页的崩溃", self.transport.last_text())

        self.send("/goal")
        self.assertIn("当前目标：修复登录页的崩溃（active）", self.transport.last_text())

    def test_goal_off_cancels_via_goal_control(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("/goal 旧目标")

        self.send("/goal off")

        self.assertEqual(
            self.rest.profile_updates[-1],
            ("s-1", {"agent_config": {"goal_control": "cancel"}}),
        )
        self.assertIn("已取消当前目标", self.transport.last_text())
        self.send("/goal")
        self.assertIn("当前没有进行中的目标", self.transport.last_text())

    def test_goal_pause_resume_map_to_goal_control(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("/goal 迁移")

        self.send("/goal pause")
        self.send("/goal resume")

        self.assertEqual(
            [update[1] for update in self.rest.profile_updates],
            [
                {"agent_config": {"goal_objective": "迁移"}},
                {"agent_config": {"goal_control": "pause"}},
                {"agent_config": {"goal_control": "resume"}},
            ],
        )
        self.assertIn("已执行 goal resume", self.transport.last_text())

    def test_goal_upstream_error_surfaces(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.profile_error = KapError(40913, "a goal is already active")

        self.send("/goal 第二个目标")

        self.assertIn("goal 操作失败：a goal is already active", self.transport.last_text())

    def test_goal_archived_session_gets_preflight_error(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1", archived=True)

        self.send("/goal 文本")

        self.assertIn("已被归档", self.transport.last_text())
        self.assertEqual(self.rest.profile_updates, [])

    def test_goal_unbound_replies_with_binding_guidance(self) -> None:
        self.send("/goal 文本")

        self.assertEqual(self.rest.profile_updates, [])

    def test_goal_is_not_persisted_on_the_binding(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")

        self.send("/goal 修复")

        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertNotIn("goal_objective", binding)

    def test_submit_body_does_not_carry_goal_fields(self) -> None:
        self.bind("s-1", effort="high")
        self.rest.add_session("s-1")

        self.send("hello")

        body = self.rest.submissions[0]["body"]
        self.assertEqual(body["thinking"], "high")
        self.assertNotIn("goal_objective", body)
        self.assertNotIn("goal_control", body)


class SessionLifecycleCommandTests(AppHandlerTestCase):
    def test_compact_calls_kap_and_confirms(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("/compact")
        self.assertEqual(self.rest.session_actions, [("s-1", "compact", None)])
        self.assertIn("压缩", self.transport.last_text())

    def test_compact_upstream_error_shows_msg(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.action_error = KapError(40904, "nothing to compact")
        self.send("/compact")
        self.assertIn("压缩失败：nothing to compact", self.transport.last_text())

    def test_compact_kap_unreachable(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.action_error = KapTransportError("timeout")
        self.send("/compact")
        self.assertIn("无法连接 kap-server", self.transport.last_text())

    def test_compact_with_arg_shows_usage(self) -> None:
        self.bind("s-1")
        self.send("/compact now")
        self.assertIn("用法", self.transport.last_text())
        self.assertEqual(self.rest.session_actions, [])

    def test_compact_without_binding_shows_hint(self) -> None:
        self.send("/compact")
        self.assertIn("尚未绑定会话", self.transport.last_text())

    def test_rename_posts_profile_with_title(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1", title="旧标题")
        self.send("/rename 新标题")
        self.assertEqual(self.rest.profile_updates, [("s-1", {"title": "新标题"})])
        self.assertEqual(self.rest.sessions["s-1"]["title"], "新标题")
        self.assertIn("已将会话重命名为「新标题」", self.transport.last_text())

    def test_rename_archived_session_gets_preflight_error(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1", archived=True)

        self.send("/rename 新标题")

        self.assertIn("已被归档", self.transport.last_text())
        self.assertEqual(self.rest.profile_updates, [])

    def test_rename_without_arg_shows_usage(self) -> None:
        self.bind("s-1")
        self.send("/rename")
        self.assertIn("用法", self.transport.last_text())
        self.assertEqual(self.rest.profile_updates, [])

    def test_rename_upstream_error_shows_msg(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.profile_error = KapError(40401, "session not found")
        self.send("/rename x")
        self.assertIn("重命名失败：session not found", self.transport.last_text())

    def test_archive_then_next_message_hits_archived_gate(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("/archive")
        self.assertEqual(self.rest.session_actions, [("s-1", "archive", None)])
        reply = self.transport.last_text()
        self.assertIn("已归档", reply)
        self.assertIn("/restore", reply)
        # §4.7: the next message errors and suggests /sessions; no submit,
        # no implicit recreation, and the binding is kept.
        self.send("hello")
        reply = self.transport.last_text()
        self.assertIn("已被归档", reply)
        self.assertIn("/sessions", reply)
        self.assertEqual(self.rest.submissions, [])
        self.assertEqual(
            [c for c in self.rest.calls if c[0] == "POST" and c[1] == "/sessions"], []
        )
        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-1")

    def test_restore_recovers_the_binding(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1", archived=True)
        self.send("/restore")
        self.assertEqual(self.rest.session_actions, [("s-1", "restore", None)])
        self.assertIn("已恢复", self.transport.last_text())
        self.send("hello")
        self.assertEqual(len(self.rest.submissions), 1)

    def test_archive_upstream_error_shows_msg(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.action_error = KapError(40905, "session is busy")
        self.send("/archive")
        self.assertIn("归档失败：session is busy", self.transport.last_text())

    def test_archive_denied_while_prompt_active(self) -> None:
        # Audit N2-MED-1 (mvp-scope aligned item 15): same check_new denial
        # as /switch (aligned item 11) — upstream archive drains agents and
        # cancels every pending turn, so the in-flight prompt's execution
        # card, terminal result and approval routing would lose visibility.
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.set_prompts("s-1", active="p-1")
        self.send("/archive")
        self.assertIn("请先 /abort 或等待完成", self.transport.last_text())
        self.assertEqual(self.rest.session_actions, [])

    def test_archive_preflight_unverifiable_is_fail_closed(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.prompts_error = KapTransportError("down")
        self.send("/archive")
        self.assertIn("无法连接 kap-server", self.transport.last_text())
        self.assertEqual(self.rest.session_actions, [])

    def test_archive_allowed_with_queued_only(self) -> None:
        # Same check_new semantics as /switch: a queued-but-not-active
        # prompt does not block (audit M9: queued 放行).
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.set_prompts("s-1", queued=("p-2",))
        self.send("/archive")
        self.assertEqual(self.rest.session_actions, [("s-1", "archive", None)])
        self.assertIn("已归档", self.transport.last_text())

    def test_restore_upstream_error_shows_msg(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1", archived=True)
        self.rest.action_error = KapError(40401, "session not found")
        self.send("/restore")
        self.assertIn("恢复失败：session not found", self.transport.last_text())

    def test_archive_restore_without_binding_shows_hint(self) -> None:
        self.send("/archive")
        self.assertIn("尚未绑定会话", self.transport.last_text())
        self.send("/restore")
        self.assertIn("尚未绑定会话", self.transport.last_text())


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

    def test_status_idle_session_never_shows_raw_none(self) -> None:
        # Audit R-1: the upstream wire enum is the STRING 'none' on an idle
        # session; the fourth parse point (KapSessionOps._parse_session) must
        # normalize it like the adapter's three, or /status leaks "待处理交互:none".
        self.bind("s-1")
        self.rest.add_session("s-1", title="Alpha", pending_interaction="none")
        self.send("/status")
        reply = self.transport.last_text()
        self.assertIn("待处理交互：无", reply)
        self.assertNotIn("none", reply)

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


class BtwTests(AppHandlerTestCase):
    def test_btw_starts_agent_once_and_submits_with_agent_id(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")

        self.send("/btw 顺带说一句")
        self.send("/btw 再来一句")

        btw_starts = [a for a in self.rest.session_actions if a[1] == "btw"]
        self.assertEqual(len(btw_starts), 1)  # cached per session
        bodies = [s["body"] for s in self.rest.submissions]
        self.assertEqual(len(bodies), 2)
        for body in bodies:
            self.assertEqual(body["agent_id"], "btw-s-1")
            self.assertEqual(body["permission_mode"], "auto")
            self.assertEqual(body["plan_mode"], False)
        self.assertIn("已发给旁路 agent", self.transport.last_text())
        # Ownership recorded for approval routing, with the sender's identity.
        entry = self.handler.prompt_ownership.entry_of("p-2")
        assert entry is not None
        self.assertEqual(entry.chat_id, CHAT_ID)
        self.assertEqual(entry.sender_open_id, ADMIN_OPEN_ID)

    def test_btw_without_text_shows_usage(self) -> None:
        self.bind("s-1")
        self.send("/btw")
        self.assertIn("/btw", self.transport.last_text())
        self.assertEqual(self.rest.submissions, [])

    def test_btw_unbound_replies_with_binding_guidance(self) -> None:
        self.send("/btw 你好")
        self.assertEqual(self.rest.submissions, [])

    def test_btw_kap_error_surfaces(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.action_error = KapError(50001, "upstream exploded")

        self.send("/btw 顺带说一句")

        self.assertIn("启动旁路 agent 失败", self.transport.last_text())
        self.assertEqual(self.rest.submissions, [])

    def test_btw_agent_not_found_clears_cache_and_retries_once(self) -> None:
        # Audit N3-MED-2: a kap restart drops forked agents; the cached id
        # then dies at submit with agent.not_found (40401-class).
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.btw_agent_ids = ["btw-s-1", "btw-s-1-fresh"]

        self.send("/btw 第一句")
        self.rest.dead_btw_agents.add("btw-s-1")  # the kap restart happens here
        self.send("/btw 第二句")

        bodies = [s["body"] for s in self.rest.submissions]
        # The dead cached id was cleared and the submit retried with a fresh
        # agent — transparently, with a normal ack.
        self.assertEqual([body["agent_id"] for body in bodies], ["btw-s-1", "btw-s-1-fresh"])
        btw_starts = [a for a in self.rest.session_actions if a[1] == "btw"]
        self.assertEqual(len(btw_starts), 2)
        self.assertIn("已发给旁路 agent", self.transport.last_text())

    def test_btw_agent_not_found_retries_only_once(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.rest.btw_agent_ids = ["btw-1", "btw-2", "btw-3"]
        self.rest.dead_btw_agents.update({"btw-1", "btw-2"})

        self.send("/btw 你好")

        # start btw-1 → submit 40401 → clear + start btw-2 → submit 40401 →
        # give up (never a third attempt).
        self.assertEqual(self.rest.submissions, [])
        btw_starts = [a for a in self.rest.session_actions if a[1] == "btw"]
        self.assertEqual(len(btw_starts), 2)
        self.assertIn("提交失败", self.transport.last_text())
        self.assertIn("agent btw-2 does not exist", self.transport.last_text())

    def test_btw_archived_session_gets_preflight_error(self) -> None:
        # Audit N3-MED-3: same §4.7 preflight as the main prompt path — no
        # silent resurrection of an archived session.
        self.bind("s-1")
        self.rest.add_session("s-1", archived=True)

        self.send("/btw 你好")

        self.assertIn("已被归档", self.transport.last_text())
        self.assertIn("KITE 不会自动新建会话", self.transport.last_text())
        self.assertEqual(self.rest.submissions, [])
        self.assertEqual([a for a in self.rest.session_actions if a[1] == "btw"], [])

    def test_btw_vanished_session_gets_preflight_error(self) -> None:
        self.bind("s-ghost")

        self.send("/btw 你好")

        self.assertIn("已不存在", self.transport.last_text())
        self.assertEqual(self.rest.submissions, [])

    def test_btw_detached_chat_is_denied_like_the_main_path(self) -> None:
        # Audit N3-MED-4: a detached chat must not run invisible work.
        self.bind("s-1", attached=False)
        self.rest.add_session("s-1")

        self.send("/btw 你好")

        self.assertEqual(
            self.transport.last_text(),
            "当前会话已暂停推送（/detach 状态），消息未提交。发送 /attach 恢复后再继续。",
        )
        self.assertEqual(self.rest.submissions, [])
        self.assertEqual([a for a in self.rest.session_actions if a[1] == "btw"], [])


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

    def test_sessions_card_includes_cwd(self) -> None:
        # mvp-scope §2: the /sessions row contract is title/cwd/busy
        # (audit L12).
        self.rest.add_session("s-1", title="Alpha", cwd="/work/alpha")
        self.rest.add_session("s-2", title="NoCwd", cwd=None)
        self.send("/sessions")
        markdown = _card_elements(self.transport.cards[0]["card"])[0]["content"]
        self.assertIn("工作目录：`/work/alpha`", markdown)
        # A session without a cwd renders no workdir line.
        self.assertEqual(markdown.count("工作目录"), 1)

    def test_sessions_sorted_by_recent_activity(self) -> None:
        self.rest.add_session("s-old", title="Old", updated_at="2026-07-01T00:00:00Z")
        self.rest.add_session("s-new", title="New", updated_at="2026-07-25T00:00:00Z")
        self.rest.add_session("s-mid", title="Mid", updated_at="2026-07-10T00:00:00Z")

        self.send("/sessions")

        markdown = _card_elements(self.transport.cards[0]["card"])[0]["content"]
        self.assertLess(markdown.index("New"), markdown.index("Mid"))
        self.assertLess(markdown.index("Mid"), markdown.index("Old"))

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
            attachment_store=PendingAttachmentStore(self.data_dir),
            group_config_store=self.group_config_store,
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
            attachment_store=PendingAttachmentStore(self.data_dir),
            group_config_store=self.group_config_store,
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
    """Image attachment inbound wiring (docs/contracts/images.md §2, §4).

    The bound session's cwd must be a real directory for staging, so these
    tests point the session at a temp work dir.
    """

    def setUp(self) -> None:
        super().setUp()
        self.work_dir = self.data_dir / "work"
        self.work_dir.mkdir()

    def _bind_with_real_cwd(self, session_id: str = "s-1") -> None:
        self.rest.add_session(session_id, cwd=str(self.work_dir))
        self.bind(session_id)

    def _staged_files(self) -> list[pathlib.Path]:
        stage_dir = self.work_dir / "_feishu_attachments"
        if not stage_dir.is_dir():
            return []
        return sorted(path for path in stage_dir.iterdir() if not path.name.startswith("."))

    def test_attachment_from_non_admin_is_rejected(self) -> None:
        self.handler.on_attachment(make_attachment(sender="ou_stranger"))
        self.assertIn("仅对管理员开放", self.transport.last_text())
        self.assertEqual(self.attachment_store.list_all(), ())

    def test_image_without_binding_gets_bind_guidance(self) -> None:
        self.handler.on_attachment(make_attachment())
        self.assertIn("尚未绑定会话", self.transport.last_text())
        self.assertEqual(self.transport.replies[-1]["parent_message_id"], "om_att")
        # Nothing downloaded, nothing staged, no record.
        self.assertEqual(self.transport.downloads, [])
        self.assertEqual(self._staged_files(), [])
        self.assertEqual(self.attachment_store.list_all(), ())

    def test_unsupported_type_gets_explicit_rejection(self) -> None:
        self._bind_with_real_cwd()
        self.handler.on_attachment(make_attachment(attachment_type="file"))
        self.assertIn("暂不支持文件附件", self.transport.last_text())
        self.assertEqual(self.transport.downloads, [])
        self.assertEqual(self.attachment_store.list_all(), ())

    def test_image_stages_into_session_cwd_and_acks_saved(self) -> None:
        self._bind_with_real_cwd()
        self.handler.on_attachment(make_attachment())
        self.assertIn("已保存，发送文字即可附带", self.transport.last_text())
        self.assertEqual(self.transport.downloads, [("om_att", "img_key", "image")])
        staged = self._staged_files()
        self.assertEqual(len(staged), 1)
        self.assertTrue(staged[0].name.endswith("-photo.png"))
        self.assertEqual(staged[0].read_bytes(), b"\x89PNG-fake-image-bytes")
        records = self.attachment_store.list_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].local_path, str(staged[0].resolve()))
        self.assertEqual(records[0].media_type, "image/png")
        self.assertGreater(records[0].expires_at, records[0].created_at)

    def test_text_prompt_consumes_pending_image_as_native_parts(self) -> None:
        self._bind_with_real_cwd()
        self.handler.on_attachment(make_attachment())
        staged_path = self._staged_files()[0]

        self.send("帮我看看这张图")

        submission = self.rest.submissions[-1]
        content = submission["body"]["content"]
        self.assertEqual(len(content), 2)
        # Text part: composed context (staged path) + the user text.
        self.assertEqual(content[0]["type"], "text")
        self.assertIn(str(staged_path), content[0]["text"])
        self.assertIn("photo.png", content[0]["text"])
        self.assertIn("用户请求：\n帮我看看这张图", content[0]["text"])
        # Native image part: base64 source (kap imageSourceSchema).
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(content[1]["source"]["kind"], "base64")
        self.assertEqual(content[1]["source"]["media_type"], "image/png")
        self.assertEqual(
            base64.b64decode(content[1]["source"]["data"]),
            b"\x89PNG-fake-image-bytes",
        )
        # Consume-once: the record is gone and the staged file is deleted.
        self.assertEqual(self.attachment_store.list_all(), ())
        self.assertEqual(self._staged_files(), [])
        self.assertIn("附带 1 张图片", self.transport.last_text())

    def test_submit_failure_restores_pending_image_for_retry(self) -> None:
        self._bind_with_real_cwd()
        self.handler.on_attachment(make_attachment())
        self.rest.submit_error = KapError(50001, "kap exploded")

        self.send("看看这张图")

        self.assertIn("提交失败", self.transport.last_text())
        # Restored: the record and the staged file survive the failure.
        records = self.attachment_store.list_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(len(self._staged_files()), 1)

        self.rest.submit_error = None
        self.send("再看看")

        submission = self.rest.submissions[-1]
        kinds = [part["type"] for part in submission["body"]["content"]]
        self.assertEqual(kinds, ["text", "image"])
        self.assertEqual(self.attachment_store.list_all(), ())
        self.assertEqual(self._staged_files(), [])

    def test_expired_attachment_blocks_prompt_and_is_swept(self) -> None:
        self._bind_with_real_cwd()
        handler = self._make_handler(
            config={
                "admin_open_ids": [ADMIN_OPEN_ID],
                "default_working_dir": DEFAULT_CWD,
                "attachment_ttl_seconds": 1,
            }
        )
        handler.on_attachment(make_attachment())
        self.assertEqual(len(self._staged_files()), 1)
        time.sleep(1.2)

        handler.on_message(make_message("看看这张图"))

        self.assertIn("附件已过期，请重新发送", self.transport.last_text())
        # Blocked fail-closed: nothing was submitted.
        self.assertEqual(self.rest.submissions, [])
        # Expired record + staged file are swept.
        self.assertEqual(self.attachment_store.list_all(), ())
        self.assertEqual(self._staged_files(), [])


# ---------------------------------------------------------------------------
# Merge-forward aggregation (p2p + the mention_only group drop; the
# assistant/all group cells live in test_group_chat.py, §3.7)
# ---------------------------------------------------------------------------


class MergeForwardTests(AppHandlerTestCase):
    def _bind(self) -> None:
        self.rest.add_session("s-1")
        self.bind("s-1")

    def test_p2p_admin_flow_end_to_end(self) -> None:
        self._bind()
        handler = self._make_handler(
            names=IdentityNames(lambda open_id: {"ou_alice": "Alice"}.get(open_id))
        )
        self.transport.merge_forward_items = [
            make_forward_item("om_c1", text="看看这段对话", sender_id="ou_alice"),
        ]

        handler.on_merge_forward(make_merge_forward(message_id="om_fwd"))

        # Buffered, not yet submitted: the window is still open.
        self.assertEqual(self.transport.merge_forward_fetches, ["om_fwd"])
        self.assertEqual(len(self.forward_timers), 1)
        self.assertEqual(self.rest.submissions, [])

        self.forward_timers[-1].fire()

        self.assertEqual(len(self.rest.submissions), 1)
        submission = self.rest.submissions[0]
        self.assertEqual(submission["session_id"], "s-1")
        content = submission["body"]["content"]
        self.assertEqual([part["type"] for part in content], ["text"])
        text = content[0]["text"]
        self.assertIn("<forwarded_messages>", text)
        self.assertIn("</forwarded_messages>", text)
        self.assertIn("Alice:", text)
        self.assertIn("看看这段对话", text)
        # The ack threads to the original merge_forward message.
        self.assertIn("已提交", self.transport.last_text())
        self.assertEqual(self.transport.replies[-1]["parent_message_id"], "om_fwd")
        # Ownership is recorded like any Feishu-originated prompt.
        entry = handler.prompt_ownership.entry_of(submission["prompt_id"])
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.chat_id, CHAT_ID)
        self.assertEqual(entry.sender_open_id, ADMIN_OPEN_ID)

    def test_two_bundles_merge_into_one_prompt(self) -> None:
        self._bind()
        self.transport.merge_forward_items = [make_forward_item("om_c1", text="第一段")]

        self.handler.on_merge_forward(make_merge_forward(message_id="om_fwd1"))
        self.transport.merge_forward_items = [make_forward_item("om_c2", text="第二段")]
        self.handler.on_merge_forward(make_merge_forward(message_id="om_fwd2"))

        self.assertEqual(len(self.forward_timers), 2)
        self.assertTrue(self.forward_timers[0].cancelled)

        self.forward_timers[-1].fire()

        self.assertEqual(len(self.rest.submissions), 1)
        text = self.rest.submissions[0]["body"]["content"][0]["text"]
        self.assertIn("第一段", text)
        self.assertIn("第二段", text)
        self.assertEqual(self.transport.replies[-1]["parent_message_id"], "om_fwd2")

    def test_next_text_claims_stash_into_one_merged_prompt(self) -> None:
        # FOCUS stash-claim (audit M12): the comment sent right after a
        # forward claims the stashed transcript — ONE prompt with the
        # transcript FIRST, so the instruction never runs without it.
        self._bind()
        self.transport.merge_forward_items = [
            make_forward_item("om_c1", text="看看这段对话", sender_id="ou_alice"),
        ]
        self.handler.on_merge_forward(make_merge_forward(message_id="om_fwd"))
        self.assertEqual(len(self.forward_timers), 1)
        self.assertEqual(self.rest.submissions, [])

        self.send("总结一下这段对话")

        self.assertEqual(len(self.rest.submissions), 1)
        text = self.rest.submissions[0]["body"]["content"][0]["text"]
        self.assertIn("<forwarded_messages>", text)
        self.assertLess(text.index("看看这段对话"), text.index("总结一下这段对话"))
        # The window was claimed: its timer is cancelled and a late fire
        # submits nothing (no second, transcript-only prompt).
        self.assertTrue(self.forward_timers[-1].cancelled)
        self.forward_timers[-1].fire()
        self.assertEqual(len(self.rest.submissions), 1)
        # The ack threads to the claiming comment, not the forward.
        self.assertEqual(self.transport.replies[-1]["parent_message_id"], "om_1")
        # Ownership records the claiming sender like any text prompt.
        entry = self.handler.prompt_ownership.entry_of(self.rest.submissions[0]["prompt_id"])
        assert entry is not None
        self.assertEqual(entry.sender_open_id, ADMIN_OPEN_ID)

    def test_unclaimed_window_still_submits_transcript_alone(self) -> None:
        # The other half of the semantics: with no claiming text inside the
        # window, the transcript flushes on its own.
        self._bind()
        self.transport.merge_forward_items = [make_forward_item("om_c1", text="转发的内容")]

        self.handler.on_merge_forward(make_merge_forward(message_id="om_fwd"))
        self.forward_timers[-1].fire()

        self.assertEqual(len(self.rest.submissions), 1)
        text = self.rest.submissions[0]["body"]["content"][0]["text"]
        self.assertIn("<forwarded_messages>", text)
        self.assertIn("转发的内容", text)

    def test_group_merge_forward_dropped_without_prompt_path(self) -> None:
        self.group_config_store.activate("oc_group", activated_by=ADMIN_OPEN_ID)
        self.rest.add_session("s-1")
        self.bind("s-1", chat_id="oc_group")
        self.transport.merge_forward_items = [make_forward_item("om_c1", text="群里的转发")]

        self.handler.on_merge_forward(
            make_merge_forward(chat_id="oc_group", chat_type="group", message_id="om_fwdg")
        )

        # mention_only: a forward carries no @mention, so it is dropped at
        # ingress — no fetch, no buffer, no prompt, no reply.
        self.assertEqual(self.transport.merge_forward_fetches, [])
        self.assertEqual(self.forward_timers, [])
        self.assertEqual(self.rest.calls, [])
        self.assertEqual(self.transport.replies, [])

    def test_non_admin_merge_forward_rejected(self) -> None:
        self.handler.on_merge_forward(make_merge_forward(sender="ou_stranger"))

        self.assertIn("仅对管理员开放", self.transport.last_text())
        self.assertEqual(self.transport.merge_forward_fetches, [])
        self.assertEqual(self.forward_timers, [])
        self.assertEqual(self.rest.calls, [])

    def test_fetch_failure_replies_and_buffers_nothing(self) -> None:
        self.transport.merge_forward_error = RuntimeError("network down")

        self.handler.on_merge_forward(make_merge_forward())

        self.assertIn("获取合并转发内容失败", self.transport.last_text())
        self.assertEqual(self.forward_timers, [])
        self.assertEqual(self.rest.submissions, [])

    def test_empty_items_replies_and_buffers_nothing(self) -> None:
        self.transport.merge_forward_items = []

        self.handler.on_merge_forward(make_merge_forward())

        self.assertIn("未包含可识别的内容", self.transport.last_text())
        self.assertEqual(self.forward_timers, [])
        self.assertEqual(self.rest.submissions, [])

    def test_unrenderable_items_reply_and_buffer_nothing(self) -> None:
        # Audit L15: the fetch succeeded (non-empty items) but nothing in
        # the bundle is renderable — here the only item is the bundle root
        # itself, which the renderer skips — so the user gets the same
        # explicit notice instead of silence.
        self.transport.merge_forward_items = [make_forward_item("om_fwd")]

        self.handler.on_merge_forward(make_merge_forward(message_id="om_fwd"))

        self.assertIn("未包含可识别的内容", self.transport.last_text())
        self.assertEqual(self.forward_timers, [])
        self.assertEqual(self.rest.submissions, [])

    def test_close_cancels_pending_window(self) -> None:
        self._bind()
        self.transport.merge_forward_items = [make_forward_item("om_c1", text="x")]

        self.handler.on_merge_forward(make_merge_forward())
        self.assertEqual(len(self.forward_timers), 1)

        self.handler.close()

        self.assertTrue(self.forward_timers[-1].cancelled)
        # A late fire after close finds no pending entry and submits nothing.
        self.forward_timers[-1].fire()
        self.assertEqual(self.rest.submissions, [])


class BareSlashTests(AppHandlerTestCase):
    """Audit L13 (FOCUS parity): a bare ``/`` (or ``/ text``) is answered as
    an unknown command, never submitted as a prompt."""

    def test_bare_slash_is_an_unknown_command_not_a_prompt(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("/")
        self.assertIn("未知命令 `/`", self.transport.last_text())
        self.assertEqual(self.rest.submissions, [])

    def test_slash_space_text_is_an_unknown_command_not_a_prompt(self) -> None:
        self.bind("s-1")
        self.rest.add_session("s-1")
        self.send("/ 帮我总结一下")
        self.assertIn("未知命令 `/`", self.transport.last_text())
        self.assertEqual(self.rest.submissions, [])

    def test_non_admin_bare_slash_gets_the_identity_gate(self) -> None:
        self.send("/", sender="ou_stranger")
        self.assertIn("仅对管理员开放", self.transport.last_text())
        self.assertEqual(self.rest.calls, [])


class ChatUnavailableTests(AppHandlerTestCase):
    """Audit L18: bot removed from a group / group disbanded deactivates the
    group config (fail-closed to silence); binding and log file survive."""

    def test_activated_group_is_deactivated_when_the_chat_dies(self) -> None:
        self.bind("s-1", chat_id="oc_group")
        self.group_config_store.activate("oc_group", activated_by=ADMIN_OPEN_ID)
        self.group_config_store.set_mode("oc_group", GROUP_MODE_ASSISTANT)
        log_store = GroupLogStore(self.data_dir)
        log_store.append(
            "oc_group",
            {
                "message_id": "om_x",
                "created_at": 1,
                "sender_open_id": "ou_member",
                "sender_type": "user",
                "sender_name": "成员",
                "msg_type": "text",
                "text": "hello",
            },
        )
        log_path = log_store.log_path("oc_group")
        self.assertTrue(log_path.exists())

        self.handler.on_chat_unavailable("oc_group", reason="bot_removed")

        config = self.group_config_store.load("oc_group")
        assert config is not None
        self.assertFalse(config["activated"])
        # The mode preference is kept (store convention), the log file is
        # kept, and the binding is kept — only the activation switch flips.
        self.assertEqual(config["mode"], GROUP_MODE_ASSISTANT)
        self.assertTrue(log_path.exists())
        self.assertIsNotNone(self.store.load("oc_group"))

    def test_chat_unavailable_without_group_config_is_a_noop(self) -> None:
        self.handler.on_chat_unavailable("oc_group", reason="disbanded")
        self.assertIsNone(self.group_config_store.load("oc_group"))

    def test_member_messages_stop_after_bot_removed(self) -> None:
        # The revived-bot scenario: the deactivated group ignores member
        # messages until an admin re-activates it explicitly.
        self.bind("s-1", chat_id="oc_group")
        self.group_config_store.activate("oc_group", activated_by=ADMIN_OPEN_ID)
        self.handler.on_chat_unavailable("oc_group", reason="disbanded")

        self.handler.on_message(
            dataclasses.replace(
                make_message("大家好", chat_id="oc_group", sender="ou_member"),
                chat_type="group",
                bot_mentioned=True,
            )
        )
        # Not activated anymore: the member @message is ignored (no prompt).
        self.assertEqual(self.rest.submissions, [])


if __name__ == "__main__":
    unittest.main()
