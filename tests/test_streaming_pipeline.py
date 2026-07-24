"""Volatile streaming pipeline tests (streaming-cards contract §3, §5).

The harness mirrors test_event_pipeline: the same fakes (Feishu transport,
kap REST, manual timers) and a real RuntimeLoop; the coalescing dispatcher
is replaced by a synchronous fake so render timing is test-driven (the real
dispatcher is covered by test_patch_dispatcher).
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from test_event_pipeline import (
    CHAT_ID,
    CHAT_ID_2,
    FakeInteractionRest,
    FakeTransport,
    ManualTimerFactory,
    kap_event,
    make_snapshot,
    turn_started,
)

from kite import cards
from kite.adapters.kap_server import (
    AssistantDelta,
    KapEvent,
    KapTransportError,
    SessionSnapshot,
)
from kite.event_pipeline import EventPipeline
from kite.prompt_ownership import PromptOwnership
from kite.runtime_loop import RuntimeLoop
from kite.stores.binding_store import BindingStore
from kite.stores.event_cursor_store import EventCursorStore
from kite.stores.terminal_result_store import TerminalResultStore

SESSION_ID = "s-1"


class StreamingFakeTransport(FakeTransport):
    """Adds the plain-text rescue send (message id returning)."""

    def __init__(self) -> None:
        super().__init__()
        self.text_sends: list[dict] = []
        self._text_counter = 0

    def reply_get_id(
        self,
        chat_id: str,
        text: str,
        *,
        parent_message_id: str = "",
        reply_in_thread: bool = False,
    ) -> str:
        self._text_counter += 1
        message_id = f"om_text_{self._text_counter}"
        self.text_sends.append({"chat_id": chat_id, "text": text, "message_id": message_id})
        return message_id


class CountingRest(FakeInteractionRest):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_calls = 0

    def get_snapshot(self, session_id: str) -> SessionSnapshot:
        self.snapshot_calls += 1
        return super().get_snapshot(session_id)


class FakeDispatcher:
    """Synchronous stand-in for CardPatchDispatcher.

    Submits are recorded; ``flush()`` invokes the queued render-thunks (the
    same full-snapshot renders the real dispatcher would produce) so tests
    control patch timing exactly.
    """

    def __init__(self) -> None:
        self.submitted: list[tuple[str, object]] = []
        self.applied: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.shutdown_called = False

    def submit(self, message_id: str, render) -> None:
        self.submitted.append((message_id, render))

    def cancel(self, message_id: str) -> None:
        self.cancelled.append(message_id)

    def flush(self) -> None:
        while self.submitted:
            message_id, render = self.submitted.pop(0)
            content = render()
            if content is not None:
                self.applied.append((message_id, content))

    def shutdown(self) -> None:
        self.shutdown_called = True

    def applied_to(self, message_id: str) -> list[str]:
        return [content for mid, content in self.applied if mid == message_id]


def rendered(serialized_card: str) -> str:
    """Readable form of a serialized card for content assertions."""
    return json.dumps(json.loads(serialized_card), ensure_ascii=False)


def make_in_flight_snapshot(
    *,
    current_prompt_id: str = "p-1",
    turn_id: int = 1,
    assistant_text: str = "",
) -> SessionSnapshot:
    snapshot = make_snapshot(
        busy=True,
        current_prompt_id=current_prompt_id,
        in_flight=True,
        turn_id=turn_id,
    )
    # make_snapshot predates the streaming field; attach the in-flight text.
    return SessionSnapshot(
        as_of_seq=snapshot.as_of_seq,
        epoch=snapshot.epoch,
        busy=snapshot.busy,
        pending_interaction=snapshot.pending_interaction,
        current_prompt_id=snapshot.current_prompt_id,
        in_flight=snapshot.in_flight,
        pending_approval_ids=snapshot.pending_approval_ids,
        pending_question_ids=snapshot.pending_question_ids,
        in_flight_turn_id=snapshot.in_flight_turn_id,
        pending_approvals=snapshot.pending_approvals,
        pending_questions=snapshot.pending_questions,
        in_flight_assistant_text=assistant_text,
    )


class StreamingPipelineTestCase(unittest.TestCase):
    stream_patch_interval = 0.7
    terminal_byte_budget = 26000

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = pathlib.Path(self._tmp.name)
        self.store = BindingStore(self.data_dir)
        self.terminal_store = TerminalResultStore(self.data_dir)
        self.cursor_store = EventCursorStore(self.data_dir)
        self.transport = StreamingFakeTransport()
        self.rest = CountingRest()
        self.rest.add_session(SESSION_ID)
        self.loop = RuntimeLoop(name="test-stream-loop")
        self.addCleanup(self.loop.stop)
        self.timers = ManualTimerFactory()
        self.ownership = PromptOwnership()
        self.dispatcher = FakeDispatcher()
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
            stream_patch_interval_seconds=self.stream_patch_interval,
            terminal_card_byte_budget=self.terminal_byte_budget,
        )

    # -- helpers -----------------------------------------------------------

    def flush(self) -> None:
        self.loop.call(lambda: None)

    def bind(self, chat_id: str = CHAT_ID, session_id: str = SESSION_ID) -> None:
        self.store.save(
            chat_id,
            {
                "session_id": session_id,
                "attached": True,
                "permission_mode": "auto",
                "plan_mode": False,
            },
        )

    def feed(self, event: KapEvent) -> None:
        self.pipeline.handle_event(event)
        self.flush()

    def delta(self, offset: int, text: str, *, session_id: str = SESSION_ID) -> None:
        self.pipeline.handle_volatile(
            AssistantDelta(session_id=session_id, offset=offset, text_delta=text)
        )
        self.flush()

    def start_prompt(
        self, *, chat_id: str = CHAT_ID, prompt_id: str = "p-1", turn_id: int = 1
    ) -> str:
        self.rest.set_prompts(SESSION_ID, active=prompt_id)
        self.feed(turn_started(turn_id=turn_id, prompt="做点事"))
        sent = self.transport.cards_to(chat_id)
        assert sent, "expected an execution card"
        return sent[-1]["message_id"]


class StreamIntoCardTests(StreamingPipelineTestCase):
    def test_delta_streams_into_the_execution_card(self) -> None:
        self.bind()
        message_id = self.start_prompt()

        self.delta(0, "你好")
        # Idle card: the first delta patches immediately (throttle §3.3).
        self.assertEqual([mid for mid, _ in self.dispatcher.submitted], [message_id])
        self.assertEqual(self.timers.created, [])
        self.delta(2, "，世界")

        self.dispatcher.flush()
        applied = self.dispatcher.applied_to(message_id)
        self.assertTrue(applied)
        self.assertIn("你好，世界", rendered(applied[-1]))

    def test_throttle_single_trailing_timer_renders_latest_state(self) -> None:
        self.bind()
        message_id = self.start_prompt()

        self.delta(0, "一")
        self.assertEqual(len(self.dispatcher.submitted), 1)
        self.delta(1, "二")
        # Within the interval: exactly one trailing timer, no new submit.
        self.assertEqual(len(self.dispatcher.submitted), 1)
        self.assertEqual(len(self.timers.live), 1)
        self.assertAlmostEqual(self.timers.live[0].delay, self.stream_patch_interval, places=1)
        self.delta(2, "三")
        self.assertEqual(len(self.timers.live), 1)  # still the single timer

        self.timers.live[0].fire()
        self.flush()
        # The immediate patch plus exactly one trailing flush, and nothing else.
        self.assertEqual(len(self.dispatcher.submitted), 2)
        self.dispatcher.flush()
        applied = self.dispatcher.applied_to(message_id)
        self.assertEqual(len(applied), 2)
        self.assertIn("一二三", rendered(applied[-1]))

    def test_durable_tool_patch_carries_the_streamed_body(self) -> None:
        # Full-snapshot invariant (§3.1): a durable-event patch re-renders the
        # whole card including the accumulated stream.
        self.bind()
        message_id = self.start_prompt()
        self.delta(0, "流式正文")

        self.feed(
            kap_event(
                "tool.call.started",
                {"turnId": 1, "toolCallId": "tc-1", "name": "Bash",
                 "display": {"kind": "command", "command": "ls"}},
            )
        )
        patches = self.transport.patches_to(message_id)
        self.assertEqual(len(patches), 1)
        rendered = json.dumps(patches[0], ensure_ascii=False)
        self.assertIn("流式正文", rendered)
        self.assertIn("Bash", rendered)

    def test_unclosed_fence_mid_stream_renders_tolerantly(self) -> None:
        self.bind()
        message_id = self.start_prompt()
        self.delta(0, "看代码：\n```python\nprint(")
        self.dispatcher.flush()
        applied = self.dispatcher.applied_to(message_id)
        self.assertEqual(len(applied), 1)
        rendered = json.dumps(json.loads(applied[0]), ensure_ascii=False)
        self.assertIn("```python", rendered)
        self.assertIn("print(", rendered)

    def test_delta_without_active_prompt_is_dropped(self) -> None:
        self.bind()
        self.delta(0, "没人接收")
        self.assertEqual(self.dispatcher.submitted, [])
        self.assertEqual(self.transport.sent, [])

    def test_target_matching_only_the_anchor_prompts_card(self) -> None:
        other_session = "s-2"
        self.rest.add_session(other_session, title="另一个会话")
        self.bind(CHAT_ID, SESSION_ID)
        self.bind(CHAT_ID_2, other_session)
        message_id = self.start_prompt(chat_id=CHAT_ID)

        # A delta for the other session (no turn started there) mutates nothing.
        self.delta(0, "别的会话", session_id=other_session)
        self.assertEqual(self.dispatcher.submitted, [])

        self.delta(0, "本会话")
        self.assertEqual([mid for mid, _ in self.dispatcher.submitted], [message_id])

    def test_fan_out_two_attached_chats_each_get_their_own_stream(self) -> None:
        self.bind(CHAT_ID)
        self.bind(CHAT_ID_2)
        self.rest.set_prompts(SESSION_ID, active="p-1")
        self.feed(turn_started(prompt="做点事"))
        first = self.transport.cards_to(CHAT_ID)[-1]["message_id"]
        second = self.transport.cards_to(CHAT_ID_2)[-1]["message_id"]
        self.assertNotEqual(first, second)

        self.delta(0, "广播正文")
        self.assertEqual(
            sorted(mid for mid, _ in self.dispatcher.submitted), sorted([first, second])
        )
        self.dispatcher.flush()
        for message_id in (first, second):
            applied = self.dispatcher.applied_to(message_id)
            self.assertEqual(len(applied), 1)
            self.assertIn("广播正文", rendered(applied[0]))


class GapRebuildTests(StreamingPipelineTestCase):
    def test_offset_gap_triggers_exactly_one_rebuild_and_heals(self) -> None:
        self.bind()
        message_id = self.start_prompt()
        # The snapshot the gap rebuild heals from (upstream's tracker applies
        # deltas before fan-out, so it already includes the lost text).
        self.rest.snapshots[SESSION_ID] = make_in_flight_snapshot(
            assistant_text="abcdefghij"
        )

        self.delta(0, "ab")
        self.assertEqual(len(self.dispatcher.submitted), 1)
        # The offset jump is a gap: exactly one snapshot rebuild (§4.1) —
        # never guess the missing text.
        self.delta(10, "zz")
        self.assertEqual(self.rest.snapshot_calls, 1)

        # The rebuild reseeded the transcript from the snapshot's in-flight
        # text; the wholesale refresh already rendered it synchronously.
        patches = self.transport.patches_to(message_id)
        self.assertTrue(any("abcdefghij" in json.dumps(p, ensure_ascii=False) for p in patches))
        # Streaming resumes at the re-baselined offset with no further rebuilds.
        self.delta(10, "k")
        self.delta(11, "!")
        self.assertEqual(self.rest.snapshot_calls, 1)
        self.dispatcher.flush()
        applied = self.dispatcher.applied_to(message_id)
        self.assertTrue(applied)
        self.assertIn("abcdefghijk!", rendered(applied[-1]))

    def test_gap_rebuild_failure_freezes_the_card_unknown(self) -> None:
        self.bind()
        message_id = self.start_prompt()
        self.rest.snapshots[SESSION_ID] = KapTransportError("boom")

        self.delta(0, "ab")
        self.delta(10, "zzz")

        self.assertEqual(self.rest.snapshot_calls, 1)
        patches = self.transport.patches_to(message_id)
        self.assertTrue(patches)
        rendered = json.dumps(patches[-1], ensure_ascii=False)
        self.assertIn("状态未知", rendered)
        self.assertIn("kitectl session status", rendered)


class TerminalReconcileTests(StreamingPipelineTestCase):
    def test_turn_ended_reconciles_authoritative_text_before_freezing(self) -> None:
        self.bind()
        message_id = self.start_prompt()
        self.delta(0, "abc")
        self.rest.assistant_text = "abcdef"

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        patches = self.transport.patches_to(message_id)
        self.assertTrue(patches)
        freeze = json.dumps(patches[-1], ensure_ascii=False)
        self.assertIn("已结束", freeze)
        self.assertIn("abcdef", freeze)

    def test_reconcile_never_shrinks_longer_streamed_content(self) -> None:
        self.bind()
        message_id = self.start_prompt()
        self.delta(0, "abcdef")
        self.rest.assistant_text = "abc"  # stale shorter read

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        # The frozen execution card keeps the longer streamed content (§3.5
        # never-shrink); the terminal card carries the authoritative text.
        freeze = json.dumps(self.transport.patches_to(message_id)[-1], ensure_ascii=False)
        self.assertIn("abcdef", freeze)
        terminal = self.transport.cards_to(CHAT_ID)[-1]["content"]
        rendered_terminal = json.dumps(terminal, ensure_ascii=False)
        self.assertIn("abc", rendered_terminal)
        self.assertNotIn("abcdef", rendered_terminal)

    def test_terminal_cancels_trailing_timer_and_queued_render(self) -> None:
        self.bind()
        message_id = self.start_prompt()
        self.rest.assistant_text = "一二"  # authoritative read matches the stream
        self.delta(0, "一")
        self.delta(1, "二")  # arms the trailing timer
        self.assertEqual(len(self.timers.live), 1)
        trailing = self.timers.live[0]

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        self.assertTrue(trailing.cancelled)
        self.assertIn(message_id, self.dispatcher.cancelled)
        # The queued render is a no-op after the terminal transition (§3.8).
        self.dispatcher.flush()
        self.assertEqual(self.dispatcher.applied_to(message_id), [])
        # The freeze patch is the last patch the card ever gets.
        freeze = json.dumps(self.transport.patches_to(message_id)[-1], ensure_ascii=False)
        self.assertIn("已结束", freeze)
        self.assertIn("一二", freeze)  # the streamed body survives freezing

    def test_terminal_card_send_failure_rescues_plain_text_once(self) -> None:
        self.bind()
        message_id = self.start_prompt()
        self.transport.fail_sends = True

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        self.assertEqual(len(self.transport.text_sends), 1)
        self.assertIn("最终答复文本", self.transport.text_sends[0]["text"])
        # The execution card is still frozen, and the store record points at
        # the rescued text message.
        self.assertIn("已结束", json.dumps(self.transport.patches_to(message_id)[-1], ensure_ascii=False))
        records = self.terminal_store.list_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].message_id, self.transport.text_sends[0]["message_id"])


class OverBudgetTerminalTests(StreamingPipelineTestCase):
    terminal_byte_budget = 100

    def test_over_budget_terminal_falls_back_to_plain_text(self) -> None:
        self.bind()
        self.start_prompt()
        self.rest.assistant_text = "一段很长的最终答复。" * 10

        self.feed(kap_event("turn.ended", {"turnId": 1, "reason": "completed"}))

        # No terminal CARD was sent (over the byte budget), but the content
        # survived as plain text, persisted for /last-style reads (§3.7).
        sent = self.transport.cards_to(CHAT_ID)
        self.assertEqual(len(sent), 1)  # only the execution card
        self.assertEqual(len(self.transport.text_sends), 1)
        self.assertIn("一段很长的最终答复", self.transport.text_sends[0]["text"])
        records = self.terminal_store.list_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].message_id, self.transport.text_sends[0]["message_id"])


class ShutdownHygieneTests(StreamingPipelineTestCase):
    def test_shutdown_cancels_trailing_timer_and_dispatcher(self) -> None:
        self.bind()
        self.start_prompt()
        self.delta(0, "一")
        self.delta(1, "二")
        self.assertEqual(len(self.timers.live), 1)
        trailing = self.timers.live[0]

        self.pipeline.shutdown()
        self.flush()

        self.assertTrue(trailing.cancelled)
        self.assertTrue(self.dispatcher.shutdown_called)


if __name__ == "__main__":
    unittest.main()
