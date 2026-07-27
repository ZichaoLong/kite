"""Outbound event pipeline contract tests.

Fakes stand in for the Feishu transport (records sends/patches), the kap REST
client (scriptable prompts/snapshot/interactions), and timers (manual fire).
A real BindingStore / TerminalResultStore / EventCursorStore in a temp dir and
a real RuntimeLoop are used, so the loop-serialization discipline is exercised
for real. Events are fed as wire-shaped KapEvent payloads so the adapter's
durable-event normalization is covered end to end.
"""

from __future__ import annotations

import json
import pathlib
import re
import tempfile
import unittest
from unittest import mock

import kite.event_pipeline as event_pipeline_module
from kite import cards
from kite.message_patch_result import MessagePatchResult
from kite.adapters.kap_server import (
    ApprovalRequestView,
    AssistantDelta,
    KapError,
    KapErrorFrame,
    KapEvent,
    KapTransportError,
    PromptQueueState,
    QuestionItemView,
    QuestionOptionView,
    QuestionRequestView,
    ResyncRequest,
    SessionSnapshot,
)
from kite.event_pipeline import (
    EventPipeline,
    OutboundAppHandler,
    SwappableKapRest,
    TimerHandle,
    WsSubscriptionHook,
    _MAX_TOOL_LINES,
    _PendingApproval,
)
from kite.feishu_transport import CardAction, InboundMessage
from kite.identity_names import IdentityNames
from kite.prompt_ownership import PromptOwnership
from kite.runtime_loop import RuntimeLoop
from kite.stores.binding_store import BindingStore
from kite.stores.event_cursor_store import EventCursorStore
from kite.stores.group_config_store import GroupConfigStore
from kite.stores.pending_attachment_store import PendingAttachmentStore
from kite.stores.terminal_result_store import TerminalResultStore

ADMIN_OPEN_ID = "ou_admin"
OTHER_OPEN_ID = "ou_other_admin"
CHAT_ID = "oc_chat"
CHAT_ID_2 = "oc_chat_2"
SESSION_ID = "s-1"
INIT_TOKEN = "test-init-token"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.patches: list[dict] = []
        self.replies: list[dict] = []
        self.fail_sends = False
        self._counter = 0
        # Every patch attempt (applied or rejected), for retry accounting.
        self.patch_attempts: list[str] = []
        # When set, patch contents containing this marker are rejected with
        # MessagePatchResult.invalid_content() (Feishu 230099 stand-in).
        self.reject_patch_containing: str | None = None

    def send_message_get_id(self, chat_id: str, msg_type: str, content: str):
        if self.fail_sends:
            return None
        self._counter += 1
        message_id = f"om_{self._counter}"
        self.sent.append(
            {
                "chat_id": chat_id,
                "msg_type": msg_type,
                "content": json.loads(content),
                "message_id": message_id,
            }
        )
        return message_id

    def patch_message_result(self, message_id: str, content: str) -> MessagePatchResult:
        self.patch_attempts.append(content)
        # Content arrives json.dumps-escaped; match markers against the
        # unescaped rendering too so CJK markers work.
        rendered = json.dumps(json.loads(content), ensure_ascii=False)
        if self.reject_patch_containing and (
            self.reject_patch_containing in content
            or self.reject_patch_containing in rendered
        ):
            return MessagePatchResult.invalid_content()
        self.patches.append({"message_id": message_id, "content": json.loads(content)})
        return MessagePatchResult.success()

    def patch_message(self, message_id: str, content: str) -> bool:
        return self.patch_message_result(message_id, content).ok

    def reply(self, chat_id: str, text: str, *, parent_message_id: str = "", reply_in_thread: bool = False) -> bool:
        self.replies.append({"chat_id": chat_id, "text": text})
        return True

    def reply_card(self, chat_id: str, card: dict, *, parent_message_id: str = "", reply_in_thread: bool = False) -> None:
        self.send_message_get_id(chat_id, "interactive", json.dumps(card))

    # -- helpers --------------------------------------------------------------

    def cards_to(self, chat_id: str) -> list[dict]:
        return [item for item in self.sent if item["chat_id"] == chat_id]

    def texts_to(self, chat_id: str) -> list[str]:
        return [item["text"] for item in self.replies if item["chat_id"] == chat_id]

    def patches_to(self, message_id: str) -> list[dict]:
        return [item["content"] for item in self.patches if item["message_id"] == message_id]


class FakeInteractionRest:
    """Scriptable stand-in for the kap REST surface the pipeline uses."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.prompt_states: dict[str, PromptQueueState] = {}
        self.snapshots: dict[str, object] = {}
        self.assistant_text = "最终答复文本"
        # When set, returned as the messages page verbatim (must be
        # newest-first, mirroring upstream's ordering contract).
        self.assistant_items: list[dict] | None = None
        self.approval_resolutions: list[dict] = []
        self.question_answers: list[dict] = []
        self.dismissals: list[tuple[str, str]] = []
        self.submissions: list[dict] = []
        self.resolve_error: Exception | None = None
        self.answer_error: Exception | None = None
        self.dismiss_error: Exception | None = None
        self.prompts_error: Exception | None = None
        self.messages_error: Exception | None = None
        self.abort_error: Exception | None = None
        self.aborts: list[tuple[str, str]] = []
        # Fired inside the approval-resolve POST, before it returns (used to
        # re-enter the handler mid-flight for the click-guard tests).
        self.resolve_hook: object = None
        self._prompt_counter = 0

    # -- scripting helpers ----------------------------------------------------

    def add_session(self, session_id: str, *, title: str = "测试会话") -> None:
        self.sessions[session_id] = {
            "id": session_id,
            "title": title,
            "busy": False,
            "pending_interaction": None,
            "archived": False,
            "metadata": {"cwd": "/work"},
        }

    def set_prompts(self, session_id: str, *, active: str | None = None, queued: tuple[str, ...] = ()) -> None:
        self.prompt_states[session_id] = PromptQueueState(
            active_prompt_id=active, queued_prompt_ids=queued
        )

    # -- KapRestClient surface ------------------------------------------------

    def call(self, method: str, path: str, body: object = None) -> object:
        if method == "POST" and path == "/sessions":
            payload = body if isinstance(body, dict) else {}
            session_id = f"s-new-{len(self.sessions) + 1}"
            self.add_session(session_id, title=str(payload.get("title") or "新会话"))
            return self.sessions[session_id]
        match = re.fullmatch(r"/sessions/([^/]+)", path)
        if method == "GET" and match:
            session = self.sessions.get(match.group(1))
            if session is None:
                raise KapError(40401, "session not found")
            return session
        match = re.fullmatch(r"/sessions/([^/]+)/messages", path.split("?")[0])
        if method == "GET" and match:
            if self.messages_error is not None:
                raise self.messages_error
            if self.assistant_items is not None:
                return {"items": list(self.assistant_items), "has_more": False}
            text = self.assistant_text
            return {
                "items": [
                    {
                        "id": "m-1",
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}] if text else [],
                    }
                ]
                if text
                else [],
                "has_more": False,
            }
        match = re.fullmatch(r"/sessions/([^/]+)/approvals/([^/]+)", path)
        if method == "POST" and match:
            if self.resolve_error is not None:
                raise self.resolve_error
            if self.resolve_hook is not None:
                self.resolve_hook()  # type: ignore[operator]
            self.approval_resolutions.append(
                {"session_id": match.group(1), "approval_id": match.group(2), "body": body}
            )
            return {"resolved": True, "resolved_at": "2026-01-01T00:00:00Z"}
        match = re.fullmatch(r"/sessions/([^/]+)/questions/([^/]+):dismiss", path)
        if method == "POST" and match:
            if self.dismiss_error is not None:
                raise self.dismiss_error
            self.dismissals.append((match.group(1), match.group(2)))
            # Upstream quirk: the dismiss success envelope carries 40909.
            raise KapError(40909, "question dismissed")
        match = re.fullmatch(r"/sessions/([^/]+)/questions/([^/]+)", path)
        if method == "POST" and match:
            if self.answer_error is not None:
                raise self.answer_error
            self.question_answers.append(
                {"session_id": match.group(1), "question_id": match.group(2), "body": body}
            )
            return {"resolved": True, "resolved_at": "2026-01-01T00:00:00Z"}
        match = re.fullmatch(r"/sessions/([^/]+):btw", path)
        if method == "POST" and match:
            session = self.sessions.get(match.group(1))
            if session is None:
                raise KapError(40401, "session not found")
            return {"agent_id": f"btw-{match.group(1)}"}
        match = re.fullmatch(r"/sessions/([^/]+)/prompts", path)
        if method == "POST" and match:
            self._prompt_counter += 1
            prompt_id = f"p-new-{self._prompt_counter}"
            self.submissions.append(
                {"session_id": match.group(1), "body": body, "prompt_id": prompt_id}
            )
            return {
                "prompt_id": prompt_id,
                "user_message_id": f"um-{prompt_id}",
                "status": "running",
                "content": [],
                "created_at": "2026-01-01T00:00:00Z",
            }
        match = re.fullmatch(r"/sessions/([^/]+)/prompts/([^/]+):abort", path)
        if method == "POST" and match:
            if self.abort_error is not None:
                raise self.abort_error
            self.aborts.append((match.group(1), match.group(2)))
            return {"aborted": True}
        raise AssertionError(f"unexpected kap call: {method} {path}")

    def get(self, path: str) -> object:
        return self.call("GET", path)

    def post(self, path: str, body: object = None) -> object:
        return self.call("POST", path, body)

    def list_sessions(self) -> list:
        return []

    def get_prompts(self, session_id: str) -> PromptQueueState:
        if self.prompts_error is not None:
            raise self.prompts_error
        return self.prompt_states.get(session_id, PromptQueueState(None, ()))

    def get_snapshot(self, session_id: str) -> SessionSnapshot:
        snapshot = self.snapshots.get(session_id)
        if snapshot is None:
            raise KapError(40401, "session not found")
        if isinstance(snapshot, Exception):
            raise snapshot
        return snapshot


class ManualTimer(TimerHandle):
    def __init__(self, delay: float, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback()


class ManualTimerFactory:
    def __init__(self) -> None:
        self.created: list[ManualTimer] = []

    def __call__(self, delay: float, callback) -> ManualTimer:
        timer = ManualTimer(delay, callback)
        self.created.append(timer)
        return timer

    @property
    def live(self) -> list[ManualTimer]:
        return [timer for timer in self.created if not timer.cancelled]

    def fire(self, index: int = -1) -> None:
        self.created[index].fire()


class FakeWsClient:
    def __init__(self) -> None:
        self.subscriptions: list[str] = []
        self.rebuild_resubscribes: list[str] = []

    def subscribe(self, session_id: str):
        self.subscriptions.append(session_id)
        return {}

    def resubscribe_after_rebuild(self, session_id: str) -> bool:
        self.rebuild_resubscribes.append(session_id)
        return True


class ImmediateDispatcher:
    """Synchronous CardPatchDispatcher stand-in (PipelineTestCase).

    The frozen-card patch path routes through the dispatcher API (audit
    R-3); this fake applies render+patch inline on submit and fires
    ``on_result`` with the outcome, so existing synchronous patch
    assertions keep working while tests can assert what flowed through the
    dispatcher. The real dispatcher's coalescing/retry semantics live in
    test_patch_dispatcher.
    """

    def __init__(self, patch) -> None:
        self._patch = patch
        # (message_id, content) actually patched, in order.
        self.applied: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.shutdown_called = False

    def submit(self, message_id: str, render, on_result=None) -> None:
        content = render()
        if content is None:
            if on_result is not None:
                on_result(MessagePatchResult.success())
            return
        result = self._patch(message_id, content)
        self.applied.append((message_id, content))
        if on_result is not None:
            on_result(result)

    def cancel(self, message_id: str) -> None:
        self.cancelled.append(message_id)

    def shutdown(self) -> None:
        self.shutdown_called = True

    def applied_to(self, message_id: str) -> list[str]:
        return [content for mid, content in self.applied if mid == message_id]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def kap_event(type_: str, payload: dict, *, session_id: str = SESSION_ID, seq: int = 1) -> KapEvent:
    return KapEvent(
        type=type_,
        session_id=session_id,
        seq=seq,
        epoch="e1",
        volatile=False,
        offset=None,
        timestamp="2026-01-01T00:00:00Z",
        payload=payload,
    )


def turn_started(*, turn_id: int = 1, prompt: str = "做点事", session_id: str = SESSION_ID) -> KapEvent:
    return kap_event(
        "turn.started",
        {"type": "turn.started", "turnId": turn_id, "origin": {"kind": "user"}, "prompt": prompt},
        session_id=session_id,
    )


def approval_requested(
    *, approval_id: str = "a-1", turn_id: int = 1, session_id: str = SESSION_ID
) -> KapEvent:
    return kap_event(
        "event.approval.requested",
        {
            "approval_id": approval_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "tool_call_id": "tc-1",
            "tool_name": "Bash",
            "action": "execute",
            "tool_input_display": {"kind": "command", "command": "rm -rf build/"},
            "created_at": "2026-01-01T00:00:00Z",
            "expires_at": "2026-01-02T00:00:00Z",
        },
        session_id=session_id,
    )


def question_requested(
    *,
    question_id: str = "q-1",
    turn_id: int = 1,
    session_id: str = SESSION_ID,
    questions: list | None = None,
) -> KapEvent:
    if questions is None:
        questions = [
            {
                "id": "q_0",
                "question": "部署到哪个环境？",
                "header": "环境",
                "options": [
                    {"id": "opt_0_0", "label": "开发", "description": "dev"},
                    {"id": "opt_0_1", "label": "生产"},
                ],
                "allow_other": True,
            }
        ]
    return kap_event(
        "event.question.requested",
        {
            "question_id": question_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "questions": questions,
            "created_at": "2026-01-01T00:00:00Z",
        },
        session_id=session_id,
    )


def make_snapshot(
    *,
    as_of_seq: int = 10,
    epoch: str = "e1",
    busy: bool = True,
    pending_interaction: str | None = None,
    current_prompt_id: str | None = None,
    in_flight: bool = False,
    turn_id: int | None = None,
    pending_approvals: tuple[ApprovalRequestView, ...] = (),
    pending_questions: tuple[QuestionRequestView, ...] = (),
) -> SessionSnapshot:
    return SessionSnapshot(
        as_of_seq=as_of_seq,
        epoch=epoch,
        busy=busy,
        pending_interaction=pending_interaction,
        current_prompt_id=current_prompt_id,
        in_flight=in_flight,
        pending_approval_ids=tuple(view.approval_id for view in pending_approvals),
        pending_question_ids=tuple(view.question_id for view in pending_questions),
        in_flight_turn_id=turn_id,
        pending_approvals=pending_approvals,
        pending_questions=pending_questions,
    )


def make_message(
    text: str,
    *,
    sender: str = ADMIN_OPEN_ID,
    chat_id: str = CHAT_ID,
    message_id: str = "om_in_1",
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
        message_id="om_card_action",
        value=value,
    )


def _collect_buttons(node: object) -> list[dict]:
    if isinstance(node, dict):
        buttons: list[dict] = []
        if node.get("tag") == "button":
            buttons.append(node)
        for value in node.values():
            buttons.extend(_collect_buttons(value))
        return buttons
    if isinstance(node, list):
        buttons = []
        for item in node:
            buttons.extend(_collect_buttons(item))
        return buttons
    return []


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = pathlib.Path(self._tmp.name)
        self.store = BindingStore(self.data_dir)
        self.terminal_store = TerminalResultStore(self.data_dir)
        self.cursor_store = EventCursorStore(self.data_dir)
        self.transport = FakeTransport()
        self.rest = FakeInteractionRest()
        self.rest.add_session(SESSION_ID)
        self.loop = RuntimeLoop(name="test-loop")
        self.addCleanup(self.loop.stop)
        self.timers = ManualTimerFactory()
        self.ownership = PromptOwnership()
        self.group_config_store = GroupConfigStore(self.data_dir)
        self.dispatcher = ImmediateDispatcher(self.transport.patch_message_result)
        self.pipeline = EventPipeline(
            transport=self.transport,
            rest=self.rest,
            binding_store=self.store,
            terminal_store=self.terminal_store,
            ownership=self.ownership,
            runtime_loop=self.loop,
            cursor_store=self.cursor_store,
            approval_timeout_seconds=300,
            question_timeout_seconds=300,
            timer_factory=self.timers,
            patch_dispatcher=self.dispatcher,
        )
        self.handler = OutboundAppHandler(
            event_pipeline=self.pipeline,
            transport=self.transport,
            rest=self.rest,
            binding_store=self.store,
            attachment_store=PendingAttachmentStore(self.data_dir),
            group_config_store=self.group_config_store,
            runtime_loop=self.loop,
            config={
                "admin_open_ids": [ADMIN_OPEN_ID, OTHER_OPEN_ID],
                "default_working_dir": "/work",
            },
            init_token=INIT_TOKEN,
            prompt_ownership=self.ownership,
            persist_admins=lambda ids: None,
        )

    # -- helpers ---------------------------------------------------------------

    def flush(self) -> None:
        """Wait until every queued loop task has run."""
        self.loop.call(lambda: None)

    def bind(self, chat_id: str = CHAT_ID, session_id: str = SESSION_ID, *, attached: bool = True) -> None:
        self.store.save(
            chat_id,
            {
                "session_id": session_id,
                "attached": attached,
                "permission_mode": "auto",
                "plan_mode": False,
            },
        )

    def feed(self, event: KapEvent) -> None:
        self.pipeline.handle_event(event)
        self.flush()

    def start_prompt(
        self,
        *,
        chat_id: str = CHAT_ID,
        prompt_id: str = "p-1",
        turn_id: int = 1,
        prompt: str = "做点事",
    ) -> str:
        """Drive turn.started; returns the execution card message id."""
        self.rest.set_prompts(SESSION_ID, active=prompt_id)
        self.feed(turn_started(turn_id=turn_id, prompt=prompt))
        sent = self.transport.cards_to(chat_id)
        assert sent, "expected an execution card"
        return sent[-1]["message_id"]


# ---------------------------------------------------------------------------
# turn.* / tool.call.* -> execution card lifecycle
# ---------------------------------------------------------------------------


class ExecutionCardTests(PipelineTestCase):
    def test_turn_started_creates_card_in_every_attached_chat(self) -> None:
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)
        self.rest.set_prompts(SESSION_ID, active="p-1", queued=("p-2",))

        self.feed(turn_started(prompt="写个脚本"))

        for chat_id in (CHAT_ID, CHAT_ID_2):
            sent = self.transport.cards_to(chat_id)
            self.assertEqual(len(sent), 1)
            content = json.dumps(sent[0]["content"], ensure_ascii=False)
            self.assertIn("写个脚本", content)
            self.assertIn("还有 1 条 prompt 排队中", content)

    def test_detached_and_unbound_chats_get_no_card(self) -> None:
        self.bind(CHAT_ID, attached=False)
        self.bind(CHAT_ID_2)
        self.rest.set_prompts(SESSION_ID, active="p-1")

        self.feed(turn_started())

        self.assertEqual(self.transport.cards_to(CHAT_ID), [])
        self.assertEqual(len(self.transport.cards_to(CHAT_ID_2)), 1)

    def test_turn_started_without_active_prompt_creates_no_card(self) -> None:
        self.bind(CHAT_ID)
        self.rest.set_prompts(SESSION_ID, active=None)

        self.feed(turn_started())

        self.assertEqual(self.transport.cards_to(CHAT_ID), [])

    def test_tool_events_patch_only_the_anchor_card(self) -> None:
        self.bind(CHAT_ID)
        message_id = self.start_prompt()

        self.feed(
            kap_event(
                "tool.call.started",
                {
                    "turnId": 1,
                    "toolCallId": "tc-1",
                    "name": "Bash",
                    "display": {"kind": "command", "command": "ls -la"},
                },
            )
        )
        patches = self.transport.patches_to(message_id)
        self.assertEqual(len(patches), 1)
        self.assertIn("Bash", json.dumps(patches[0], ensure_ascii=False))
        self.assertIn("ls -la", json.dumps(patches[0], ensure_ascii=False))

        # An event for another turn/prompt must not touch the anchored card.
        self.feed(
            kap_event(
                "tool.call.started",
                {"turnId": 99, "toolCallId": "tc-x", "name": "Edit"},
            )
        )
        self.assertEqual(len(self.transport.patches_to(message_id)), 1)

        self.feed(
            kap_event("tool.result", {"turnId": 1, "toolCallId": "tc-1", "isError": False})
        )
        patches = self.transport.patches_to(message_id)
        self.assertEqual(len(patches), 2)
        self.assertIn("✅", json.dumps(patches[-1], ensure_ascii=False))

        self.feed(
            kap_event(
                "tool.call.started",
                {"turnId": 1, "toolCallId": "tc-2", "name": "Bash", "display": {"kind": "command", "command": "false"}},
            )
        )
        self.feed(
            kap_event("tool.result", {"turnId": 1, "toolCallId": "tc-2", "isError": True})
        )
        self.assertIn("❌", json.dumps(self.transport.patches_to(message_id)[-1], ensure_ascii=False))

    def test_turn_ended_sends_terminal_card_and_freezes_execution_card(self) -> None:
        self.bind(CHAT_ID)
        message_id = self.start_prompt()

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(sent), 2)  # execution card + terminal card
        terminal = sent[-1]["content"]
        rendered = json.dumps(terminal, ensure_ascii=False)
        self.assertIn("最终答复文本", rendered)
        # The execution card was patched to frozen-done.
        patches = self.transport.patches_to(message_id)
        self.assertEqual(len(patches), 1)
        self.assertIn("已结束", json.dumps(patches[0], ensure_ascii=False))
        # The terminal text is persisted for /last-style reads.
        records = self.terminal_store.list_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].final_reply_text, "最终答复文本")
        self.assertEqual(records[0].session_id, SESSION_ID)
        # The anchor is gone: later prompt-scoped events touch nothing.
        self.feed(
            kap_event(
                "tool.call.started",
                {"turnId": 1, "toolCallId": "tc-late", "name": "Bash"},
            )
        )
        self.assertEqual(len(self.transport.patches_to(message_id)), 1)

    def test_turn_ended_with_successor_active_does_not_pin_stale_text(self) -> None:
        # Audit L3: attempt-1 of the terminal reconcile used to run before
        # the queue refresh (and after the handler cleared the watermark), so
        # the moved_on guard could never fire for it — the previous prompt's
        # reply got pinned on this prompt's terminal card AND into the store.
        self.bind(CHAT_ID)
        message_id = self.start_prompt()
        # The server already advanced to the next prompt; the session's last
        # assistant text belongs to an earlier prompt.
        self.rest.set_prompts(SESSION_ID, active="p-2")
        self.rest.assistant_text = "上一轮答复"

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        terminal = self.transport.cards_to(CHAT_ID)[-1]["content"]
        self.assertNotIn("上一轮答复", json.dumps(terminal, ensure_ascii=False))
        # The unattributable text must not reach the terminal store either.
        self.assertEqual(self.terminal_store.list_all(), ())
        # The execution card still froze as done.
        patches = self.transport.patches_to(message_id)
        self.assertIn("已结束", json.dumps(patches[-1], ensure_ascii=False))

    def test_orphan_terminal_delivery_is_persisted_for_last(self) -> None:
        # Audit L4: the standalone (orphan) terminal path delivered the card
        # but never upserted the terminal store, so /last lost the result.
        self.bind(CHAT_ID)
        self.start_prompt()
        # The anchor vanishes mid-flight: a failed rebuild freezes the card
        # as unknown and drops it from the registry.
        self.rest.snapshots[SESSION_ID] = KapTransportError("connection refused")
        self.pipeline.handle_resync_required(
            ResyncRequest(session_id=SESSION_ID, reason=None, current_seq=None, epoch=None)
        )
        self.flush()

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        records = self.terminal_store.list_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].final_reply_text, "最终答复文本")
        self.assertEqual(records[0].session_id, SESSION_ID)
        self.assertEqual(records[0].terminal_result_id, "p-1")

    def test_tool_lines_cap_keeps_tail_with_truncation_notice(self) -> None:
        # Audit L6: the cap used to drop the NEWEST tool line silently.
        self.bind(CHAT_ID)
        message_id = self.start_prompt()
        for index in range(1, _MAX_TOOL_LINES + 2):  # one past the cap
            self.feed(
                kap_event(
                    "tool.call.started",
                    {
                        "turnId": 1,
                        "toolCallId": f"tc-{index}",
                        "name": "Bash",
                        "display": {"kind": "command", "command": f"step{index:02d}"},
                    },
                )
            )

        rendered = json.dumps(self.transport.patches_to(message_id)[-1], ensure_ascii=False)
        self.assertIn(f"step{_MAX_TOOL_LINES + 1:02d}", rendered)  # newest kept
        self.assertNotIn("step01", rendered)  # oldest evicted
        self.assertIn("已截断", rendered)  # truncation notice shown

        # A result for the evicted tool is a harmless no-op; a result for a
        # live tool still flips exactly its own line (indices shifted right).
        self.feed(kap_event("tool.result", {"turnId": 1, "toolCallId": "tc-1", "isError": False}))
        self.feed(
            kap_event(
                "tool.result",
                {"turnId": 1, "toolCallId": f"tc-{_MAX_TOOL_LINES + 1}", "isError": False},
            )
        )
        rendered = json.dumps(self.transport.patches_to(message_id)[-1], ensure_ascii=False)
        self.assertIn(f"✅ `Bash` step{_MAX_TOOL_LINES + 1:02d}", rendered)
        self.assertNotIn("✅ `Bash` step02", rendered)

    def test_duplicate_turn_started_reuses_existing_card(self) -> None:
        # Audit L7: a replayed turn.started used to freeze the live card and
        # send a replacement; it must be idempotent (FOCUS reuse_existing_card).
        self.bind(CHAT_ID)
        message_id = self.start_prompt()

        self.feed(turn_started(turn_id=1, prompt="做点事"))

        self.assertEqual(len(self.transport.cards_to(CHAT_ID)), 1)
        self.assertEqual(self.transport.patches_to(message_id), [])
        # The live card still belongs to the prompt: tool events patch it.
        self.feed(
            kap_event("tool.call.started", {"turnId": 1, "toolCallId": "tc-1", "name": "Bash"})
        )
        self.assertEqual(len(self.transport.patches_to(message_id)), 1)

    def test_session_title_failure_is_not_cached(self) -> None:
        # Audit L8: a failed title fetch used to cache "" forever.
        self.bind(CHAT_ID)
        del self.rest.sessions[SESSION_ID]  # get_session now fails
        self.rest.set_prompts(SESSION_ID, active="p-1")
        self.feed(turn_started(turn_id=1))
        first = self.transport.cards_to(CHAT_ID)[-1]["content"]
        self.assertNotIn("测试会话", json.dumps(first, ensure_ascii=False))

        self.rest.add_session(SESSION_ID)  # REST healthy again
        self.rest.set_prompts(SESSION_ID, active="p-2")
        self.feed(turn_started(turn_id=2))
        second = self.transport.cards_to(CHAT_ID)[-1]["content"]
        self.assertIn("测试会话", json.dumps(second, ensure_ascii=False))

    def test_terminal_delivered_registry_is_bounded(self) -> None:
        # Audit L9: the dedup registry grew unbounded; it FIFO-evicts at the cap.
        self.bind(CHAT_ID)
        with mock.patch.object(event_pipeline_module, "_TERMINAL_DELIVERED_CAP", 3):
            for index in range(1, 5):
                self.rest.set_prompts(SESSION_ID, active=f"p-{index}")
                self.feed(turn_started(turn_id=index))
                self.feed(kap_event("turn.ended", {"turnId": index, "reason": "completed"}))

        delivered = self.pipeline._terminal_delivered
        self.assertEqual(len(delivered), 3)
        self.assertNotIn((SESSION_ID, "p-1"), delivered)
        self.assertIn((SESSION_ID, "p-4"), delivered)

    def test_frozen_card_content_rejected_retries_minimal_once(self) -> None:
        # FOCUS 5787d4c port: a non-running execution card whose full frozen
        # content is rejected by Feishu (230099) gets ONE minimal-terminal
        # retry (no tool lines, no reply projection) instead of staying
        # "执行中" forever with a live cancel button.
        self.bind(CHAT_ID)
        message_id = self.start_prompt()
        self.feed(
            kap_event(
                "tool.call.started",
                {
                    "turnId": 1,
                    "toolCallId": "tc-1",
                    "name": "Bash",
                    "display": {"kind": "command", "command": "ls -la"},
                },
            )
        )
        attempts_before = len(self.transport.patch_attempts)
        self.transport.reject_patch_containing = "ls -la"

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        # Full freeze rejected + exactly one minimal retry (no third attempt).
        freeze_attempts = self.transport.patch_attempts[attempts_before:]
        self.assertEqual(len(freeze_attempts), 2)
        self.assertIn("ls -la", freeze_attempts[0])
        self.assertNotIn("ls -la", freeze_attempts[1])
        # The minimal frozen card still renders the terminal state and no
        # longer offers the cancel button.
        minimal = json.dumps(self.transport.patches_to(message_id)[-1], ensure_ascii=False)
        self.assertIn("已结束", minimal)
        self.assertNotIn("取消", minimal)
        # The terminal card still went out with the full reply text.
        terminal = self.transport.cards_to(CHAT_ID)[-1]["content"]
        self.assertIn("最终答复文本", json.dumps(terminal, ensure_ascii=False))

    def test_frozen_card_minimal_retry_is_one_shot(self) -> None:
        # Even when the minimal retry is also rejected, there is no third
        # attempt (one-shot, never a retry loop); the result still reached
        # the user via the terminal card.
        self.bind(CHAT_ID)
        message_id = self.start_prompt()
        self.feed(
            kap_event(
                "tool.call.started",
                {
                    "turnId": 1,
                    "toolCallId": "tc-1",
                    "name": "Bash",
                    "display": {"kind": "command", "command": "ls -la"},
                },
            )
        )
        attempts_before = len(self.transport.patch_attempts)
        patches_before = len(self.transport.patches)
        self.transport.reject_patch_containing = "已结束"

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        freeze_attempts = self.transport.patch_attempts[attempts_before:]
        self.assertEqual(len(freeze_attempts), 2)
        # Both the full and the minimal freeze were rejected: nothing applied.
        self.assertEqual(len(self.transport.patches), patches_before)
        self.assertIn(
            "最终答复文本",
            json.dumps(self.transport.cards_to(CHAT_ID)[-1]["content"], ensure_ascii=False),
        )

    def test_frozen_card_content_rejected_without_strippable_content_does_not_retry(self) -> None:
        # No tool lines and no reply projection: the minimal card would be
        # identical, so a rejection must not trigger a retry (FOCUS guards
        # on `log_text or reply_segments`).
        self.bind(CHAT_ID)
        message_id = self.start_prompt()
        self.rest.snapshots[SESSION_ID] = KapTransportError("connection refused")
        attempts_before = len(self.transport.patch_attempts)
        self.transport.reject_patch_containing = "状态未知"

        self.pipeline.handle_resync_required(
            ResyncRequest(session_id=SESSION_ID, reason=None, current_seq=None, epoch=None)
        )
        self.flush()

        freeze_attempts = self.transport.patch_attempts[attempts_before:]
        self.assertEqual(len(freeze_attempts), 1)
        self.assertEqual(self.transport.patches_to(message_id), [])

    def test_turn_ended_failed_uses_upstream_error_text(self) -> None:
        self.bind(CHAT_ID)
        self.start_prompt()

        self.feed(
            kap_event(
                "turn.ended",
                {
                    "turnId": 1,
                    "reason": "failed",
                    "error": {"code": "provider.api_error", "message": "上游模型报错", "retryable": False},
                },
            )
        )

        terminal = self.transport.cards_to(CHAT_ID)[-1]["content"]
        rendered = json.dumps(terminal, ensure_ascii=False)
        self.assertIn("失败", rendered)
        self.assertIn("上游模型报错", rendered)

    def test_prompt_aborted_produces_aborted_terminal_card(self) -> None:
        self.bind(CHAT_ID)
        self.start_prompt()

        self.feed(kap_event("prompt.aborted", {"promptId": "p-1", "abortedAt": "2026-01-01T00:00:00Z"}))

        terminal = self.transport.cards_to(CHAT_ID)[-1]["content"]
        self.assertIn("已中止", json.dumps(terminal, ensure_ascii=False))

    def test_terminal_text_is_latest_assistant_message_not_previous(self) -> None:
        # Upstream (messageLegacyService.list) returns items newest-first and
        # applies the role filter AFTER pagination; the newest assistant text
        # must win over older turns (live bug 2026-07-22: terminal cards
        # showed the previous prompt's reply).
        self.rest.assistant_items = [
            {"id": "m-new", "role": "assistant",
             "content": [{"type": "text", "text": "本轮答复"}]},
            {"id": "m-tool", "role": "tool",
             "content": [{"type": "text", "text": "tool output"}]},
            {"id": "m-old", "role": "assistant",
             "content": [{"type": "text", "text": "上一轮答复"}]},
        ]
        self.bind(CHAT_ID)
        self.start_prompt()

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        records = self.terminal_store.list_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].final_reply_text, "本轮答复")

    def test_terminal_is_idempotent_across_abort_and_turn_end(self) -> None:
        self.bind(CHAT_ID)
        self.start_prompt()

        self.feed(kap_event("prompt.aborted", {"promptId": "p-1", "abortedAt": "2026-01-01T00:00:00Z"}))
        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "cancelled"}))

        # Exactly one terminal card despite two terminal-class events.
        self.assertEqual(len(self.transport.cards_to(CHAT_ID)), 2)

    def test_queue_abort_refreshes_queue_depth_on_running_card(self) -> None:
        self.bind(CHAT_ID)
        message_id = self.start_prompt()
        self.rest.set_prompts(SESSION_ID, active="p-1", queued=("p-2",))
        self.pipeline.handle_event(kap_event("prompt.steered", {"activePromptId": "p-1", "promptIds": ["p-2"]}))
        self.flush()
        self.assertIn(
            "还有 1 条 prompt 排队中",
            json.dumps(self.transport.patches_to(message_id)[-1], ensure_ascii=False),
        )

        self.rest.set_prompts(SESSION_ID, active="p-1", queued=())
        self.feed(kap_event("prompt.aborted", {"promptId": "p-2", "abortedAt": "2026-01-01T00:00:00Z"}))

        # No terminal card for the queued prompt; the running card was patched.
        self.assertEqual(len(self.transport.cards_to(CHAT_ID)), 1)
        self.assertNotIn(
            "排队中",
            json.dumps(self.transport.patches_to(message_id)[-1], ensure_ascii=False),
        )

    def test_work_changed_tracks_session_work_state(self) -> None:
        self.feed(
            kap_event(
                "event.session.work_changed",
                {"busy": True, "pending_interaction": "approval"},
            )
        )
        busy, pending = self.loop.call(self.pipeline.work_state_of, SESSION_ID)
        self.assertTrue(busy)
        self.assertEqual(pending, "approval")

        self.feed(
            kap_event(
                "event.session.work_changed",
                {"busy": False, "pending_interaction": "none", "last_turn_reason": "completed"},
            )
        )
        busy, pending = self.loop.call(self.pipeline.work_state_of, SESSION_ID)
        self.assertFalse(busy)
        self.assertIsNone(pending)  # wire 'none' normalizes to None at the adapter


# ---------------------------------------------------------------------------
# approval.* lifecycle
# ---------------------------------------------------------------------------


class ApprovalTests(PipelineTestCase):
    def _start_and_request_approval(self, *, certainty: str = "certain") -> str:
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)
        message_id = self.start_prompt()
        if certainty == "certain":
            self.ownership.record("p-1", CHAT_ID)
        elif certainty == "best_effort":
            self.ownership.record_best_effort("p-1", CHAT_ID)
        self.feed(approval_requested())
        return message_id

    def test_approval_card_goes_to_owner_only_others_get_notice(self) -> None:
        self._start_and_request_approval()

        owner_cards = self.transport.cards_to(CHAT_ID)
        approval_card = owner_cards[-1]["content"]
        rendered = json.dumps(approval_card, ensure_ascii=False)
        self.assertIn("审批请求", rendered)
        self.assertIn("Bash", rendered)
        self.assertIn("rm -rf build/", rendered)
        self.assertIn("批准", rendered)
        # The other attached chat gets the read-only notice, not the approval
        # card (its only card is the broadcast execution card, §3).
        other_cards = self.transport.cards_to(CHAT_ID_2)
        self.assertEqual(len(other_cards), 1)
        self.assertNotIn("审批请求", json.dumps(other_cards[0]["content"], ensure_ascii=False))
        notices = self.transport.texts_to(CHAT_ID_2)
        self.assertEqual(len(notices), 1)
        self.assertIn("等待 `p-1` 号 prompt 的发起者处理审批", notices[0])
        # The timeout timer is running.
        self.assertEqual(len(self.timers.live), 1)
        self.assertEqual(self.timers.live[0].delay, 300)

    def test_notice_uses_display_name_when_resolvable(self) -> None:
        self.pipeline._names = IdentityNames(lambda open_id: "张三")
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)
        self.start_prompt()
        self.ownership.record("p-1", CHAT_ID, sender_open_id=ADMIN_OPEN_ID)

        self.feed(approval_requested())

        notices = self.transport.texts_to(CHAT_ID_2)
        self.assertEqual(len(notices), 1)
        self.assertIn("等待 张三（`p-1` 号 prompt 的发起者）处理审批", notices[0])

    def test_notice_falls_back_without_sender_on_record(self) -> None:
        # Resolver present, but the ownership entry has no sender (rebuild
        # path) — the wording falls back to the generic one.
        self.pipeline._names = IdentityNames(lambda open_id: "张三")
        self._start_and_request_approval()

        notices = self.transport.texts_to(CHAT_ID_2)
        self.assertEqual(len(notices), 1)
        self.assertIn("等待 `p-1` 号 prompt 的发起者处理审批", notices[0])

    def test_best_effort_ownership_gets_expired_card_no_timer(self) -> None:
        self._start_and_request_approval(certainty="best_effort")

        card = self.transport.cards_to(CHAT_ID)[-1]["content"]
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("已过期", rendered)
        self.assertNotIn("批准", rendered)
        self.assertEqual(self.timers.live, [])
        # Fail-closed to the end (audit M2): resolved upstream as rejected.
        self.assertEqual(
            self.rest.approval_resolutions,
            [{"session_id": SESSION_ID, "approval_id": "a-1", "body": {"decision": "rejected"}}],
        )

    def test_unknown_ownership_expires_to_all_attached_chats(self) -> None:
        self._start_and_request_approval(certainty="unknown")

        for chat_id in (CHAT_ID, CHAT_ID_2):
            card = self.transport.cards_to(chat_id)[-1]["content"]
            self.assertIn("已过期", json.dumps(card, ensure_ascii=False))
        self.assertEqual(self.timers.live, [])
        self.assertEqual(
            self.rest.approval_resolutions,
            [{"session_id": SESSION_ID, "approval_id": "a-1", "body": {"decision": "rejected"}}],
        )

    def test_unattributable_approval_resolves_upstream_once(self) -> None:
        # No active prompt and no turn mapping: the approval cannot be
        # attributed. Fail-closed (audit M2): expired card + upstream reject,
        # and a replayed requested event closes nothing twice.
        self.bind(CHAT_ID)
        self.rest.set_prompts(SESSION_ID, active=None)

        self.feed(approval_requested())

        cards_sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(cards_sent), 1)
        self.assertIn("已过期", json.dumps(cards_sent[0]["content"], ensure_ascii=False))
        self.assertEqual(
            self.rest.approval_resolutions,
            [{"session_id": SESSION_ID, "approval_id": "a-1", "body": {"decision": "rejected"}}],
        )
        self.assertEqual(self.timers.live, [])

        self.feed(approval_requested())

        self.assertEqual(len(self.transport.cards_to(CHAT_ID)), 1)
        self.assertEqual(len(self.rest.approval_resolutions), 1)

    def test_external_resolution_freezes_card_and_cancels_timer(self) -> None:
        self._start_and_request_approval()
        card_message_id = self.transport.cards_to(CHAT_ID)[-1]["message_id"]

        self.feed(
            kap_event(
                "event.approval.resolved",
                {"approval_id": "a-1", "decision": "approved", "resolved_at": "2026-01-01T00:00:00Z"},
            )
        )

        patches = self.transport.patches_to(card_message_id)
        self.assertEqual(len(patches), 1)
        self.assertIn("已批准", json.dumps(patches[0], ensure_ascii=False))
        self.assertEqual(self.timers.live, [])

    def test_approve_button_resolves_and_freezes(self) -> None:
        self._start_and_request_approval()

        response = self.handler.on_card_action(
            make_card_action(
                {
                    "action": cards.ACTION_APPROVAL_RESOLVE,
                    "decision": cards.APPROVAL_DECISION_APPROVED,
                    "approval_id": "a-1",
                    "prompt_id": "p-1",
                }
            )
        )

        self.assertEqual(
            self.rest.approval_resolutions,
            [{"session_id": SESSION_ID, "approval_id": "a-1", "body": {"decision": "approved"}}],
        )
        self.assertIsNotNone(response.card)
        self.assertIn("已批准", json.dumps(response.card, ensure_ascii=False))
        self.assertEqual(self.timers.live, [])
        # A repeated click after the entry closed is a notice, not an error.
        second = self.handler.on_card_action(
            make_card_action(
                {
                    "action": cards.ACTION_APPROVAL_RESOLVE,
                    "decision": cards.APPROVAL_DECISION_APPROVED,
                    "approval_id": "a-1",
                    "prompt_id": "p-1",
                }
            )
        )
        self.assertEqual(second.toast, cards.APPROVAL_STALE_NOTICE)
        self.assertEqual(len(self.rest.approval_resolutions), 1)

    def test_40902_on_resolve_freezes_card_with_notice(self) -> None:
        self._start_and_request_approval()
        self.rest.resolve_error = KapError(40902, "approval a-1 already resolved")

        response = self.handler.on_card_action(
            make_card_action(
                {
                    "action": cards.ACTION_APPROVAL_RESOLVE,
                    "decision": cards.APPROVAL_DECISION_REJECTED,
                    "approval_id": "a-1",
                    "prompt_id": "p-1",
                }
            )
        )

        self.assertEqual(response.toast, cards.APPROVAL_ALREADY_PROCESSED_NOTICE)
        self.assertIsNotNone(response.card)
        self.assertIn("已处理", json.dumps(response.card, ensure_ascii=False))

    def test_reject_with_feedback_two_step_flow(self) -> None:
        self._start_and_request_approval()
        card_message_id = self.transport.cards_to(CHAT_ID)[-1]["message_id"]

        response = self.handler.on_card_action(
            make_card_action(
                {
                    "action": cards.ACTION_APPROVAL_REJECT_WITH_FEEDBACK,
                    "approval_id": "a-1",
                    "prompt_id": "p-1",
                }
            )
        )
        self.assertIn("反馈", response.toast or "")

        # Text from a different admin in the same chat is NOT claimed as feedback.
        self.handler.on_message(make_message("这句话不该被认领", sender=OTHER_OPEN_ID))
        self.assertEqual(self.rest.approval_resolutions, [])

        self.handler.on_message(make_message("不要删除构建目录", sender=ADMIN_OPEN_ID))

        self.assertEqual(
            self.rest.approval_resolutions,
            [
                {
                    "session_id": SESSION_ID,
                    "approval_id": "a-1",
                    "body": {"decision": "rejected", "feedback": "不要删除构建目录"},
                }
            ],
        )
        patches = self.transport.patches_to(card_message_id)
        self.assertIn("已拒绝", json.dumps(patches[-1], ensure_ascii=False))
        self.assertIn("不要删除构建目录", json.dumps(patches[-1], ensure_ascii=False))
        self.assertIn("已拒绝并提交反馈", self.transport.texts_to(CHAT_ID)[-1])

    def test_timeout_auto_rejects_and_notifies_initiator(self) -> None:
        self._start_and_request_approval()
        card_message_id = self.transport.cards_to(CHAT_ID)[-1]["message_id"]

        self.timers.fire(0)
        self.flush()

        # Never auto-approve: the resolution is a rejection.
        self.assertEqual(
            self.rest.approval_resolutions,
            [{"session_id": SESSION_ID, "approval_id": "a-1", "body": {"decision": "rejected"}}],
        )
        patch = json.dumps(self.transport.patches_to(card_message_id)[-1], ensure_ascii=False)
        self.assertIn("已过期", patch)
        self.assertIn("已自动拒绝", patch)
        notice = self.transport.texts_to(CHAT_ID)[-1]
        self.assertIn("已自动拒绝", notice)
        self.assertIn("不会自动批准", notice)

    def test_resolution_after_timeout_is_ignored(self) -> None:
        self._start_and_request_approval()
        card_message_id = self.transport.cards_to(CHAT_ID)[-1]["message_id"]
        self.timers.fire(0)
        self.flush()
        patches_before = len(self.transport.patches_to(card_message_id))

        self.feed(
            kap_event(
                "event.approval.resolved",
                {"approval_id": "a-1", "decision": "approved", "resolved_at": "2026-01-01T00:00:00Z"},
            )
        )
        self.assertEqual(len(self.transport.patches_to(card_message_id)), patches_before)

    # -- reject-with-feedback state dies with its approval (audit M3) -------

    def _plant_feedback(self) -> None:
        self._start_and_request_approval()
        response = self.handler.on_card_action(
            make_card_action(
                {
                    "action": cards.ACTION_APPROVAL_REJECT_WITH_FEEDBACK,
                    "approval_id": "a-1",
                    "prompt_id": "p-1",
                }
            )
        )
        self.assertIn("反馈", response.toast or "")

    def _assert_next_text_is_a_prompt(self) -> None:
        """The audit M3 scenario: once the approval is closed by ANY path,
        the user's next plain text must reach the prompt path — never be
        swallowed as stale feedback."""
        self.handler.on_message(make_message("现在做点别的", sender=ADMIN_OPEN_ID))
        self.assertEqual(len(self.rest.submissions), 1)
        self.assertNotIn(
            cards.APPROVAL_ALREADY_PROCESSED_NOTICE, self.transport.texts_to(CHAT_ID)
        )

    def test_feedback_cleared_when_approval_closed_by_another_actor(self) -> None:
        self._plant_feedback()

        # Another admin approves the still-live approval: it closes, and the
        # planted feedback entry must die with it.
        self.handler.on_card_action(
            make_card_action(
                {
                    "action": cards.ACTION_APPROVAL_RESOLVE,
                    "decision": cards.APPROVAL_DECISION_APPROVED,
                    "approval_id": "a-1",
                    "prompt_id": "p-1",
                },
                operator=OTHER_OPEN_ID,
            )
        )

        self._assert_next_text_is_a_prompt()

    def test_feedback_cleared_on_timeout(self) -> None:
        self._plant_feedback()

        self.timers.fire(0)
        self.flush()

        self._assert_next_text_is_a_prompt()

    def test_feedback_cleared_on_external_resolution(self) -> None:
        self._plant_feedback()

        self.feed(
            kap_event(
                "event.approval.resolved",
                {"approval_id": "a-1", "decision": "approved", "resolved_at": "2026-01-01T00:00:00Z"},
            )
        )

        self._assert_next_text_is_a_prompt()

    def test_feedback_cleared_on_rebuild(self) -> None:
        self._plant_feedback()
        # The approval is gone upstream (resolved elsewhere while the event
        # stream was broken): the rebuild freezes the card and drops the
        # planted feedback entry.
        self.rest.snapshots[SESSION_ID] = make_snapshot(
            current_prompt_id="p-1",
            in_flight=True,
            turn_id=1,
        )

        self.pipeline.handle_resync_required(
            __import__("kite.adapters.kap_server", fromlist=["ResyncRequest"]).ResyncRequest(
                session_id=SESSION_ID, reason="buffer_overflow", current_seq=None, epoch=None
            )
        )
        self.flush()

        self._assert_next_text_is_a_prompt()


# ---------------------------------------------------------------------------
# question.* lifecycle (option-button cards + numbered-reply fallback, §3.9)
# ---------------------------------------------------------------------------


class QuestionTests(PipelineTestCase):
    def _start_and_request_question(self) -> None:
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)
        self.start_prompt()
        self.ownership.record("p-1", CHAT_ID)
        self.feed(question_requested())

    def test_question_card_goes_to_owner_only_others_get_notice(self) -> None:
        self._start_and_request_question()

        # The owner gets one option-button card per question item (§3.9),
        # not the text pass-through.
        owner_cards = self.transport.cards_to(CHAT_ID)
        question_card = owner_cards[-1]["content"]
        rendered = json.dumps(question_card, ensure_ascii=False)
        self.assertIn("部署到哪个环境？", rendered)
        self.assertIn("开发", rendered)
        self.assertIn("生产", rendered)
        self.assertEqual(self.transport.texts_to(CHAT_ID), [])
        buttons = _collect_buttons(question_card)
        self.assertEqual(
            [button["value"] for button in buttons],
            [
                {"action": "question_answer", "question_id": "q-1", "item_index": 0, "label": "开发"},
                {"action": "question_answer", "question_id": "q-1", "item_index": 0, "label": "生产"},
            ],
        )
        notices = self.transport.texts_to(CHAT_ID_2)
        self.assertEqual(len(notices), 1)
        self.assertIn("等待 `p-1` 号 prompt 的发起者处理", notices[0])
        self.assertEqual(len(self.timers.live), 1)

    def test_numbered_reply_is_answered_over_rest(self) -> None:
        self._start_and_request_question()

        self.handler.on_message(make_message("2"))

        self.assertEqual(
            self.rest.question_answers,
            [
                {
                    "session_id": SESSION_ID,
                    "question_id": "q-1",
                    "body": {"answers": {"q_0": {"kind": "single", "option_id": "opt_0_1"}}},
                }
            ],
        )
        self.assertIn("已提交回答", self.transport.texts_to(CHAT_ID)[-1])
        # The follow-up resolved event clears the pending question + timer.
        self.feed(
            kap_event("event.question.answered", {"question_id": "q-1", "answers": {}, "resolved_at": "2026-01-01T00:00:00Z"})
        )
        self.assertEqual(self.timers.live, [])

    def test_other_text_reply(self) -> None:
        self._start_and_request_question()

        self.handler.on_message(make_message("其他：先发灰度环境"))

        self.assertEqual(
            self.rest.question_answers[-1]["body"],
            {"answers": {"q_0": {"kind": "other", "text": "先发灰度环境"}}},
        )

    def test_out_of_range_reply_is_consumed_with_guidance(self) -> None:
        self._start_and_request_question()

        self.handler.on_message(make_message("9"))

        self.assertEqual(self.rest.question_answers, [])
        self.assertIn("无法识别回答", self.transport.texts_to(CHAT_ID)[-1])

    def test_unrelated_text_still_becomes_a_prompt(self) -> None:
        self._start_and_request_question()

        self.handler.on_message(make_message("顺便帮我看下日志"))

        self.assertEqual(self.rest.question_answers, [])
        self.assertEqual(len(self.rest.submissions), 1)
        self.assertIn("已提交", self.transport.texts_to(CHAT_ID)[-1])

    def test_timeout_auto_dismisses_with_notice(self) -> None:
        self._start_and_request_question()

        self.timers.fire(0)
        self.flush()

        self.assertEqual(self.rest.dismissals, [(SESSION_ID, "q-1")])
        self.assertIn("已自动关闭", self.transport.texts_to(CHAT_ID)[-1])

    def _question_click(self, *, operator: str = ADMIN_OPEN_ID, chat_id: str = CHAT_ID, **value_overrides):
        value = {
            "action": cards.ACTION_QUESTION_ANSWER,
            "question_id": "q-1",
            "item_index": 0,
            "label": "生产",
        }
        value.update(value_overrides)
        return self.handler.on_card_action(make_card_action(value, operator=operator, chat_id=chat_id))

    def test_button_click_answers_and_freezes_card(self) -> None:
        self._start_and_request_question()
        card_message_id = self.transport.cards_to(CHAT_ID)[-1]["message_id"]

        response = self._question_click()

        self.assertEqual(
            self.rest.question_answers,
            [
                {
                    "session_id": SESSION_ID,
                    "question_id": "q-1",
                    "body": {"answers": {"q_0": {"kind": "single", "option_id": "opt_0_1"}}},
                }
            ],
        )
        # The clicked card is frozen with the chosen label; the timer is gone.
        self.assertIsNotNone(response.card)
        frozen = json.dumps(response.card, ensure_ascii=False)
        self.assertIn("已回答：生产", frozen)
        self.assertNotIn("button", frozen)
        self.assertIn("已回答", response.toast or "")
        self.assertEqual(self.timers.live, [])
        # The follow-up answered event closes the entry without re-patching
        # the clicked card (it keeps the answer label).
        self.feed(
            kap_event("event.question.answered", {"question_id": "q-1", "answers": {}, "resolved_at": "2026-01-01T00:00:00Z"})
        )
        self.assertEqual(self.transport.patches_to(card_message_id), [])

    def test_second_click_before_event_gets_already_answered_notice(self) -> None:
        self._start_and_request_question()
        self._question_click()

        second = self._question_click()

        self.assertEqual(second.toast, cards.QUESTION_ALREADY_ANSWERED_NOTICE)
        self.assertNotEqual(second.toast_type, "error")
        self.assertIsNone(second.card)
        self.assertEqual(len(self.rest.question_answers), 1)

    def test_click_after_event_gets_stale_notice(self) -> None:
        self._start_and_request_question()
        self._question_click()
        self.feed(
            kap_event("event.question.answered", {"question_id": "q-1", "answers": {}, "resolved_at": "2026-01-01T00:00:00Z"})
        )

        response = self._question_click()

        self.assertEqual(response.toast, cards.QUESTION_STALE_NOTICE)
        self.assertEqual(len(self.rest.question_answers), 1)

    def test_click_on_unknown_question_gets_stale_notice(self) -> None:
        self._start_and_request_question()

        response = self._question_click(question_id="q-ghost")

        self.assertEqual(response.toast, cards.QUESTION_STALE_NOTICE)
        self.assertEqual(self.rest.question_answers, [])

    def test_click_from_foreign_chat_is_denied(self) -> None:
        self._start_and_request_question()

        response = self._question_click(chat_id=CHAT_ID_2)

        self.assertEqual(response.toast_type, "error")
        self.assertIn("发起聊天", response.toast or "")
        self.assertEqual(self.rest.question_answers, [])

    def test_click_with_malformed_value_is_an_error_toast(self) -> None:
        self._start_and_request_question()

        for bad_value in (
            {"item_index": 9, "label": "生产"},  # out of range
            {"item_index": 0, "label": "不存在"},  # unknown label
            {"item_index": "0", "label": "生产"},  # wrong type
        ):
            value = {"action": cards.ACTION_QUESTION_ANSWER, "question_id": "q-1"}
            value.update(bad_value)
            response = self.handler.on_card_action(make_card_action(value))
            self.assertEqual(response.toast_type, "error")
            self.assertIn("操作无效", response.toast or "")
        self.assertEqual(self.rest.question_answers, [])

    def test_answered_event_from_any_client_freezes_card_and_cancels_timer(self) -> None:
        self._start_and_request_question()
        card_message_id = self.transport.cards_to(CHAT_ID)[-1]["message_id"]

        self.feed(
            kap_event("event.question.answered", {"question_id": "q-1", "answers": {}, "resolved_at": "2026-01-01T00:00:00Z"})
        )

        patches = self.transport.patches_to(card_message_id)
        self.assertEqual(len(patches), 1)
        rendered = json.dumps(patches[0], ensure_ascii=False)
        self.assertIn("已关闭", rendered)
        self.assertIn("已在其他客户端回答", rendered)
        self.assertNotIn("button", rendered)
        self.assertEqual(self.timers.live, [])

    def test_timeout_freezes_the_card(self) -> None:
        self._start_and_request_question()
        card_message_id = self.transport.cards_to(CHAT_ID)[-1]["message_id"]

        self.timers.fire(0)
        self.flush()

        patch = json.dumps(self.transport.patches_to(card_message_id)[-1], ensure_ascii=False)
        self.assertIn("已关闭", patch)
        self.assertIn("超时未回复", patch)

    def test_numbered_reply_still_answers_and_event_freezes_card(self) -> None:
        # The numbered fallback and the buttons land on the same pending
        # entry; the card freezes when the answered event lands.
        self._start_and_request_question()
        card_message_id = self.transport.cards_to(CHAT_ID)[-1]["message_id"]

        self.handler.on_message(make_message("2"))

        self.assertEqual(
            self.rest.question_answers,
            [
                {
                    "session_id": SESSION_ID,
                    "question_id": "q-1",
                    "body": {"answers": {"q_0": {"kind": "single", "option_id": "opt_0_1"}}},
                }
            ],
        )
        self.feed(
            kap_event("event.question.answered", {"question_id": "q-1", "answers": {}, "resolved_at": "2026-01-01T00:00:00Z"})
        )
        patch = json.dumps(self.transport.patches_to(card_message_id)[-1], ensure_ascii=False)
        self.assertIn("已关闭", patch)
        self.assertEqual(self.timers.live, [])

    def test_card_send_failure_falls_back_to_numbered_text(self) -> None:
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)
        self.start_prompt()
        self.ownership.record("p-1", CHAT_ID)
        self.transport.fail_sends = True
        self.feed(question_requested())

        texts = self.transport.texts_to(CHAT_ID)
        self.assertEqual(len(texts), 1)
        self.assertIn("回复选项编号", texts[0])
        # The pending entry is still answerable via the numbered reply.
        self.handler.on_message(make_message("1"))
        self.assertEqual(len(self.rest.question_answers), 1)

    def test_multi_item_question_renders_one_card_per_item(self) -> None:
        self.bind(CHAT_ID)
        self.start_prompt()
        self.ownership.record("p-1", CHAT_ID)
        self.feed(
            question_requested(
                questions=[
                    {
                        "id": "q_0",
                        "question": "问题一？",
                        "options": [
                            {"id": "opt_0_0", "label": "甲"},
                            {"id": "opt_0_1", "label": "乙"},
                        ],
                    },
                    {
                        "id": "q_1",
                        "question": "问题二？",
                        "options": [
                            {"id": "opt_1_0", "label": "丙"},
                            {"id": "opt_1_1", "label": "丁"},
                        ],
                    },
                ]
            )
        )

        question_cards = self.transport.cards_to(CHAT_ID)[-2:]
        self.assertIn("问题一？", json.dumps(question_cards[0]["content"], ensure_ascii=False))
        self.assertIn("问题二？", json.dumps(question_cards[1]["content"], ensure_ascii=False))
        first_buttons = _collect_buttons(question_cards[0]["content"])
        second_buttons = _collect_buttons(question_cards[1]["content"])
        self.assertEqual(
            [button["value"]["item_index"] for button in first_buttons], [0, 0]
        )
        self.assertEqual(
            [button["value"]["item_index"] for button in second_buttons], [1, 1]
        )
        self.assertEqual(
            [button["value"]["label"] for button in second_buttons], ["丙", "丁"]
        )
        # Only one timeout timer for the whole question.
        self.assertEqual(len(self.timers.live), 1)

        # Clicking item 2 answers that item over REST.
        response = self._question_click(item_index=1, label="丁")
        self.assertEqual(
            self.rest.question_answers[-1]["body"],
            {"answers": {"q_1": {"kind": "single", "option_id": "opt_1_1"}}},
        )
        self.assertIn("已回答：丁", json.dumps(response.card, ensure_ascii=False))
        # The answered event freezes item 1's card; item 2's keeps its label.
        self.feed(
            kap_event("event.question.answered", {"question_id": "q-1", "answers": {}, "resolved_at": "2026-01-01T00:00:00Z"})
        )
        patches = self.transport.patches_to(question_cards[0]["message_id"])
        self.assertEqual(len(patches), 1)
        self.assertIn("已关闭", json.dumps(patches[0], ensure_ascii=False))
        self.assertEqual(self.transport.patches_to(question_cards[1]["message_id"]), [])

    def test_unknown_ownership_gets_expired_notice(self) -> None:
        self.bind(CHAT_ID)
        self.start_prompt()
        # No ownership recorded.
        self.feed(question_requested())

        texts = self.transport.texts_to(CHAT_ID)
        self.assertEqual(len(texts), 1)
        self.assertIn("已过期", texts[0])
        self.assertEqual(self.timers.live, [])
        # Fail-closed to the end (audit M2): dismissed upstream too.
        self.assertEqual(self.rest.dismissals, [(SESSION_ID, "q-1")])

    def test_unattributable_question_dismisses_upstream_once(self) -> None:
        # No active prompt and no turn mapping: expired notice + upstream
        # dismiss (audit M2); a replayed requested event is a no-op.
        self.bind(CHAT_ID)
        self.rest.set_prompts(SESSION_ID, active=None)

        self.feed(question_requested())

        texts = self.transport.texts_to(CHAT_ID)
        self.assertEqual(len(texts), 1)
        self.assertIn("已过期", texts[0])
        self.assertEqual(self.rest.dismissals, [(SESSION_ID, "q-1")])
        self.assertEqual(self.timers.live, [])

        self.feed(question_requested())

        self.assertEqual(len(self.transport.texts_to(CHAT_ID)), 1)
        self.assertEqual(len(self.rest.dismissals), 1)

    def test_timeout_race_with_click_keeps_entry_for_answered_event(self) -> None:
        # Audit M1(b): the click resolved the question but the answered
        # event lags; a timer fire already queued on the loop must NOT pop
        # the entry — the answered event needs it to freeze the other item
        # cards.
        self.bind(CHAT_ID)
        self.start_prompt()
        self.ownership.record("p-1", CHAT_ID)
        self.feed(
            question_requested(
                questions=[
                    {
                        "id": "q_0",
                        "question": "部署到哪个环境？",
                        "header": "环境",
                        "options": [{"id": "opt_0_0", "label": "开发"}],
                    },
                    {
                        "id": "q_1",
                        "question": "是否备份？",
                        "header": "备份",
                        "options": [{"id": "opt_1_0", "label": "是"}],
                    },
                ]
            )
        )
        cards_sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(cards_sent), 3)  # execution card + one card per item
        other_card_id = cards_sent[-1]["message_id"]

        self._question_click(item_index=0, label="开发")
        # The queued timer fire lands after the click (the race).
        self.loop.call(self.pipeline._question_timed_out, "q-1")

        # The entry survived: no dismiss was attempted and the answered
        # event still freezes the second item's card.
        self.assertEqual(self.rest.dismissals, [])
        self.feed(
            kap_event("event.question.answered", {"question_id": "q-1", "answers": {}, "resolved_at": "2026-01-01T00:00:00Z"})
        )
        patches = self.transport.patches_to(other_card_id)
        self.assertEqual(len(patches), 1)
        self.assertIn("已在其他客户端回答", json.dumps(patches[0], ensure_ascii=False))

    def test_timeout_dismiss_transient_error_keeps_entry(self) -> None:
        # Audit M1(a): a transient business error on the timeout dismiss
        # re-adds the entry (approval-path symmetry) — the card stays
        # actionable instead of dying as "已失效或已处理".
        self._start_and_request_question()
        self.rest.dismiss_error = KapError(40909 - 1, "upstream hiccup")

        self.timers.fire(0)
        self.flush()

        pending = self.loop.call(lambda: self.pipeline._questions.get("q-1"))
        self.assertIsNotNone(pending)
        # The user can still answer from the card afterwards.
        self.rest.dismiss_error = None
        response = self._question_click()
        self.assertEqual(response.toast, "已回答。")
        self.assertEqual(len(self.rest.question_answers), 1)


# ---------------------------------------------------------------------------
# Snapshot rebuild (resync + restart recovery)
# ---------------------------------------------------------------------------


class RebuildTests(PipelineTestCase):
    def test_resync_rebuild_refreshes_card_and_adopts_cursor(self) -> None:
        self.bind(CHAT_ID)
        message_id = self.start_prompt()
        self.rest.snapshots[SESSION_ID] = make_snapshot(
            as_of_seq=42,
            current_prompt_id="p-1",
            in_flight=True,
            turn_id=1,
        )

        self.pipeline.handle_resync_required(
            __import__("kite.adapters.kap_server", fromlist=["ResyncRequest"]).ResyncRequest(
                session_id=SESSION_ID, reason="buffer_overflow", current_seq=42, epoch="e1"
            )
        )
        self.flush()

        cursor = self.cursor_store.get(SESSION_ID)
        self.assertIsNotNone(cursor)
        self.assertEqual((cursor.seq, cursor.epoch), (42, "e1"))
        # The in-flight card was refreshed wholesale (patched in place).
        self.assertTrue(self.transport.patches_to(message_id))
        busy, _ = self.loop.call(self.pipeline.work_state_of, SESSION_ID)
        self.assertTrue(busy)

    def test_rebuild_failure_freezes_card_as_unknown(self) -> None:
        self.bind(CHAT_ID)
        message_id = self.start_prompt()
        self.rest.snapshots[SESSION_ID] = KapTransportError("connection refused")

        self.pipeline.handle_resync_required(
            __import__("kite.adapters.kap_server", fromlist=["ResyncRequest"]).ResyncRequest(
                session_id=SESSION_ID, reason=None, current_seq=None, epoch=None
            )
        )
        self.flush()

        patches = self.transport.patches_to(message_id)
        self.assertEqual(len(patches), 1)
        rendered = json.dumps(patches[0], ensure_ascii=False)
        self.assertIn("状态未知", rendered)
        self.assertIn("kitectl session status", rendered)
        # The cursor was NOT adopted and the anchor is gone (never patched again).
        self.assertIsNone(self.cursor_store.get(SESSION_ID))
        self.feed(
            kap_event(
                "tool.call.started",
                {"turnId": 1, "toolCallId": "tc-1", "name": "Bash"},
            )
        )
        self.assertEqual(len(self.transport.patches_to(message_id)), 1)

    def test_startup_recovery_reanchors_in_flight_card(self) -> None:
        # kited restart: no in-memory anchors, ownership rebuilt best-effort.
        self.bind(CHAT_ID)
        self.ownership.record_best_effort("p-9", CHAT_ID)
        self.rest.set_prompts(SESSION_ID, active="p-9", queued=("p-10",))
        self.rest.snapshots[SESSION_ID] = make_snapshot(
            current_prompt_id="p-9",
            in_flight=True,
            turn_id=7,
        )

        self.pipeline.startup_recovery([SESSION_ID])
        self.flush()

        sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(sent), 1)
        rendered = json.dumps(sent[0]["content"], ensure_ascii=False)
        self.assertIn("执行中", rendered)
        self.assertIn("还有 1 条 prompt 排队中", rendered)

    def test_unrebuildable_approval_is_expired_after_restart(self) -> None:
        # §4.6: ownership rebuilt best-effort can never route an approval card.
        self.bind(CHAT_ID)
        self.ownership.record_best_effort("p-9", CHAT_ID)
        self.rest.set_prompts(SESSION_ID, active="p-9")
        self.rest.snapshots[SESSION_ID] = make_snapshot(
            current_prompt_id="p-9",
            in_flight=True,
            turn_id=7,
            pending_interaction="approval",
            pending_approvals=(
                ApprovalRequestView(
                    approval_id="a-old",
                    turn_id=7,
                    tool_call_id="tc-1",
                    tool_name="Bash",
                    action="execute",
                    detail="rm -rf /",
                ),
            ),
        )

        self.pipeline.startup_recovery([SESSION_ID])
        self.flush()

        cards_sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(cards_sent), 2)  # re-anchored execution card + expired card
        expired = json.dumps(cards_sent[-1]["content"], ensure_ascii=False)
        self.assertIn("已过期", expired)
        self.assertIn("KITE 重启后无法确认该审批的发起者", expired)
        self.assertEqual(self.timers.live, [])
        # Fail-closed to the end (audit M2): the approval is also resolved
        # upstream as rejected, or the turn would block forever.
        self.assertEqual(
            self.rest.approval_resolutions,
            [{"session_id": SESSION_ID, "approval_id": "a-old", "body": {"decision": "rejected"}}],
        )

    def test_unroutable_approval_is_closed_once_across_rebuilds(self) -> None:
        # The recorded close-out (audit M2) means a second rebuild of the
        # same still-unroutable approval neither re-cards nor re-resolves.
        self.bind(CHAT_ID)
        self.ownership.record_best_effort("p-9", CHAT_ID)
        self.rest.set_prompts(SESSION_ID, active="p-9")
        self.rest.snapshots[SESSION_ID] = make_snapshot(
            current_prompt_id="p-9",
            in_flight=True,
            turn_id=7,
            pending_interaction="approval",
            pending_approvals=(
                ApprovalRequestView(
                    approval_id="a-old",
                    turn_id=7,
                    tool_call_id="tc-1",
                    tool_name="Bash",
                    action="execute",
                    detail="rm -rf /",
                ),
            ),
        )

        self.pipeline.startup_recovery([SESSION_ID])
        self.flush()
        self.pipeline.startup_recovery([SESSION_ID])
        self.flush()

        expired_cards = [
            item
            for item in self.transport.cards_to(CHAT_ID)
            if "已过期" in json.dumps(item["content"], ensure_ascii=False)
        ]
        self.assertEqual(len(expired_cards), 1)
        self.assertEqual(len(self.rest.approval_resolutions), 1)

    def test_rebuild_routes_tracked_approval_when_ownership_certain(self) -> None:
        # Runtime resync (no restart): the prompt was submitted by this
        # process, so a missed approval.requested can still be routed.
        self.bind(CHAT_ID)
        self.start_prompt()
        self.ownership.record("p-1", CHAT_ID)
        self.rest.snapshots[SESSION_ID] = make_snapshot(
            current_prompt_id="p-1",
            in_flight=True,
            turn_id=1,
            pending_interaction="approval",
            pending_approvals=(
                ApprovalRequestView(
                    approval_id="a-2",
                    turn_id=1,
                    tool_call_id="tc-9",
                    tool_name="Write",
                    action="write",
                    detail="/tmp/x",
                ),
            ),
        )

        self.pipeline.handle_resync_required(
            __import__("kite.adapters.kap_server", fromlist=["ResyncRequest"]).ResyncRequest(
                session_id=SESSION_ID, reason="buffer_overflow", current_seq=None, epoch=None
            )
        )
        self.flush()

        approval_card = self.transport.cards_to(CHAT_ID)[-1]["content"]
        rendered = json.dumps(approval_card, ensure_ascii=False)
        self.assertIn("审批请求", rendered)
        self.assertIn("Write", rendered)
        self.assertEqual(len(self.timers.live), 1)

    def test_rebuild_freezes_tracked_approval_resolved_elsewhere(self) -> None:
        self.bind(CHAT_ID)
        self.start_prompt()
        self.ownership.record("p-1", CHAT_ID)
        self.feed(approval_requested())
        card_message_id = self.transport.cards_to(CHAT_ID)[-1]["message_id"]
        # The approval is gone upstream (resolved via the web UI while the
        # event stream was broken).
        self.rest.snapshots[SESSION_ID] = make_snapshot(
            current_prompt_id="p-1",
            in_flight=True,
            turn_id=1,
        )

        self.pipeline.handle_resync_required(
            __import__("kite.adapters.kap_server", fromlist=["ResyncRequest"]).ResyncRequest(
                session_id=SESSION_ID, reason="epoch_changed", current_seq=None, epoch=None
            )
        )
        self.flush()

        patches = self.transport.patches_to(card_message_id)
        self.assertEqual(len(patches), 1)
        self.assertIn("已处理", json.dumps(patches[0], ensure_ascii=False))
        self.assertEqual(self.timers.live, [])

    def test_snapshot_cursor_never_moves_backwards(self) -> None:
        from kite.stores.event_cursor_store import EventCursor

        self.bind(CHAT_ID)
        self.cursor_store.set(SESSION_ID, EventCursor(seq=50, epoch="e1"))
        self.rest.snapshots[SESSION_ID] = make_snapshot(as_of_seq=42)

        self.pipeline.startup_recovery([SESSION_ID])
        self.flush()

        cursor = self.cursor_store.get(SESSION_ID)
        self.assertEqual((cursor.seq, cursor.epoch), (50, "e1"))


# ---------------------------------------------------------------------------
# Small wiring units
# ---------------------------------------------------------------------------


class WiringTests(PipelineTestCase):
    def test_swappable_rest_fails_closed_until_set(self) -> None:
        proxy = SwappableKapRest()
        with self.assertRaises(KapTransportError):
            proxy.get_prompts(SESSION_ID)
        proxy.set_client(self.rest)
        self.assertEqual(proxy.get_prompts(SESSION_ID), PromptQueueState(None, ()))

    def test_ws_hook_defers_when_no_client_and_subscribes_when_set(self) -> None:
        hook = WsSubscriptionHook()
        hook("s-1")  # no client yet: logged, not raised
        hook.resubscribe_after_rebuild("s-1")  # likewise deferred silently
        ws = FakeWsClient()
        hook.set_client(ws)
        hook("s-1")
        self.assertEqual(ws.subscriptions, ["s-1"])
        hook.resubscribe_after_rebuild("s-1")
        self.assertEqual(ws.rebuild_resubscribes, ["s-1"])

    def test_successful_rebuild_fires_snapshot_hook_for_resubscribe(self) -> None:
        # M7 wiring: kited's snapshot-rebuilt hook is what re-subscribes a
        # session whose ack-listed resync established no server-side
        # subscription — the hook must fire on a successful rebuild (and
        # only then).
        hook = WsSubscriptionHook()
        ws = FakeWsClient()
        hook.set_client(ws)
        self.pipeline.set_snapshot_rebuilt_hook(
            lambda session_id, _snapshot: hook.resubscribe_after_rebuild(session_id)
        )
        self.bind(CHAT_ID)
        self.rest.snapshots[SESSION_ID] = make_snapshot(as_of_seq=42)

        self.pipeline.handle_resync_required(
            __import__("kite.adapters.kap_server", fromlist=["ResyncRequest"]).ResyncRequest(
                session_id=SESSION_ID, reason=None, current_seq=None, epoch=None
            )
        )
        self.flush()

        self.assertEqual(ws.rebuild_resubscribes, [SESSION_ID])

        # A failed rebuild freezes instead and must NOT re-subscribe.
        self.rest.snapshots[SESSION_ID] = KapTransportError("connection refused")
        self.pipeline.handle_resync_required(
            __import__("kite.adapters.kap_server", fromlist=["ResyncRequest"]).ResyncRequest(
                session_id=SESSION_ID, reason=None, current_seq=None, epoch=None
            )
        )
        self.flush()
        self.assertEqual(ws.rebuild_resubscribes, [SESSION_ID])

    def test_shutdown_cancels_all_timers(self) -> None:
        self.bind(CHAT_ID)
        self.start_prompt()
        self.ownership.record("p-1", CHAT_ID)
        self.feed(approval_requested())
        self.feed(question_requested())
        self.assertEqual(len(self.timers.live), 2)

        self.pipeline.shutdown()
        self.flush()

        self.assertEqual(self.timers.live, [])


# ---------------------------------------------------------------------------
# WS error frames -> terminal failure (fail-closed for dead-on-arrival prompts)
# ---------------------------------------------------------------------------


class ErrorFrameTests(PipelineTestCase):
    def _error(
        self,
        *,
        session_id: str | None = SESSION_ID,
        code: str | None = "model.not_configured",
        message: str = "Model not set",
    ) -> KapErrorFrame:
        return KapErrorFrame(
            code=code,
            message=message,
            session_id=session_id,
            agent_id="main",
            retryable=False,
        )

    def test_error_frame_finishes_existing_card_as_failed(self) -> None:
        self.bind(CHAT_ID)
        self.ownership.record("p-1", CHAT_ID)
        message_id = self.start_prompt()

        self.pipeline.handle_error_frame(self._error())
        self.flush()

        sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(sent), 2)  # execution card + failed terminal card
        rendered = json.dumps(sent[-1]["content"], ensure_ascii=False)
        self.assertIn("model.not_configured", rendered)
        self.assertIn("Model not set", rendered)
        # The execution card is frozen exactly once.
        self.assertEqual(len(self.transport.patches_to(message_id)), 1)
        # Ownership is dropped; a second frame for the same prompt re-attributes.
        self.assertIsNone(self.ownership.owner_of("p-1"))

    def test_error_frame_without_card_sends_standalone_terminal(self) -> None:
        self.bind(CHAT_ID)
        self.ownership.record("p-1", CHAT_ID)
        # Active per the queue, but no turn.started yet -> no execution card.
        self.rest.set_prompts(SESSION_ID, active="p-1")

        self.pipeline.handle_error_frame(self._error())
        self.flush()

        sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(sent), 1)  # standalone failed terminal card
        rendered = json.dumps(sent[0]["content"], ensure_ascii=False)
        self.assertIn("model.not_configured", rendered)
        self.assertIsNone(self.ownership.owner_of("p-1"))

    def test_error_frame_without_active_prompt_sends_text_notice(self) -> None:
        self.bind(CHAT_ID)
        self.rest.set_prompts(SESSION_ID, active=None)

        self.pipeline.handle_error_frame(self._error())
        self.flush()

        self.assertEqual(self.transport.cards_to(CHAT_ID), [])
        texts = self.transport.texts_to(CHAT_ID)
        self.assertEqual(len(texts), 1)
        self.assertIn("model.not_configured", texts[0])

    def test_error_frame_without_session_is_log_only(self) -> None:
        self.bind(CHAT_ID)
        self.pipeline.handle_error_frame(self._error(session_id=None))
        self.flush()
        self.assertEqual(self.transport.cards_to(CHAT_ID), [])
        self.assertEqual(self.transport.texts_to(CHAT_ID), [])


# ---------------------------------------------------------------------------
# /btw side-channel agent routing (audit N3-HIGH-1, mvp-scope aligned item 13)
# ---------------------------------------------------------------------------


class BtwRoutingTests(PipelineTestCase):
    """Events whose agent_id is not the main agent take the lightweight path:
    no card, no main-stream pollution, plain-text answer on turn.ended."""

    BTW_AGENT = "btw-s-1"

    def _btw_event(self, type_: str, payload: dict) -> KapEvent:
        # The real wire stamps agentId/sessionId on every agent-event payload.
        return kap_event(
            type_,
            {"type": type_, "agentId": self.BTW_AGENT, "sessionId": SESSION_ID, **payload},
        )

    def _btw_turn_started(self, *, turn_id: int = 7) -> KapEvent:
        return self._btw_event(
            "turn.started",
            {"turnId": turn_id, "origin": {"kind": "user"}, "prompt": "旁路问题"},
        )

    def _btw_turn_ended(
        self, *, turn_id: int = 7, reason: str = "completed", error: str = ""
    ) -> KapEvent:
        payload: dict = {"turnId": turn_id, "reason": reason}
        if error:
            payload["error"] = {"message": error}
        return self._btw_event("turn.ended", payload)

    def _btw_delta(self, text: str) -> None:
        # Upstream stamps no offsets for non-main agents (inFlightTurnTracker
        # is main-only): the side stream accumulates in arrival order.
        self.pipeline.handle_volatile(
            AssistantDelta(
                session_id=SESSION_ID, offset=None, text_delta=text, agent_id=self.BTW_AGENT
            )
        )
        self.flush()

    def _submit_btw(self, prompt_id: str = "p-b1", chat_id: str = CHAT_ID) -> None:
        """The AppHandler side of a /btw submit: ownership + the FIFO seam."""
        self.ownership.record(prompt_id, chat_id, sender_open_id=ADMIN_OPEN_ID)
        self.pipeline.note_btw_prompt(SESSION_ID, self.BTW_AGENT, prompt_id)

    def test_btw_idle_turn_produces_no_card_and_delivers_text(self) -> None:
        self.bind(CHAT_ID)
        self.rest.set_prompts(SESSION_ID, active=None)  # main idle
        self._submit_btw("p-b1")

        self.feed(self._btw_turn_started())
        # No execution card, and no queue fetch is needed for attribution.
        self.assertEqual(self.transport.cards_to(CHAT_ID), [])

        self._btw_delta("旁路")
        self._btw_delta("答案")
        self.assertEqual(self.transport.cards_to(CHAT_ID), [])
        self.assertEqual(self.transport.patches, [])

        self.feed(self._btw_turn_ended())
        self.assertEqual(self.transport.cards_to(CHAT_ID), [])  # still no card
        self.assertEqual(self.transport.texts_to(CHAT_ID), ["旁路回复：旁路答案"])
        # The btw prompt's ownership is retired with the delivery.
        self.assertIsNone(self.ownership.owner_of("p-b1"))

    def test_btw_empty_stream_delivers_completion_note(self) -> None:
        self.bind(CHAT_ID)
        self._submit_btw()
        self.feed(self._btw_turn_started())

        self.feed(self._btw_turn_ended())

        self.assertEqual(self.transport.cards_to(CHAT_ID), [])
        self.assertEqual(self.transport.texts_to(CHAT_ID), ["旁路 prompt 已完成（没有文本输出）。"])

    def test_btw_answer_broadcasts_when_owner_unknown(self) -> None:
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)
        # No note_btw_prompt (e.g. the /btw predates a kited restart).
        self.feed(self._btw_turn_started())
        self._btw_delta("答案")

        self.feed(self._btw_turn_ended())

        self.assertEqual(self.transport.texts_to(CHAT_ID), ["旁路回复：答案"])
        self.assertEqual(self.transport.texts_to(CHAT_ID_2), ["旁路回复：答案"])

    def test_btw_busy_never_touches_main_card_and_main_terminal_lands(self) -> None:
        self.bind(CHAT_ID)
        self.ownership.record("p-1", CHAT_ID, sender_open_id=ADMIN_OPEN_ID)
        message_id = self.start_prompt()  # main busy with a live execution card

        self._submit_btw("p-b1")
        self.feed(self._btw_turn_started())
        # No second card, and the live card is untouched.
        self.assertEqual(len(self.transport.cards_to(CHAT_ID)), 1)
        self.assertEqual(self.transport.patches_to(message_id), [])

        self._btw_delta("旁路答复")
        self.feed(
            self._btw_event(
                "tool.call.started",
                {"turnId": 7, "toolCallId": "tc-b1", "name": "Bash"},
            )
        )
        self.assertEqual(self.transport.patches_to(message_id), [])

        self.feed(self._btw_turn_ended())
        # The btw answer arrives as plain text; no terminal card hijack, no
        # freeze of the main execution card.
        self.assertEqual(len(self.transport.cards_to(CHAT_ID)), 1)
        self.assertEqual(self.transport.texts_to(CHAT_ID), ["旁路回复：旁路答复"])
        self.assertEqual(self.transport.patches_to(message_id), [])

        # The main turn's real terminal still lands (not deduped away by the
        # btw delivery) and carries the main reply, not the btw text.
        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))
        sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(sent), 2)  # execution card + main terminal card
        rendered = json.dumps(sent[-1]["content"], ensure_ascii=False)
        self.assertIn("最终答复文本", rendered)
        self.assertNotIn("旁路答复", rendered)
        patches = self.transport.patches_to(message_id)
        self.assertEqual(len(patches), 1)  # the freeze patch, exactly once
        self.assertNotIn("旁路答复", json.dumps(patches[-1], ensure_ascii=False))

    def test_btw_failure_notifies_once_in_upstream_order(self) -> None:
        # Upstream emits turn.ended(failed) THEN the error frame for the
        # same failure (loopService.ts): exactly one failure notice, and the
        # main prompt is untouched throughout (audit R3-MED-1 + N3-HIGH-1).
        self.bind(CHAT_ID)
        self.ownership.record("p-1", CHAT_ID, sender_open_id=ADMIN_OPEN_ID)
        message_id = self.start_prompt()  # main busy
        self._submit_btw("p-b1")
        self.feed(self._btw_turn_started())

        self.feed(self._btw_turn_ended(reason="failed", error="Model not set"))
        self.assertEqual(
            self.transport.texts_to(CHAT_ID), ["旁路 prompt 执行失败：Model not set"]
        )

        self.pipeline.handle_error_frame(
            KapErrorFrame(
                code="model.not_configured",
                message="Model not set",
                session_id=SESSION_ID,
                agent_id=self.BTW_AGENT,
                retryable=False,
            )
        )
        self.flush()
        # Still exactly one notice (the error frame does not repeat it).
        self.assertEqual(len(self.transport.texts_to(CHAT_ID)), 1)

        # Main: no failed terminal, no freeze, ownership intact.
        self.assertEqual(len(self.transport.cards_to(CHAT_ID)), 1)
        self.assertEqual(self.transport.patches_to(message_id), [])
        self.assertEqual(self.ownership.owner_of("p-1"), CHAT_ID)
        self.assertIsNone(self.ownership.owner_of("p-b1"))
        # And the main prompt still finishes normally afterwards.
        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))
        self.assertEqual(len(self.transport.cards_to(CHAT_ID)), 2)

    def test_btw_error_frame_on_a_tracked_turn_stays_silent(self) -> None:
        # Defensive order (error frame BEFORE turn.ended): the tracked turn
        # is left alone — turn.ended delivers the single failure text.
        self.bind(CHAT_ID)
        self._submit_btw("p-b1")
        self.feed(self._btw_turn_started())

        self.pipeline.handle_error_frame(
            KapErrorFrame(
                code="model.not_configured",
                message="Model not set",
                session_id=SESSION_ID,
                agent_id=self.BTW_AGENT,
                retryable=False,
            )
        )
        self.flush()
        self.assertEqual(self.transport.texts_to(CHAT_ID), [])

        self.feed(self._btw_turn_ended(reason="failed", error="Model not set"))
        self.assertEqual(
            self.transport.texts_to(CHAT_ID), ["旁路 prompt 执行失败：Model not set"]
        )

    def test_btw_error_frame_before_turn_start_retires_the_submission(self) -> None:
        self.bind(CHAT_ID)
        self._submit_btw("p-b1")
        # The side prompt dies before any turn.started (dead-on-arrival).

        self.pipeline.handle_error_frame(
            KapErrorFrame(
                code="model.not_configured",
                message="Model not set",
                session_id=SESSION_ID,
                agent_id=self.BTW_AGENT,
                retryable=False,
            )
        )
        self.flush()

        # The initiating chat is still told, and the queued submission is the
        # one retired (its ownership forgotten).
        self.assertEqual(
            self.transport.texts_to(CHAT_ID),
            ["⚠️ 旁路 prompt 失败：上游错误 model.not_configured: Model not set"],
        )
        self.assertIsNone(self.ownership.owner_of("p-b1"))
        # A subsequent /btw turn starts clean: its submission attributes to
        # ITS OWN prompt, not the retired one.
        self._submit_btw("p-b2")
        self.feed(self._btw_turn_started(turn_id=8))
        self._btw_delta("新答案")
        self.feed(self._btw_turn_ended(turn_id=8))
        self.assertEqual(self.transport.texts_to(CHAT_ID)[-1], "旁路回复：新答案")

    def test_btw_answer_targets_only_the_initiating_chat(self) -> None:
        # Audit R3-HIGH-1: ownership must be read BEFORE it is retired, or
        # every answer degrades to a broadcast.
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)
        self._submit_btw("p-b1", CHAT_ID)
        self.feed(self._btw_turn_started())
        self._btw_delta("机密答案")

        self.feed(self._btw_turn_ended())

        self.assertEqual(self.transport.texts_to(CHAT_ID), ["旁路回复：机密答案"])
        self.assertEqual(self.transport.texts_to(CHAT_ID_2), [])

    def test_btw_error_frame_targets_only_the_initiating_chat(self) -> None:
        # R3-HIGH-1 on the dead-before-start path (two chats bound).
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)
        self._submit_btw("p-b1", CHAT_ID)  # dies before turn.started

        self.pipeline.handle_error_frame(
            KapErrorFrame(
                code="model.not_configured",
                message="Model not set",
                session_id=SESSION_ID,
                agent_id=self.BTW_AGENT,
                retryable=False,
            )
        )
        self.flush()

        self.assertEqual(
            self.transport.texts_to(CHAT_ID),
            ["⚠️ 旁路 prompt 失败：上游错误 model.not_configured: Model not set"],
        )
        self.assertEqual(self.transport.texts_to(CHAT_ID_2), [])

    def test_untracked_btw_turn_ended_delivers_degraded_notice(self) -> None:
        # Audit R3-MED-3: a kited restart crossed the in-flight side turn —
        # the answer is unrecoverable, but the end is never silent.
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)

        self.feed(self._btw_turn_ended())

        notice = "旁路 prompt 已结束（KITE 重启，答复内容无法取回）。"
        self.assertEqual(self.transport.texts_to(CHAT_ID), [notice])
        self.assertEqual(self.transport.texts_to(CHAT_ID_2), [notice])

    def test_btw_delivery_with_zero_attached_chats_is_log_only(self) -> None:
        # The documented drop case (_btw_target_chats): no binding means no
        # delivery surface at all — dropped with a log warning, never raised.
        self._submit_btw("p-b1", CHAT_ID)
        self.feed(self._btw_turn_started())
        self._btw_delta("答案")
        with self.assertLogs("kite.outbound", level="WARNING"):
            self.feed(self._btw_turn_ended())
        self.assertEqual(self.transport.texts_to(CHAT_ID), [])

    def test_rebuild_sweeps_btw_tracking_and_realigns_attribution(self) -> None:
        # Audit R3-MED-2: the btw turn.ended is LOST (resync gap); without a
        # sweep the stale FIFO head mis-attributes the next prompt to the
        # previous owner.
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)
        self.rest.snapshots[SESSION_ID] = make_snapshot(busy=False)
        self._submit_btw("p-b1", CHAT_ID)
        self.feed(self._btw_turn_started())

        self.pipeline.handle_resync_required(
            ResyncRequest(session_id=SESSION_ID, reason=None, current_seq=None, epoch=None)
        )
        self.flush()

        notice = "⚠️ 事件流重建，在飞的旁路 prompt 状态已丢失；若答复未送达，请重新发送 /btw。"
        self.assertEqual(self.transport.texts_to(CHAT_ID), [notice])
        self.assertEqual(self.transport.texts_to(CHAT_ID_2), [notice])
        self.assertIsNone(self.ownership.owner_of("p-b1"))

        # The next /btw turn attributes to ITS OWN owner only.
        self._submit_btw("p-b2", CHAT_ID_2)
        self.feed(self._btw_turn_started(turn_id=8))
        self._btw_delta("答案")
        self.feed(self._btw_turn_ended(turn_id=8))
        self.assertEqual(self.transport.texts_to(CHAT_ID), [notice])
        self.assertEqual(self.transport.texts_to(CHAT_ID_2)[-1], "旁路回复：答案")

    def test_btw_prompt_aborted_retires_the_fifo_entry(self) -> None:
        # Audit R3-MED-2: a remotely-aborted side prompt must not wedge the
        # attribution FIFO (turn never started).
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)
        self._submit_btw("p-b1", CHAT_ID)

        self.feed(self._btw_event("prompt.aborted", {"promptId": "p-b1"}))

        self.assertIsNone(self.ownership.owner_of("p-b1"))
        # The next submission attributes to ITS owner, not the aborted one's.
        self._submit_btw("p-b2", CHAT_ID_2)
        self.feed(self._btw_turn_started(turn_id=8))
        self._btw_delta("答案")
        self.feed(self._btw_turn_ended(turn_id=8))
        self.assertEqual(self.transport.texts_to(CHAT_ID), [])
        self.assertEqual(self.transport.texts_to(CHAT_ID_2), ["旁路回复：答案"])

    def test_btw_prompt_aborted_for_tracked_turn_closes_via_turn_ended(self) -> None:
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)
        self._submit_btw("p-b1", CHAT_ID)
        self.feed(self._btw_turn_started())

        self.feed(self._btw_event("prompt.aborted", {"promptId": "p-b1"}))
        # Nothing delivered or retired yet — turn.ended closes the live turn.
        self.assertEqual(self.transport.texts_to(CHAT_ID), [])
        self.assertEqual(self.ownership.owner_of("p-b1"), CHAT_ID)

        self.feed(self._btw_turn_ended(reason="cancelled"))
        self.assertEqual(self.transport.texts_to(CHAT_ID), ["旁路 prompt 已取消。"])
        self.assertEqual(self.transport.texts_to(CHAT_ID_2), [])

    def test_error_frame_without_agent_id_takes_main_path(self) -> None:
        self.bind(CHAT_ID)
        self.ownership.record("p-1", CHAT_ID, sender_open_id=ADMIN_OPEN_ID)
        self.start_prompt()

        self.pipeline.handle_error_frame(
            KapErrorFrame(
                code="model.not_configured",
                message="Model not set",
                session_id=SESSION_ID,
                agent_id=None,  # missing -> main path (defensive)
                retryable=False,
            )
        )
        self.flush()

        sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(sent), 2)  # execution card + failed terminal card
        self.assertIn("model.not_configured", json.dumps(sent[-1]["content"], ensure_ascii=False))

    def test_btw_interaction_events_are_inert(self) -> None:
        self.bind(CHAT_ID)
        self._submit_btw()
        self.feed(self._btw_turn_started())

        # The upstream btw agent has every tool call vetoed, so these can
        # never occur; if one ever arrives it must still not route a card or
        # touch upstream (lightweight path).
        self.feed(
            self._btw_event(
                "event.approval.requested",
                {
                    "approval_id": "a-b1",
                    "session_id": SESSION_ID,
                    "turn_id": 7,
                    "tool_call_id": "tc-1",
                    "tool_name": "Bash",
                    "action": "execute",
                    "tool_input_display": {"kind": "command", "command": "rm -rf build/"},
                    "created_at": "2026-01-01T00:00:00Z",
                    "expires_at": "2026-01-02T00:00:00Z",
                },
            )
        )
        self.assertEqual(self.transport.cards_to(CHAT_ID), [])
        self.assertEqual(self.rest.approval_resolutions, [])

    def test_btw_work_changed_does_not_move_main_work_state(self) -> None:
        self.bind(CHAT_ID)
        self.feed(kap_event("event.session.work_changed", {"busy": True, "agentId": "main"}))
        busy, _ = self.loop.call(self.pipeline.work_state_of, SESSION_ID)
        self.assertTrue(busy)

        self.feed(
            self._btw_event("event.session.work_changed", {"busy": False})
        )
        busy, _ = self.loop.call(self.pipeline.work_state_of, SESSION_ID)
        self.assertTrue(busy)  # work state tracks the main agent only

    def test_cmd_btw_seam_attributes_the_answer_to_the_initiating_chat(self) -> None:
        """End to end through OutboundAppHandler: the /btw submit feeds the
        pipeline FIFO, so the side turn's events find the owner chat."""
        self.bind(CHAT_ID)

        self.handler.on_message(make_message("/btw 旁路问题"))

        self.assertIn("已发给旁路 agent", self.transport.texts_to(CHAT_ID)[-1])
        submissions = [s for s in self.rest.submissions if s["session_id"] == SESSION_ID]
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]["body"]["agent_id"], "btw-s-1")
        prompt_id = submissions[0]["prompt_id"]

        self.feed(self._btw_turn_started())
        self._btw_delta("旁路答案")
        self.feed(self._btw_turn_ended())

        texts = self.transport.texts_to(CHAT_ID)
        self.assertIn("旁路回复：旁路答案", texts)
        self.assertIsNone(self.ownership.owner_of(prompt_id))


# ---------------------------------------------------------------------------
# Frozen-card patches through the dispatcher (audit R-3)
# ---------------------------------------------------------------------------


class FrozenDispatcherTests(PipelineTestCase):
    """The freeze / minimal-retry patches route through the CardPatchDispatcher
    (audit R-3): a Feishu 230020 on them is rescheduled instead of leaving a
    card "执行中" forever. (The dispatcher's own coalescing/retry semantics
    are locked in test_patch_dispatcher.)"""

    def test_freeze_patch_flows_through_the_dispatcher(self) -> None:
        self.bind(CHAT_ID)
        message_id = self.start_prompt()

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        applied = self.dispatcher.applied_to(message_id)
        self.assertEqual(len(applied), 1)
        self.assertIn("已结束", json.dumps(json.loads(applied[0]), ensure_ascii=False))
        # And the transport received exactly that one freeze patch.
        self.assertEqual(len(self.transport.patches_to(message_id)), 1)

    def test_frozen_card_minimal_retry_goes_through_the_dispatcher(self) -> None:
        self.bind(CHAT_ID)
        message_id = self.start_prompt()
        # Armed before the tool line exists, so the running-card patch with
        # the marker is rejected too and only the freeze sequence lands.
        self.transport.reject_patch_containing = "SENSITIVE_MARKER"
        self.feed(
            kap_event(
                "tool.call.started",
                {
                    "turnId": 1,
                    "toolCallId": "tc-1",
                    "name": "Bash",
                    "display": {"kind": "command", "command": "rm -rf SENSITIVE_MARKER"},
                },
            )
        )

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        applied = self.dispatcher.applied_to(message_id)
        # Two attempts through the dispatcher: the full frozen card (with
        # the tool line, rejected 230099-style), then the minimal one.
        self.assertEqual(len(applied), 2)
        self.assertIn("SENSITIVE_MARKER", applied[0])
        self.assertNotIn("SENSITIVE_MARKER", applied[1])
        # The minimal frozen card landed (one-shot: no third attempt).
        patches = self.transport.patches_to(message_id)
        self.assertEqual(len(patches), 1)
        rendered = json.dumps(patches[0], ensure_ascii=False)
        self.assertIn("已结束", rendered)
        self.assertNotIn("SENSITIVE_MARKER", rendered)

    def test_frozen_card_without_strippable_content_does_not_retry(self) -> None:
        self.bind(CHAT_ID)
        message_id = self.start_prompt()  # no tool lines, no streamed body
        self.transport.reject_patch_containing = "做点事"  # in full AND minimal

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        # The minimal card would be identical (nothing strippable): exactly
        # one rejected attempt, no retry (unchanged FOCUS discipline).
        self.assertEqual(len(self.dispatcher.applied_to(message_id)), 1)
        self.assertEqual(self.transport.patches_to(message_id), [])


# ---------------------------------------------------------------------------
# Terminal reconcile: retry-on-empty + delivery dedup (FOCUS recovery port)
# ---------------------------------------------------------------------------


class TerminalReconcileTests(PipelineTestCase):
    def _end_completed(self) -> None:
        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

    def test_empty_terminal_text_retries_then_uses_late_text(self) -> None:
        self.rest.assistant_text = ""
        self.bind(CHAT_ID)
        message_id = self.start_prompt()

        self._end_completed()

        # No terminal card yet: the empty read scheduled a retry instead.
        self.assertEqual(len(self.transport.cards_to(CHAT_ID)), 1)
        self.assertEqual(len(self.timers.created), 1)
        self.assertEqual(self.timers.created[0].delay, 1.0)

        self.rest.assistant_text = "迟到的最终答复"
        self.timers.created[0].fire()
        self.flush()

        sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(sent), 2)  # execution card + terminal card
        self.assertIn("迟到的最终答复", json.dumps(sent[-1]["content"], ensure_ascii=False))
        # The card got a queue-refresh patch in the reconcile window, then
        # the freeze patch; the text was persisted; no further retry.
        patches = self.transport.patches_to(message_id)
        self.assertEqual(len(patches), 2)
        self.assertIn("已结束", json.dumps(patches[-1], ensure_ascii=False))
        self.assertEqual(len(self.timers.created), 1)
        records = self.terminal_store.list_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].final_reply_text, "迟到的最终答复")

    def test_empty_terminal_text_falls_back_to_stub_after_retries(self) -> None:
        self.rest.assistant_text = ""
        self.bind(CHAT_ID)
        self.start_prompt()

        self._end_completed()
        # Default: 3 retries (delay 1.0s each) before the stub fallback.
        for expected_timers in (1, 2, 3):
            self.assertEqual(len(self.timers.created), expected_timers)
            self.assertEqual(self.timers.created[-1].delay, 1.0)
            self.assertEqual(len(self.transport.cards_to(CHAT_ID)), 1)
            self.timers.created[-1].fire()
            self.flush()

        self.assertEqual(len(self.timers.created), 3)
        sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(sent), 2)
        self.assertIn("无最终输出", json.dumps(sent[-1]["content"], ensure_ascii=False))
        # Stub text is not persisted as a result.
        self.assertEqual(len(self.terminal_store.list_all()), 0)

    def test_abort_during_retry_window_wins_the_dedup(self) -> None:
        self.rest.assistant_text = ""
        self.bind(CHAT_ID)
        self.start_prompt()
        self._end_completed()
        self.assertEqual(len(self.timers.created), 1)

        self.feed(
            kap_event("prompt.aborted", {"promptId": "p-1", "abortedAt": "2026-01-01T00:00:00Z"})
        )

        # One terminal card (the abort); the pending retry was cancelled.
        self.assertEqual(len(self.transport.cards_to(CHAT_ID)), 2)
        terminal = json.dumps(self.transport.cards_to(CHAT_ID)[-1]["content"], ensure_ascii=False)
        self.assertIn("已中止", terminal)
        self.assertTrue(self.timers.created[0].cancelled)
        # A late retry fire is a no-op: still exactly one terminal card.
        self.timers.created[0].fire()
        self.flush()
        self.assertEqual(len(self.transport.cards_to(CHAT_ID)), 2)

    def test_retry_never_pins_new_prompt_text_on_old_terminal(self) -> None:
        self.rest.assistant_text = ""
        self.bind(CHAT_ID)
        self.start_prompt()
        self._end_completed()
        self.assertEqual(len(self.timers.created), 1)

        # A newer prompt takes over the session before the retry fires.
        self.rest.set_prompts(SESSION_ID, active="p-2")
        self.feed(turn_started(turn_id=2, prompt="另一个任务"))
        self.rest.assistant_text = "新 prompt 的部分输出"

        self.timers.created[0].fire()
        self.flush()

        # p-1's terminal is delivered standalone with stub text — the newer
        # prompt's text must never land on the old prompt's terminal card
        # (monotonic rule: never deliver stale text).
        sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(sent), 3)  # p-1 card, p-2 card, p-1 terminal
        terminal = json.dumps(sent[-1]["content"], ensure_ascii=False)
        self.assertIn("无最终输出", terminal)
        self.assertNotIn("新 prompt 的部分输出", terminal)
        self.assertEqual(len(self.terminal_store.list_all()), 0)


# ---------------------------------------------------------------------------
# Approval click two-phase guard (FOCUS interaction_request_controller port)
# ---------------------------------------------------------------------------


class ApprovalClickGuardTests(PipelineTestCase):
    def _start_and_request_approval(self) -> None:
        self.bind(CHAT_ID)
        self.start_prompt()
        self.ownership.record("p-1", CHAT_ID)
        self.feed(approval_requested())

    def _approve_action(self, approval_id: str = "a-1") -> CardAction:
        return make_card_action(
            {
                "action": cards.ACTION_APPROVAL_RESOLVE,
                "decision": cards.APPROVAL_DECISION_APPROVED,
                "approval_id": approval_id,
                "prompt_id": "p-1",
            }
        )

    def test_second_click_while_processing_gets_processing_notice(self) -> None:
        self._start_and_request_approval()
        nested_toasts: list[str | None] = []

        def click_again_mid_flight() -> None:
            nested_toasts.append(self.handler.on_card_action(self._approve_action()).toast)

        self.rest.resolve_hook = click_again_mid_flight

        response = self.handler.on_card_action(self._approve_action())

        self.assertEqual(nested_toasts, [cards.APPROVAL_PROCESSING_NOTICE])
        # The mid-flight click never double-submits: exactly one resolution.
        self.assertEqual(len(self.rest.approval_resolutions), 1)
        self.assertIn("已批准", response.toast or "")

    def test_transport_failure_rolls_back_to_pending_and_allows_retry(self) -> None:
        self._start_and_request_approval()
        self.rest.resolve_error = KapTransportError("connection refused")

        first = self.handler.on_card_action(self._approve_action())

        self.assertEqual(first.toast_type, "error")
        self.assertIn("无法连接 kap-server", first.toast or "")
        self.assertEqual(self.rest.approval_resolutions, [])

        self.rest.resolve_error = None
        second = self.handler.on_card_action(self._approve_action())

        self.assertIn("已批准", second.toast or "")
        self.assertEqual(len(self.rest.approval_resolutions), 1)

    def test_click_on_missing_entry_gets_stale_notice_not_error(self) -> None:
        self._start_and_request_approval()

        response = self.handler.on_card_action(self._approve_action("a-ghost"))

        self.assertEqual(response.toast, cards.APPROVAL_STALE_NOTICE)
        self.assertNotEqual(response.toast_type, "error")
        self.assertEqual(self.rest.approval_resolutions, [])


# ---------------------------------------------------------------------------
# Fail-close sweep: unbind (/new /switch) + shutdown entry points
# ---------------------------------------------------------------------------


class SweepTests(PipelineTestCase):
    def _pending_approval_and_question(self) -> str:
        """A bound chat with one tracked pending approval + question.

        Returns the approval card's message id (the question's option-button
        card is sent after it, so capture before feeding the question)."""
        self.bind(CHAT_ID)
        self.start_prompt()
        self.ownership.record("p-1", CHAT_ID)
        self.feed(approval_requested())
        approval_card_id = self.transport.cards_to(CHAT_ID)[-1]["message_id"]
        self.feed(question_requested())
        return approval_card_id

    def test_new_sweeps_old_session_pending_interactions(self) -> None:
        approval_card_id = self._pending_approval_and_question()
        # /new's preflight only passes when the old session is idle upstream.
        self.rest.set_prompts(SESSION_ID, active=None)

        self.handler.on_message(make_message("/new"))

        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-new-2")
        # The old session's approval was rejected upstream and expired locally.
        self.assertEqual(
            self.rest.approval_resolutions,
            [{"session_id": SESSION_ID, "approval_id": "a-1", "body": {"decision": "rejected"}}],
        )
        patch = json.dumps(self.transport.patches_to(approval_card_id)[-1], ensure_ascii=False)
        self.assertIn("已过期", patch)
        self.assertIn("已切换到其他会话", patch)
        # The question was dismissed upstream and closed with a notice.
        self.assertEqual(self.rest.dismissals, [(SESSION_ID, "q-1")])
        self.assertTrue(
            any("已关闭" in text for text in self.transport.texts_to(CHAT_ID))
        )
        self.assertEqual(self.timers.live, [])

    def test_switch_sweeps_old_session_pending_interactions(self) -> None:
        approval_card_id = self._pending_approval_and_question()
        self.rest.add_session("s-2", title="Beta")
        # /switch's preflight (mvp-scope aligned item 11) only passes when
        # the old session is idle upstream — same discipline as /new.
        self.rest.set_prompts(SESSION_ID, active=None)

        self.handler.on_message(make_message("/switch s-2"))

        binding = self.store.load(CHAT_ID)
        assert binding is not None
        self.assertEqual(binding["session_id"], "s-2")
        self.assertEqual(
            self.rest.approval_resolutions,
            [{"session_id": SESSION_ID, "approval_id": "a-1", "body": {"decision": "rejected"}}],
        )
        patch = json.dumps(self.transport.patches_to(approval_card_id)[-1], ensure_ascii=False)
        self.assertIn("已过期", patch)
        self.assertEqual(self.rest.dismissals, [(SESSION_ID, "q-1")])
        self.assertEqual(self.timers.live, [])

    def test_unbind_sweep_only_touches_interactions_routed_to_that_chat(self) -> None:
        self._pending_approval_and_question()
        # Another chat's pending approval on the SAME session (multi-chat
        # binding is an admin-only shape, but the sweep must not eat it).
        self.loop.call(
            self.pipeline._approvals.__setitem__,
            "a-other",
            _PendingApproval(
                approval_id="a-other",
                session_id=SESSION_ID,
                prompt_id="p-1",
                owner_chat_id=CHAT_ID_2,
                card_message_id="",
                timer=None,
            ),
        )
        self.rest.add_session("s-2", title="Beta")
        # Same /switch preflight as /new: allowed only while the old
        # session is idle upstream.
        self.rest.set_prompts(SESSION_ID, active=None)

        self.handler.on_message(make_message("/switch s-2"))

        self.assertEqual(
            self.rest.approval_resolutions,
            [{"session_id": SESSION_ID, "approval_id": "a-1", "body": {"decision": "rejected"}}],
        )
        self.assertTrue(self.loop.call(lambda: "a-other" in self.pipeline._approvals))

    def test_shutdown_sweeps_all_pending_interactions(self) -> None:
        approval_card_id = self._pending_approval_and_question()

        self.pipeline.shutdown()
        self.flush()

        self.assertEqual(
            self.rest.approval_resolutions,
            [{"session_id": SESSION_ID, "approval_id": "a-1", "body": {"decision": "rejected"}}],
        )
        patch = json.dumps(self.transport.patches_to(approval_card_id)[-1], ensure_ascii=False)
        self.assertIn("已过期", patch)
        self.assertIn("KITE 服务已停止", patch)
        self.assertEqual(self.rest.dismissals, [(SESSION_ID, "q-1")])
        texts = self.transport.texts_to(CHAT_ID)
        self.assertTrue(any("已关闭" in text and "KITE 服务已停止" in text for text in texts))
        self.assertEqual(self.timers.live, [])

    def test_shutdown_sweep_patches_cards_expired_when_kap_unreachable(self) -> None:
        approval_card_id = self._pending_approval_and_question()
        self.rest.resolve_error = KapTransportError("connection refused")
        self.rest.dismiss_error = KapTransportError("connection refused")

        self.pipeline.shutdown()
        self.flush()

        # Upstream responds failed, but nothing stays clickable locally.
        self.assertEqual(self.rest.approval_resolutions, [])
        self.assertEqual(self.rest.dismissals, [])
        patch = json.dumps(self.transport.patches_to(approval_card_id)[-1], ensure_ascii=False)
        self.assertIn("已过期", patch)
        texts = self.transport.texts_to(CHAT_ID)
        self.assertTrue(any("已关闭" in text for text in texts))
        self.assertEqual(self.timers.live, [])

    def test_sweep_marks_card_handled_when_upstream_already_resolved(self) -> None:
        approval_card_id = self._pending_approval_and_question()
        self.rest.resolve_error = KapError(40902, "approval a-1 already resolved")

        self.pipeline.shutdown()
        self.flush()

        patch = json.dumps(self.transport.patches_to(approval_card_id)[-1], ensure_ascii=False)
        self.assertIn("已处理", patch)
        self.assertNotIn("已过期", patch)


# ---------------------------------------------------------------------------
# Group chat: actor checks at click/reply time (group-chat contract §3.3)
# ---------------------------------------------------------------------------

GROUP_CHAT_ID = "oc_group"
MEMBER_OPEN_ID = "ou_member"
BYSTANDER_OPEN_ID = "ou_bystander"


def make_group_message(
    text: str,
    *,
    sender: str = MEMBER_OPEN_ID,
    chat_id: str = GROUP_CHAT_ID,
    message_id: str = "om_g1",
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_type="group",
        msg_type="text",
        text=text,
        sender_open_id=sender,
        sender_user_id="u_1",
        sender_type="user",
        bot_mentioned=True,
        mentions=[],
        thread_id="",
        root_id="",
        parent_id="",
        create_time=0,
    )


class GroupActorTests(PipelineTestCase):
    def _start_group_prompt_with_approval(self, *, sender: str | None = MEMBER_OPEN_ID) -> str:
        """Group + p2p chats bound to one session; the prompt is owned by the
        group (initiator ``sender``; None records an unknown sender); the
        approval card is routed to the group. Returns its message id."""
        self.bind(CHAT_ID)  # p2p chat attached to the same session
        self.bind(GROUP_CHAT_ID)
        self.start_prompt()
        if sender is None:
            self.ownership.record("p-1", GROUP_CHAT_ID)
        else:
            self.ownership.record("p-1", GROUP_CHAT_ID, sender_open_id=sender)
        self.feed(approval_requested())
        return self.transport.cards_to(GROUP_CHAT_ID)[-1]["message_id"]

    def _approve_click(self, operator: str, chat_id: str = GROUP_CHAT_ID):
        return self.handler.on_card_action(
            make_card_action(
                {
                    "action": cards.ACTION_APPROVAL_RESOLVE,
                    "decision": cards.APPROVAL_DECISION_APPROVED,
                    "approval_id": "a-1",
                    "prompt_id": "p-1",
                },
                operator=operator,
                chat_id=chat_id,
            )
        )

    def test_broadcast_to_group_and_p2p_approval_only_to_initiator_chat(self) -> None:
        # Broadcast (§3.5 + mvp-scope §3): a group is an ordinary attached
        # chat — the execution card lands in both chats, the actionable
        # approval card goes only to the initiator's chat (the group), and
        # the p2p chat gets the read-only notice.
        self._start_group_prompt_with_approval()

        p2p_cards = self.transport.cards_to(CHAT_ID)
        group_cards = self.transport.cards_to(GROUP_CHAT_ID)
        self.assertEqual(len(p2p_cards), 1)  # execution card only
        self.assertEqual(len(group_cards), 2)  # execution card + approval card
        self.assertNotIn("审批请求", json.dumps(p2p_cards[-1]["content"], ensure_ascii=False))
        self.assertIn("审批请求", json.dumps(group_cards[-1]["content"], ensure_ascii=False))
        notices = self.transport.texts_to(CHAT_ID)
        self.assertEqual(len(notices), 1)
        self.assertIn("发起者处理审批", notices[0])

    def test_bystander_click_denied_no_state_change_no_rest_call(self) -> None:
        card_message_id = self._start_group_prompt_with_approval()

        response = self._approve_click(BYSTANDER_OPEN_ID)

        self.assertEqual(response.toast_type, "error")
        self.assertIn("发起者或管理员", response.toast or "")
        # No upstream call, no card patch, and the approval is still live:
        # the initiator can still resolve it afterwards (§4.2).
        self.assertEqual(self.rest.approval_resolutions, [])
        self.assertEqual(self.transport.patches_to(card_message_id), [])
        followup = self._approve_click(MEMBER_OPEN_ID)
        self.assertIn("已批准", followup.toast or "")
        self.assertEqual(len(self.rest.approval_resolutions), 1)

    def test_initiator_click_resolves(self) -> None:
        self._start_group_prompt_with_approval()

        response = self._approve_click(MEMBER_OPEN_ID)

        self.assertEqual(
            self.rest.approval_resolutions,
            [{"session_id": SESSION_ID, "approval_id": "a-1", "body": {"decision": "approved"}}],
        )
        self.assertIn("已批准", response.toast or "")

    def test_admin_click_resolves(self) -> None:
        self._start_group_prompt_with_approval()

        response = self._approve_click(OTHER_OPEN_ID)

        self.assertEqual(len(self.rest.approval_resolutions), 1)
        self.assertIn("已批准", response.toast or "")

    def test_unknown_initiator_fails_closed_to_admin_only(self) -> None:
        # Ownership without a sender (control-plane submit / restart rebuild):
        # no member can claim the initiator role (fail-closed).
        self._start_group_prompt_with_approval(sender=None)

        response = self._approve_click(MEMBER_OPEN_ID)

        self.assertEqual(response.toast_type, "error")
        self.assertEqual(self.rest.approval_resolutions, [])
        followup = self._approve_click(OTHER_OPEN_ID)
        self.assertIn("已批准", followup.toast or "")
        self.assertEqual(len(self.rest.approval_resolutions), 1)

    def test_missing_operator_identity_is_a_non_member(self) -> None:
        # §4.4: sender identity missing from an event -> treat as non-member.
        self._start_group_prompt_with_approval()

        response = self._approve_click("")

        self.assertEqual(response.toast_type, "error")
        self.assertEqual(self.rest.approval_resolutions, [])

    def _start_group_question(self) -> None:
        self.bind(GROUP_CHAT_ID)
        self.start_prompt(chat_id=GROUP_CHAT_ID)
        self.ownership.record("p-1", GROUP_CHAT_ID, sender_open_id=MEMBER_OPEN_ID)
        self.feed(question_requested())
        # Member text only enters in an activated group.
        self.group_config_store.activate(GROUP_CHAT_ID, activated_by=ADMIN_OPEN_ID)

    def test_group_question_reply_from_bystander_is_not_claimed(self) -> None:
        self._start_group_question()

        # A bystander's @bot "1" is ordinary member text: it enters the
        # prompt path instead of answering the initiator's question (§3.3).
        self.handler.on_message(make_group_message("1", sender=BYSTANDER_OPEN_ID))

        self.assertEqual(self.rest.question_answers, [])
        self.assertEqual(len(self.rest.submissions), 1)
        # The question is still pending for the initiator, who can answer it.
        self.handler.on_message(make_group_message("1", sender=MEMBER_OPEN_ID))
        self.assertEqual(len(self.rest.question_answers), 1)

    def test_group_question_reply_from_initiator_is_answered(self) -> None:
        self._start_group_question()

        self.handler.on_message(make_group_message("1", sender=MEMBER_OPEN_ID))

        self.assertEqual(len(self.rest.question_answers), 1)
        answer = self.rest.question_answers[0]
        self.assertEqual(answer["session_id"], SESSION_ID)
        self.assertEqual(answer["question_id"], "q-1")
        self.assertIn("已提交回答", self.transport.texts_to(GROUP_CHAT_ID)[-1])

    def test_group_question_reply_from_admin_is_answered(self) -> None:
        self._start_group_question()

        self.handler.on_message(make_group_message("1", sender=OTHER_OPEN_ID))

        self.assertEqual(len(self.rest.question_answers), 1)

    def _group_question_click(self, operator: str):
        return self.handler.on_card_action(
            make_card_action(
                {
                    "action": cards.ACTION_QUESTION_ANSWER,
                    "question_id": "q-1",
                    "item_index": 0,
                    "label": "生产",
                },
                operator=operator,
                chat_id=GROUP_CHAT_ID,
            )
        )

    def test_group_question_button_click_from_bystander_is_denied(self) -> None:
        self._start_group_question()
        card_message_id = self.transport.cards_to(GROUP_CHAT_ID)[-1]["message_id"]

        response = self._group_question_click(BYSTANDER_OPEN_ID)

        self.assertEqual(response.toast_type, "error")
        self.assertIn("发起者或管理员", response.toast or "")
        # No upstream call, no card patch, and the question is still live:
        # the initiator can still answer it afterwards (§3.3).
        self.assertEqual(self.rest.question_answers, [])
        self.assertEqual(self.transport.patches_to(card_message_id), [])
        followup = self._group_question_click(MEMBER_OPEN_ID)
        self.assertIn("已回答", followup.toast or "")
        self.assertEqual(len(self.rest.question_answers), 1)

    def test_group_question_button_click_from_initiator_is_answered(self) -> None:
        self._start_group_question()

        response = self._group_question_click(MEMBER_OPEN_ID)

        self.assertEqual(len(self.rest.question_answers), 1)
        answer = self.rest.question_answers[0]
        self.assertEqual(answer["session_id"], SESSION_ID)
        self.assertEqual(answer["question_id"], "q-1")
        self.assertIn("已回答", response.toast or "")

    def test_group_question_button_click_from_admin_is_answered(self) -> None:
        self._start_group_question()

        response = self._group_question_click(OTHER_OPEN_ID)

        self.assertEqual(len(self.rest.question_answers), 1)
        self.assertIn("已回答", response.toast or "")


# ---------------------------------------------------------------------------
# Execution-card cancel button (same actor rule as /abort)
# ---------------------------------------------------------------------------


class AbortActionTests(PipelineTestCase):
    def _action(self, *, operator: str = ADMIN_OPEN_ID, prompt_id: str = "p-1") -> CardAction:
        return make_card_action(
            {"action": cards.ACTION_PROMPT_ABORT, "prompt_id": prompt_id, "session_id": SESSION_ID},
            operator=operator,
        )

    def test_initiator_click_aborts(self) -> None:
        self.bind(CHAT_ID)
        self.ownership.record("p-1", CHAT_ID, sender_open_id=ADMIN_OPEN_ID)
        response = self.handler.handle_abort_action(self._action())
        self.assertEqual(self.rest.aborts, [(SESSION_ID, "p-1")])
        self.assertIn("已发起中止", response.toast or "")

    def test_admin_click_aborts_when_not_initiator(self) -> None:
        self.bind(CHAT_ID)
        self.ownership.record("p-1", CHAT_ID, sender_open_id="ou_someone_else")
        response = self.handler.handle_abort_action(self._action())
        self.assertEqual(self.rest.aborts, [(SESSION_ID, "p-1")])
        self.assertIn("已发起中止", response.toast or "")

    def test_bystander_click_denied_without_rest_call(self) -> None:
        self.bind(CHAT_ID)
        self.ownership.record("p-1", CHAT_ID, sender_open_id=ADMIN_OPEN_ID)
        response = self.handler.handle_abort_action(self._action(operator="ou_bystander"))
        self.assertEqual(self.rest.aborts, [])
        self.assertIn("发起者或管理员", response.toast or "")

    def test_finished_prompt_answers_ended(self) -> None:
        self.bind(CHAT_ID)
        self.ownership.record("p-1", CHAT_ID, sender_open_id=ADMIN_OPEN_ID)
        self.rest.abort_error = KapError(40402, "one or more prompts are not pending")
        response = self.handler.handle_abort_action(self._action())
        self.assertIn("已结束", response.toast or "")

    def test_malformed_value_is_an_error_toast(self) -> None:
        self.bind(CHAT_ID)
        response = self.handler.handle_abort_action(
            make_card_action({"action": cards.ACTION_PROMPT_ABORT, "prompt_id": ""})
        )
        self.assertEqual(self.rest.aborts, [])
        self.assertIn("操作无效", response.toast or "")


# ---------------------------------------------------------------------------
# Reject-with-feedback toast wording (mode-aware, audit L17) and
# structured-log fields (mvp-scope §6, audit D1)
# ---------------------------------------------------------------------------


class FeedbackToastModeTests(ApprovalTests):
    def _feedback_action(self) -> CardAction:
        return make_card_action(
            {
                "action": cards.ACTION_APPROVAL_REJECT_WITH_FEEDBACK,
                "approval_id": "a-1",
                "prompt_id": "p-1",
            }
        )

    def test_all_mode_feedback_toast_has_no_at_clause(self) -> None:
        self.pipeline._group_mode_of = lambda chat_id: "all"
        self._start_and_request_approval()

        response = self.handler.handle_approval_action(self._feedback_action())

        self.assertNotIn("@机器人", response.toast or "")

    def test_mention_only_feedback_toast_mentions_at(self) -> None:
        self.pipeline._group_mode_of = lambda chat_id: "mention_only"
        self._start_and_request_approval()

        response = self.handler.handle_approval_action(self._feedback_action())

        self.assertIn("@机器人", response.toast or "")


class StructuredLogFieldTests(ApprovalTests):
    def test_prompt_started_log_carries_chat_and_prompt_id(self) -> None:
        self.bind(CHAT_ID)
        self.ownership.record("p-1", CHAT_ID, sender_open_id=ADMIN_OPEN_ID)

        with self.assertLogs("kite.outbound", level="INFO") as captured:
            self.start_prompt()

        started = [line for line in captured.output if "prompt started" in line]
        self.assertEqual(len(started), 1)
        self.assertIn(f"chat_id={CHAT_ID}", started[0])
        self.assertIn("prompt_id=p-1", started[0])

    def test_prompt_ended_log_carries_chat_and_prompt_id(self) -> None:
        self.bind(CHAT_ID)
        self.ownership.record("p-1", CHAT_ID, sender_open_id=ADMIN_OPEN_ID)
        self.start_prompt()

        with self.assertLogs("kite.outbound", level="INFO") as captured:
            self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        ended = [line for line in captured.output if "prompt ended" in line]
        self.assertEqual(len(ended), 1)
        self.assertIn(f"chat_id={CHAT_ID}", ended[0])
        self.assertIn("prompt_id=p-1", ended[0])

    def test_approval_resolved_log_carries_prompt_id(self) -> None:
        self._start_and_request_approval()

        with self.assertLogs("kite.outbound", level="INFO") as captured:
            self.feed(
                kap_event(
                    "event.approval.resolved",
                    {"approval_id": "a-1", "decision": "approved"},
                )
            )

        resolved = [line for line in captured.output if "approval resolved" in line]
        self.assertEqual(len(resolved), 1)
        self.assertIn("prompt_id=p-1", resolved[0])

    def test_session_rebuilt_log_carries_ids(self) -> None:
        # Audit R-6 (mvp-scope §6): the resync/snapshot-rebuild line carries
        # session_id + prompt_id, plus chat_id where a chat is attributable
        # (the snapshot's current prompt has a recorded owner).
        self.bind(CHAT_ID)
        self.ownership.record("p-1", CHAT_ID, sender_open_id=ADMIN_OPEN_ID)
        self.rest.snapshots[SESSION_ID] = make_snapshot(
            as_of_seq=42, current_prompt_id="p-1", in_flight=True, turn_id=1
        )

        with self.assertLogs("kite.outbound", level="INFO") as captured:
            self.pipeline.handle_resync_required(
                ResyncRequest(
                    session_id=SESSION_ID,
                    reason="buffer_overflow",
                    current_seq=42,
                    epoch="e1",
                )
            )
            self.flush()

        rebuilt = [line for line in captured.output if "session rebuilt" in line]
        self.assertEqual(len(rebuilt), 1)
        self.assertIn(f"chat_id={CHAT_ID}", rebuilt[0])
        self.assertIn(f"session={SESSION_ID}", rebuilt[0])
        self.assertIn("prompt_id=p-1", rebuilt[0])

    def test_session_rebuilt_log_without_owner_dashes_chat_id(self) -> None:
        # No attributable chat (no recorded owner for the current prompt):
        # chat_id degrades to "-" rather than being guessed (§6).
        self.rest.snapshots[SESSION_ID] = make_snapshot(
            as_of_seq=42, current_prompt_id="p-1", in_flight=True, turn_id=1
        )

        with self.assertLogs("kite.outbound", level="INFO") as captured:
            self.pipeline.handle_resync_required(
                ResyncRequest(
                    session_id=SESSION_ID,
                    reason="buffer_overflow",
                    current_seq=42,
                    epoch="e1",
                )
            )
            self.flush()

        rebuilt = [line for line in captured.output if "session rebuilt" in line]
        self.assertEqual(len(rebuilt), 1)
        self.assertIn("chat_id=-", rebuilt[0])
        self.assertIn("prompt_id=p-1", rebuilt[0])


if __name__ == "__main__":
    unittest.main()
