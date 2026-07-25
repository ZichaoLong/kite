"""ForwardAggregator contract tests (FOCUS forward_aggregator port).

The aggregation window is driven by a FakeTimer: tests fire the pending
timer manually instead of waiting out the real 2s window.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from kite.forward_aggregator import ForwardAggregator, MergedForwardBatch

ROOT_ID = "om-root"


class FakeTimer:
    """Scriptable timer: records its lifecycle, fired manually by the test."""

    def __init__(self, timeout: float, callback: Any, args: list[str]) -> None:
        self.timeout = timeout
        self.callback = callback
        self.args = args
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.callback(*self.args)


def make_item(
    message_id: str,
    *,
    msg_type: str = "text",
    text: str | None = None,
    content: str | None = None,
    sender_id: str = "ou_alice",
    sender_type: str = "user",
    create_time: Any = 1712476800000,  # 2024-04-07 08:00:00 UTC
    upper_message_id: str = "",
    body: Any = ...,
) -> Any:
    if content is None:
        content = json.dumps({"text": text}, ensure_ascii=False) if text is not None else "{}"
    return SimpleNamespace(
        message_id=message_id,
        msg_type=msg_type,
        upper_message_id=upper_message_id,
        sender=SimpleNamespace(id=sender_id, sender_type=sender_type),
        create_time=create_time,
        body=SimpleNamespace(content=content) if body is ... else body,
    )


class AggregatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.timers: list[FakeTimer] = []
        self.batches: list[MergedForwardBatch] = []
        self.names = {"ou_alice": "Alice", "ou_bob": "Bob"}
        self.aggregator = self.make_aggregator()

    def make_aggregator(self, **overrides: Any) -> ForwardAggregator:
        kwargs: dict[str, Any] = dict(
            on_batch=self.batches.append,
            name_of=self.name_of,
            window_seconds=2.0,
            timer_factory=self.timer_factory,
        )
        kwargs.update(overrides)
        return ForwardAggregator(**kwargs)

    def timer_factory(self, timeout: float, callback: Any, args: list[str]) -> FakeTimer:
        timer = FakeTimer(timeout, callback, args)
        self.timers.append(timer)
        return timer

    def name_of(self, open_id: str, *, sender_type: str = "user") -> str:
        if not open_id:
            return "unknown"
        return self.names.get(open_id, open_id[:8])

    def buffer(
        self,
        message_id: str = ROOT_ID,
        items: list[Any] | None = None,
        *,
        sender: str = "ou_admin",
        chat_id: str = "oc_chat",
    ) -> None:
        if items is None:
            items = [make_item(f"{message_id}-c1", text="hello")]
        self.aggregator.buffer(
            sender_open_id=sender,
            chat_id=chat_id,
            message_id=message_id,
            items=items,
        )


class AggregationWindowTests(AggregatorTestCase):
    def test_n_bundles_merge_into_one_dispatch(self) -> None:
        self.buffer("om-1", [make_item("om-c1", text="第一段")])
        self.buffer("om-2", [make_item("om-c2", text="第二段")])
        self.buffer("om-3", [make_item("om-c3", text="第三段")])

        self.assertEqual(len(self.timers), 3)
        self.assertEqual(self.timers[0].timeout, 2.0)
        self.assertTrue(self.timers[0].cancelled)
        self.assertTrue(self.timers[1].cancelled)
        self.assertFalse(self.timers[2].cancelled)
        self.assertTrue(all(timer.started for timer in self.timers))

        self.timers[2].fire()

        self.assertEqual(len(self.batches), 1)
        batch = self.batches[0]
        self.assertEqual(batch.chat_id, "oc_chat")
        self.assertEqual(batch.sender_open_id, "ou_admin")
        # The latest bundle's message anchors the reply.
        self.assertEqual(batch.message_id, "om-3")
        self.assertTrue(batch.text.startswith("<forwarded_messages>\n"))
        self.assertTrue(batch.text.endswith("\n</forwarded_messages>"))
        self.assertLess(batch.text.index("第一段"), batch.text.index("第二段"))
        self.assertLess(batch.text.index("第二段"), batch.text.index("第三段"))

    def test_timer_cancel_replace_on_rebuffer(self) -> None:
        self.buffer("om-1")
        first = self.timers[-1]
        self.assertTrue(first.started)
        self.assertFalse(first.cancelled)

        self.buffer("om-2")
        second = self.timers[-1]
        self.assertIsNot(first, second)
        self.assertTrue(first.cancelled)
        self.assertTrue(second.started)
        self.assertFalse(second.cancelled)

    def test_keys_are_per_sender_and_chat(self) -> None:
        self.buffer("om-1", sender="ou_a", chat_id="oc_1")
        self.buffer("om-2", sender="ou_b", chat_id="oc_1")
        self.buffer("om-3", sender="ou_a", chat_id="oc_2")

        self.assertEqual(len(self.timers), 3)
        self.assertFalse(any(timer.cancelled for timer in self.timers))

        self.timers[0].fire()
        self.timers[1].fire()
        self.timers[2].fire()
        self.assertEqual(len(self.batches), 3)

    def test_flush_without_pending_is_a_noop(self) -> None:
        self.aggregator._flush("ou_nobody", "oc_nowhere")
        self.assertEqual(self.batches, [])

    def test_no_renderable_content_dispatches_nothing(self) -> None:
        # Only the root itself comes back: no children to render.
        self.buffer("om-1", [make_item("om-1")])
        self.timers[-1].fire()
        self.assertEqual(self.batches, [])


class ClaimTests(AggregatorTestCase):
    """FOCUS stash-claim semantics (audit M12): the next plain text from the
    same (sender, chat) claims the buffered transcript within the window."""

    def test_claim_renders_stash_and_cancels_timer(self) -> None:
        self.buffer("om-1", [make_item("om-c1", text="转发的内容", sender_id="ou_alice")])

        batch = self.aggregator.claim("ou_admin", "oc_chat")

        self.assertIsNotNone(batch)
        assert batch is not None
        self.assertIn("<forwarded_messages>", batch.text)
        self.assertIn("Alice:", batch.text)
        self.assertIn("转发的内容", batch.text)
        self.assertEqual(batch.message_id, "om-1")
        self.assertTrue(self.timers[-1].cancelled)
        # The stash is gone: a second claim and the (cancelled) timer's late
        # fire both find nothing.
        self.assertIsNone(self.aggregator.claim("ou_admin", "oc_chat"))
        self.timers[-1].fire()
        self.assertEqual(self.batches, [])

    def test_claim_merges_every_buffered_bundle(self) -> None:
        self.buffer("om-1", [make_item("om-c1", text="第一段")])
        self.buffer("om-2", [make_item("om-c2", text="第二段")])

        batch = self.aggregator.claim("ou_admin", "oc_chat")

        self.assertIsNotNone(batch)
        assert batch is not None
        self.assertLess(batch.text.index("第一段"), batch.text.index("第二段"))
        self.assertEqual(batch.message_id, "om-2")

    def test_claim_is_keyed_per_sender_and_chat(self) -> None:
        self.buffer("om-1", sender="ou_a", chat_id="oc_1")

        self.assertIsNone(self.aggregator.claim("ou_b", "oc_1"))
        self.assertIsNone(self.aggregator.claim("ou_a", "oc_2"))
        # The stash survives a foreign claim and still flushes on the timer.
        self.timers[-1].fire()
        self.assertEqual(len(self.batches), 1)

    def test_claim_without_renderable_content_returns_none(self) -> None:
        self.buffer("om-1", [make_item("om-1")])

        self.assertIsNone(self.aggregator.claim("ou_admin", "oc_chat"))
        self.assertTrue(self.timers[-1].cancelled)


class ExpansionTests(AggregatorTestCase):
    def test_transcript_renders_senders_timestamps_and_text(self) -> None:
        items = [
            make_item("om-c1", text="看看这个", sender_id="ou_alice"),
            make_item("om-c2", text="不错", sender_id="ou_bob", create_time=0),
        ]
        self.buffer("om-1", items)
        self.timers[-1].fire()

        text = self.batches[0].text
        self.assertIn("[04-07 16:00:00] Alice:\n    看看这个", text)
        self.assertIn("[未知时间] Bob:\n    不错", text)

    def test_app_sender_gets_bot_marker(self) -> None:
        self.names["ou_helper"] = "小助手"
        items = [make_item("om-c1", text="自动回复", sender_id="ou_helper", sender_type="app")]
        self.buffer("om-1", items)
        self.timers[-1].fire()

        self.assertIn("小助手[机器人]:", self.batches[0].text)

    def test_unresolved_sender_falls_back_to_short_id(self) -> None:
        items = [make_item("om-c1", text="hi", sender_id="ou_stranger_xyz")]
        self.buffer("om-1", items)
        self.timers[-1].fire()

        self.assertIn("ou_stran:", self.batches[0].text)

    def test_recursive_nested_merge_forward(self) -> None:
        items = [
            make_item("om-c1", text="外层", sender_id="ou_alice"),
            make_item("om-nested", msg_type="merge_forward", sender_id="ou_bob"),
            make_item("om-g1", text="内层消息", sender_id="ou_alice", upper_message_id="om-nested"),
        ]
        self.buffer("om-1", items)
        self.timers[-1].fire()

        text = self.batches[0].text
        self.assertIn("Bob: [forwarded messages]", text)
        # The nested child is rendered one indent level deeper.
        self.assertIn("    [04-07 16:00:00] Alice:\n        内层消息", text)

    def test_depth_cap_truncates(self) -> None:
        aggregator = self.make_aggregator(max_depth=2)
        items = [
            make_item("om-l1", msg_type="merge_forward"),
            make_item("om-l2", msg_type="merge_forward", upper_message_id="om-l1"),
            make_item("om-l3", msg_type="merge_forward", upper_message_id="om-l2"),
            make_item("om-leaf", text="太深了", upper_message_id="om-l3"),
        ]
        aggregator.buffer(
            sender_open_id="ou_admin", chat_id="oc_chat", message_id="om-1", items=items
        )
        self.timers[-1].fire()

        text = self.batches[0].text
        self.assertIn("[嵌套转发层数过深，已截断]", text)
        self.assertNotIn("太深了", text)

    def test_item_cap(self) -> None:
        aggregator = self.make_aggregator(max_items=3)
        items = [make_item(f"om-c{i}", text=f"消息{i}") for i in range(5)]
        aggregator.buffer(
            sender_open_id="ou_admin", chat_id="oc_chat", message_id="om-1", items=items
        )
        self.timers[-1].fire()

        text = self.batches[0].text
        for i in range(3):
            self.assertIn(f"消息{i}", text)
        self.assertNotIn("消息3", text)
        self.assertNotIn("消息4", text)

    def test_per_item_error_isolation(self) -> None:
        class BadBody:
            @property
            def content(self) -> str:
                raise RuntimeError("boom")

        items = [
            make_item("om-c1", text="好的"),
            make_item("om-bad", body=BadBody()),
            make_item("om-c2", text="也好"),
        ]
        self.buffer("om-1", items)
        self.timers[-1].fire()

        self.assertEqual(len(self.batches), 1)
        text = self.batches[0].text
        self.assertIn("好的", text)
        self.assertIn("也好", text)

    def test_post_item_extracts_text(self) -> None:
        post = {
            "title": "",
            "content": [
                [{"tag": "text", "text": "第一段"}],
                [{"tag": "text", "text": "第二段"}],
            ],
        }
        items = [make_item("om-c1", msg_type="post", content=json.dumps(post, ensure_ascii=False))]
        self.buffer("om-1", items)
        self.timers[-1].fire()

        self.assertIn("第一段\n    第二段", self.batches[0].text)

    def test_attachment_and_unknown_types_render_labels(self) -> None:
        items = [
            make_item("om-c1", msg_type="image", content='{"image_key": "k"}'),
            make_item("om-c2", msg_type="file", content="not json"),
            make_item("om-c3", msg_type="hongbao"),
        ]
        self.buffer("om-1", items)
        self.timers[-1].fire()

        text = self.batches[0].text
        self.assertIn("[图片]", text)
        self.assertIn("[文件]", text)
        self.assertIn("[hongbao 消息]", text)

    def test_unparseable_text_content_degrades_to_type_label(self) -> None:
        items = [make_item("om-c1", msg_type="text", content="{not json")]
        self.buffer("om-1", items)
        self.timers[-1].fire()

        self.assertIn("[text 消息]", self.batches[0].text)


class CloseTests(AggregatorTestCase):
    def test_close_cancels_pending_timers(self) -> None:
        self.buffer("om-1")
        self.buffer("om-2", sender="ou_other", chat_id="oc_other")
        self.aggregator.close()

        self.assertTrue(all(timer.cancelled for timer in self.timers))

        # A timer that still fires (cancel lost the race) finds nothing.
        for timer in self.timers:
            timer.fire()
        self.assertEqual(self.batches, [])

    def test_buffer_after_close_fails_closed(self) -> None:
        self.aggregator.close()
        self.buffer("om-1")

        self.assertEqual(self.timers, [])
        self.assertEqual(self.batches, [])


if __name__ == "__main__":
    unittest.main()
