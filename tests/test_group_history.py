"""GroupHistoryRecovery contract tests (group-chat contract §3.3/§4.5).

Covers: local-log/REST-backfill merge, boundary-triple dedup (incl.
same-millisecond messages), self-app filtering, the
<group_chat_scope>/<group_chat_context>/<group_chat_current_turn> envelope
shape, the 50-message / 24h-lookback / 5s-slack limits, and fail-closed
fetch errors.

The fakes (FakeListMessages, make_history_item, make_render_text) are shared
with test_group_chat.py's assistant-mode ingress tests.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace

from kite.feishu_transport import ListedMessagesPage
from kite.group_history import GroupHistoryRecovery
from kite.stores.group_log_store import GroupLogStore

CHAT_ID = "oc_group"
APP_ID = "cli_kite_self"
NOW_MS = 1720000000000


class FakeListMessages:
    """Scriptable stand-in for FeishuTransport.list_messages."""

    def __init__(self) -> None:
        self.pages: list[ListedMessagesPage] = []
        self.error: Exception | None = None
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, chat_id: str, **kwargs) -> ListedMessagesPage:
        self.calls.append((chat_id, kwargs))
        if self.error is not None:
            raise self.error
        if self.pages:
            return self.pages.pop(0)
        return ListedMessagesPage(items=[])


def make_history_item(
    message_id: str,
    *,
    text: str = "消息",
    sender_id: str = "ou_member",
    sender_type: str = "user",
    create_time: int = NOW_MS,
    msg_type: str = "text",
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        msg_type=msg_type,
        sender=SimpleNamespace(id=sender_id, sender_type=sender_type),
        create_time=create_time,
        mentions=[],
        body=SimpleNamespace(content=json.dumps({"text": text}, ensure_ascii=False)),
    )


def make_render_text(msg_type: str, content_dict: dict, mentions: list) -> str:
    """Deterministic render port: plain extraction, no mention rewriting."""
    return str(content_dict.get("text") or "").strip()


def make_name_of(open_id: str, *, sender_type: str = "user") -> str:
    """Deterministic name port (mirrors the IdentityNames fallback chain)."""
    normalized = str(open_id or "").strip()
    if not normalized:
        return "unknown"
    if sender_type == "app":
        return f"机器人:{normalized[:8]}"
    return f"用户:{normalized[:8]}"


class GroupHistoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = pathlib.Path(self._tmp.name)
        self.log_store = GroupLogStore(self.data_dir)
        self.list_messages = FakeListMessages()
        self.recovery = self._make_recovery()

    def _make_recovery(self, **overrides) -> GroupHistoryRecovery:
        kwargs = dict(
            list_messages=self.list_messages,
            render_text=make_render_text,
            name_of=make_name_of,
            log_store=self.log_store,
            app_id=APP_ID,
        )
        kwargs.update(overrides)
        return GroupHistoryRecovery(**kwargs)

    def log_entry(self, message_id: str, *, created_at: int = NOW_MS, text: str = "消息", **overrides) -> int:
        entry = {
            "message_id": message_id,
            "created_at": created_at,
            "sender_open_id": "ou_member",
            "sender_type": "user",
            "sender_name": "成员小王",
            "msg_type": "text",
            "text": text,
        }
        entry.update(overrides)
        return self.log_store.append(CHAT_ID, entry)


class CollectContextTests(GroupHistoryTestCase):
    def test_local_entries_since_boundary_excluding_trigger(self) -> None:
        self.log_entry("om_1", text="边界前", created_at=NOW_MS - 3000)
        seq_2 = self.log_entry("om_2", text="边界", created_at=NOW_MS - 2000)
        self.log_store.set_boundary(
            CHAT_ID, {"seq": seq_2, "created_at": NOW_MS - 2000, "message_ids": ["om_2"]}
        )
        self.log_entry("om_3", text="上下文中", created_at=NOW_MS - 1000)
        trigger_seq = self.log_entry("om_4", text="触发", created_at=NOW_MS)
        entries = self.recovery.collect_context_entries(
            chat_id=CHAT_ID,
            current_message_id="om_4",
            current_create_time=NOW_MS,
            current_seq=trigger_seq,
        )
        self.assertEqual([e["message_id"] for e in entries], ["om_3"])

    def test_history_backfill_merged_and_sorted_with_local(self) -> None:
        self.log_entry("om_1", text="本地", created_at=NOW_MS - 500)
        self.list_messages.pages.append(
            ListedMessagesPage(
                items=[
                    make_history_item("om_h1", text="回捞早", create_time=NOW_MS - 900),
                    make_history_item("om_h2", text="回捞晚", create_time=NOW_MS - 100),
                ]
            )
        )
        entries = self.recovery.collect_context_entries(
            chat_id=CHAT_ID,
            current_message_id="om_now",
            current_create_time=NOW_MS,
            current_seq=0,
        )
        self.assertEqual(
            [e["message_id"] for e in entries], ["om_h1", "om_1", "om_h2"]
        )

    def test_history_dedups_against_local_log_and_current(self) -> None:
        self.log_entry("om_1", text="本地", created_at=NOW_MS - 100)
        self.list_messages.pages.append(
            ListedMessagesPage(
                items=[
                    make_history_item("om_1", text="重复", create_time=NOW_MS - 100),
                    make_history_item("om_now", text="触发自身", create_time=NOW_MS),
                    make_history_item("om_h1", text="新消息", create_time=NOW_MS - 50),
                ]
            )
        )
        entries = self.recovery.collect_context_entries(
            chat_id=CHAT_ID,
            current_message_id="om_now",
            current_create_time=NOW_MS,
            current_seq=0,
        )
        self.assertEqual([e["message_id"] for e in entries], ["om_1", "om_h1"])

    def test_boundary_triple_dedup_same_millisecond(self) -> None:
        # Boundary at ms T with id om_b recorded; the REST window (with 5s
        # slack) returns three messages at exactly T: the recorded one is
        # deduped, the unrecorded same-millisecond ones are kept.
        self.log_store.set_boundary(
            CHAT_ID, {"seq": 1, "created_at": NOW_MS - 1000, "message_ids": ["om_b"]}
        )
        self.list_messages.pages.append(
            ListedMessagesPage(
                items=[
                    make_history_item("om_old", text="太早", create_time=NOW_MS - 2000),
                    make_history_item("om_b", text="边界消息", create_time=NOW_MS - 1000),
                    make_history_item("om_same1", text="同毫秒一", create_time=NOW_MS - 1000),
                    make_history_item("om_same2", text="同毫秒二", create_time=NOW_MS - 1000),
                ]
            )
        )
        entries = self.recovery.collect_context_entries(
            chat_id=CHAT_ID,
            current_message_id="om_now",
            current_create_time=NOW_MS,
            current_seq=0,
        )
        self.assertEqual(
            [e["message_id"] for e in entries], ["om_same1", "om_same2"]
        )

    def test_self_app_messages_are_filtered(self) -> None:
        self.list_messages.pages.append(
            ListedMessagesPage(
                items=[
                    make_history_item(
                        "om_self", text="机器人自己的回复", sender_id=APP_ID, sender_type="app"
                    ),
                    make_history_item(
                        "om_other_bot", text="别的机器人", sender_id="cli_other", sender_type="app"
                    ),
                    make_history_item("om_user", text="成员消息", sender_id="ou_member"),
                ]
            )
        )
        entries = self.recovery.collect_context_entries(
            chat_id=CHAT_ID,
            current_message_id="om_now",
            current_create_time=NOW_MS,
            current_seq=0,
        )
        self.assertEqual([e["message_id"] for e in entries], ["om_other_bot", "om_user"])
        other_bot = entries[0]
        self.assertEqual(other_bot["sender_type"], "app")
        self.assertTrue(other_bot["sender_name"].startswith("机器人:"))

    def test_empty_text_items_are_skipped(self) -> None:
        self.list_messages.pages.append(
            ListedMessagesPage(
                items=[
                    make_history_item("om_empty", text=""),
                    make_history_item("om_ok", text="有内容"),
                ]
            )
        )
        entries = self.recovery.collect_context_entries(
            chat_id=CHAT_ID,
            current_message_id="om_now",
            current_create_time=NOW_MS,
            current_seq=0,
        )
        self.assertEqual([e["message_id"] for e in entries], ["om_ok"])

    def test_fetch_failure_propagates(self) -> None:
        # §4.5: the caller (AppHandler) turns this into the blocking notice.
        self.list_messages.error = RuntimeError("code=230001, msg=rate limited")
        with self.assertRaises(RuntimeError):
            self.recovery.collect_context_entries(
                chat_id=CHAT_ID,
                current_message_id="om_now",
                current_create_time=NOW_MS,
                current_seq=0,
            )

    def test_disabled_backfill_returns_local_only(self) -> None:
        recovery = self._make_recovery(fetch_limit=0)
        self.log_entry("om_1", text="本地")
        entries = recovery.collect_context_entries(
            chat_id=CHAT_ID,
            current_message_id="om_now",
            current_create_time=NOW_MS,
            current_seq=0,
        )
        self.assertEqual([e["message_id"] for e in entries], ["om_1"])
        self.assertEqual(self.list_messages.calls, [])


class LimitTests(GroupHistoryTestCase):
    def test_fetch_limit_caps_backfill_to_newest(self) -> None:
        recovery = self._make_recovery(fetch_limit=2)
        self.list_messages.pages.append(
            ListedMessagesPage(
                items=[
                    make_history_item("om_1", create_time=NOW_MS - 400),
                    make_history_item("om_2", create_time=NOW_MS - 300),
                    make_history_item("om_3", create_time=NOW_MS - 200),
                    make_history_item("om_4", create_time=NOW_MS - 100),
                ]
            )
        )
        entries = recovery.collect_context_entries(
            chat_id=CHAT_ID,
            current_message_id="om_now",
            current_create_time=NOW_MS,
            current_seq=0,
        )
        self.assertEqual([e["message_id"] for e in entries], ["om_3", "om_4"])

    def test_lookback_window_and_boundary_slack_in_request(self) -> None:
        recovery = self._make_recovery(lookback_seconds=3600, boundary_slack_seconds=5)
        # No boundary: start = end - lookback.
        recovery.collect_context_entries(
            chat_id=CHAT_ID,
            current_message_id="om_now",
            current_create_time=NOW_MS,
            current_seq=0,
        )
        _, kwargs = self.list_messages.calls[-1]
        self.assertEqual(kwargs["end_time"], str(int(NOW_MS / 1000)))
        self.assertEqual(kwargs["start_time"], str(int(NOW_MS / 1000) - 3600))
        self.assertEqual(kwargs["sort_type"], "ByCreateTimeAsc")

        # With a boundary inside the lookback: start = boundary - 5s slack.
        boundary_ms = NOW_MS - 60_000
        self.log_store.set_boundary(
            CHAT_ID, {"seq": 1, "created_at": boundary_ms, "message_ids": []}
        )
        recovery.collect_context_entries(
            chat_id=CHAT_ID,
            current_message_id="om_now",
            current_create_time=NOW_MS,
            current_seq=0,
        )
        _, kwargs = self.list_messages.calls[-1]
        self.assertEqual(kwargs["start_time"], str(int(boundary_ms / 1000) - 5))

    def test_pagination_follows_page_tokens(self) -> None:
        self.list_messages.pages.extend(
            [
                ListedMessagesPage(
                    items=[make_history_item("om_1", create_time=NOW_MS - 200)],
                    has_more=True,
                    page_token="tok-2",
                ),
                ListedMessagesPage(
                    items=[make_history_item("om_2", create_time=NOW_MS - 100)],
                    has_more=False,
                ),
            ]
        )
        entries = self.recovery.collect_context_entries(
            chat_id=CHAT_ID,
            current_message_id="om_now",
            current_create_time=NOW_MS,
            current_seq=0,
        )
        self.assertEqual([e["message_id"] for e in entries], ["om_1", "om_2"])
        self.assertEqual(self.list_messages.calls[1][1]["page_token"], "tok-2")


class BoundaryMessageIdsTests(GroupHistoryTestCase):
    def test_collects_trigger_plus_same_millisecond_ids(self) -> None:
        context = [
            {"message_id": "om_a", "created_at": NOW_MS - 1},
            {"message_id": "om_same1", "created_at": NOW_MS},
            {"message_id": "om_same2", "created_at": NOW_MS},
            {"message_id": "", "created_at": NOW_MS},
        ]
        ids = GroupHistoryRecovery.collect_boundary_message_ids(
            current_message_id="om_now",
            current_created_at=NOW_MS,
            context_entries=context,
        )
        self.assertEqual(ids, ["om_now", "om_same1", "om_same2"])

    def test_zero_created_at_yields_empty(self) -> None:
        ids = GroupHistoryRecovery.collect_boundary_message_ids(
            current_message_id="om_now",
            current_created_at=0,
            context_entries=[{"message_id": "om_a", "created_at": 0}],
        )
        self.assertEqual(ids, [])


class EnvelopeTests(GroupHistoryTestCase):
    def test_envelope_shape(self) -> None:
        self.log_entry("om_1", text="上下文消息", created_at=NOW_MS - 1000)
        entries = self.log_store.entries_since(CHAT_ID, 0)
        envelope = self.recovery.build_envelope(
            "现在该说什么？",
            sender_name="成员小王",
            context_entries=entries,
            log_path=self.log_store.log_path(CHAT_ID),
        )
        self.assertIn("<group_chat_scope>", envelope)
        self.assertIn("</group_chat_scope>", envelope)
        self.assertIn("<group_chat_context>", envelope)
        self.assertIn("群聊日志文件", envelope)
        self.assertIn("[#1 ", envelope)
        self.assertIn("上下文消息", envelope)
        self.assertIn("<group_chat_current_turn>", envelope)
        self.assertIn("sender_name: 成员小王", envelope)
        self.assertIn("现在该说什么？", envelope)
        self.assertIn("</group_chat_current_turn>", envelope)
        # The envelope orders scope -> context -> current turn.
        self.assertLess(envelope.index("<group_chat_scope>"), envelope.index("<group_chat_context>"))
        self.assertLess(envelope.index("<group_chat_context>"), envelope.index("<group_chat_current_turn>"))

    def test_envelope_empty_context_uses_placeholder(self) -> None:
        envelope = self.recovery.build_envelope(
            "你好",
            sender_name="成员小王",
            context_entries=[],
            log_path=self.log_store.log_path(CHAT_ID),
        )
        self.assertIn("暂无可用群聊消息", envelope)

    def test_format_entries_marks_non_text_and_bots(self) -> None:
        entries = [
            {
                "message_id": "om_1",
                "created_at": NOW_MS,
                "seq": 1,
                "sender_open_id": "ou_x",
                "sender_type": "user",
                "sender_name": "成员",
                "msg_type": "post",
                "text": "富文本内容",
            },
            {
                "message_id": "om_2",
                "created_at": NOW_MS,
                "sender_open_id": "",
                "sender_type": "app",
                "sender_name": "别的机器人",
                "msg_type": "text",
                "text": "机器人发言",
            },
        ]
        text = self.recovery.format_context_entries(entries)
        self.assertIn("(post)", text)
        self.assertIn("别的机器人[机器人]", text)
        # Backfill entries (no seq) render without the #seq prefix.
        self.assertNotIn("[#2", text)


if __name__ == "__main__":
    unittest.main()
