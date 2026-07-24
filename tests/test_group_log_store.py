"""GroupLogStore contract tests (group-chat contract §1/§4).

The assistant log axis: JSONL append with monotonic seq, the boundary triple
set/get, the per-chat size cap, fail-closed reads (corruption reads as an
empty log), strict writes, and restart reload.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from kite.stores.group_log_store import (
    GROUP_LOG_STORE_SCHEMA_VERSION,
    GroupLogStore,
)

CHAT_ID = "oc_group"
OTHER_CHAT_ID = "oc_group_2"


def make_entry(message_id: str = "om_1", **overrides) -> dict:
    entry = {
        "message_id": message_id,
        "created_at": 1720000000000,
        "sender_open_id": "ou_member",
        "sender_type": "user",
        "sender_name": "成员小王",
        "msg_type": "text",
        "text": "你好",
    }
    entry.update(overrides)
    return entry


class GroupLogStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = pathlib.Path(self._tmp.name)
        self.store = GroupLogStore(self.data_dir)

    def _write_state_raw(self, payload: object) -> None:
        (self.data_dir / "group_log_state.json").write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )

    def _write_log_raw(self, chat_id: str, lines: list[str]) -> None:
        path = self.store.log_path(chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class AppendAndReadTests(GroupLogStoreTestCase):
    def test_append_returns_monotonic_seq(self) -> None:
        self.assertEqual(self.store.append(CHAT_ID, make_entry("om_1")), 1)
        self.assertEqual(self.store.append(CHAT_ID, make_entry("om_2")), 2)
        self.assertEqual(self.store.append(OTHER_CHAT_ID, make_entry("om_3")), 1)
        self.assertEqual(self.store.append(CHAT_ID, make_entry("om_4")), 3)

    def test_entries_since_returns_entries_after_seq_in_order(self) -> None:
        self.store.append(CHAT_ID, make_entry("om_1", text="一"))
        self.store.append(CHAT_ID, make_entry("om_2", text="二"))
        self.store.append(CHAT_ID, make_entry("om_3", text="三"))
        entries = self.store.entries_since(CHAT_ID, 1)
        self.assertEqual([e["seq"] for e in entries], [2, 3])
        self.assertEqual([e["text"] for e in entries], ["二", "三"])
        self.assertEqual(entries[0]["sender_name"], "成员小王")

    def test_entries_since_missing_log_is_empty(self) -> None:
        self.assertEqual(self.store.entries_since(CHAT_ID, 0), [])

    def test_entries_since_zero_returns_everything(self) -> None:
        self.store.append(CHAT_ID, make_entry("om_1"))
        self.store.append(CHAT_ID, make_entry("om_2"))
        self.assertEqual(len(self.store.entries_since(CHAT_ID, 0)), 2)

    def test_logs_are_isolated_per_chat(self) -> None:
        self.store.append(CHAT_ID, make_entry("om_1"))
        self.store.append(OTHER_CHAT_ID, make_entry("om_2"))
        self.assertEqual(len(self.store.entries_since(CHAT_ID, 0)), 1)
        self.assertEqual(len(self.store.entries_since(OTHER_CHAT_ID, 0)), 1)

    def test_append_persists_across_instances(self) -> None:
        self.store.append(CHAT_ID, make_entry("om_1", text="旧消息"))
        reloaded = GroupLogStore(self.data_dir)
        entries = reloaded.entries_since(CHAT_ID, 0)
        self.assertEqual([e["text"] for e in entries], ["旧消息"])
        # The seq counter survived the restart too.
        self.assertEqual(reloaded.append(CHAT_ID, make_entry("om_2")), 2)


class SizeCapTests(GroupLogStoreTestCase):
    def test_oldest_lines_dropped_beyond_cap(self) -> None:
        store = GroupLogStore(self.data_dir, size_cap=5)
        for index in range(1, 8):
            self.assertEqual(store.append(CHAT_ID, make_entry(f"om_{index}")), index)
        entries = store.entries_since(CHAT_ID, 0)
        # Only the newest 5 lines remain; the seq counter keeps moving.
        self.assertEqual([e["seq"] for e in entries], [3, 4, 5, 6, 7])
        # ...and the cap survives a restart (the file itself was rewritten).
        reloaded = GroupLogStore(self.data_dir, size_cap=5)
        self.assertEqual(len(reloaded.entries_since(CHAT_ID, 0)), 5)

    def test_default_cap_is_200(self) -> None:
        for index in range(1, 202):
            self.store.append(CHAT_ID, make_entry(f"om_{index}"))
        entries = self.store.entries_since(CHAT_ID, 0)
        self.assertEqual(len(entries), 200)
        self.assertEqual(entries[0]["seq"], 2)


class BoundaryTests(GroupLogStoreTestCase):
    def test_default_boundary_is_zero(self) -> None:
        self.assertEqual(
            self.store.boundary(CHAT_ID),
            {"seq": 0, "created_at": 0, "message_ids": []},
        )

    def test_set_boundary_roundtrip(self) -> None:
        boundary = self.store.set_boundary(
            CHAT_ID,
            {"seq": 3, "created_at": 1720000000123, "message_ids": ["om_b", "om_a"]},
        )
        # message_ids are normalized (sorted, deduped).
        self.assertEqual(boundary["message_ids"], ["om_a", "om_b"])
        loaded = self.store.boundary(CHAT_ID)
        self.assertEqual(loaded["seq"], 3)
        self.assertEqual(loaded["created_at"], 1720000000123)
        self.assertEqual(loaded["message_ids"], ["om_a", "om_b"])

    def test_boundary_persists_across_instances(self) -> None:
        self.store.set_boundary(
            CHAT_ID, {"seq": 7, "created_at": 1720000000456, "message_ids": ["om_x"]}
        )
        reloaded = GroupLogStore(self.data_dir)
        self.assertEqual(reloaded.boundary(CHAT_ID)["seq"], 7)
        self.assertEqual(reloaded.boundary(CHAT_ID)["message_ids"], ["om_x"])

    def test_set_boundary_preserves_log_seq(self) -> None:
        self.store.append(CHAT_ID, make_entry("om_1"))
        self.store.set_boundary(
            CHAT_ID, {"seq": 1, "created_at": 1720000000000, "message_ids": ["om_1"]}
        )
        self.assertEqual(self.store.append(CHAT_ID, make_entry("om_2")), 2)

    def test_boundaries_are_isolated_per_chat(self) -> None:
        self.store.set_boundary(
            CHAT_ID, {"seq": 1, "created_at": 1, "message_ids": []}
        )
        self.assertEqual(self.store.boundary(OTHER_CHAT_ID)["seq"], 0)


class StrictWriteValidationTests(GroupLogStoreTestCase):
    def test_empty_chat_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.append("  ", make_entry())
        with self.assertRaises(ValueError):
            self.store.entries_since("", 0)
        with self.assertRaises(ValueError):
            self.store.boundary("")

    def test_entry_requires_message_id(self) -> None:
        with self.assertRaises(ValueError):
            self.store.append(CHAT_ID, make_entry(message_id=" "))

    def test_entry_requires_text(self) -> None:
        with self.assertRaises(ValueError):
            self.store.append(CHAT_ID, make_entry(text=""))

    def test_entry_requires_non_negative_created_at(self) -> None:
        with self.assertRaises(ValueError):
            self.store.append(CHAT_ID, make_entry(created_at=-1))
        with self.assertRaises(ValueError):
            self.store.append(CHAT_ID, make_entry(created_at="now"))

    def test_boundary_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.store.set_boundary(CHAT_ID, {"seq": -1, "created_at": 0, "message_ids": []})
        with self.assertRaises(ValueError):
            self.store.set_boundary(
                CHAT_ID, {"seq": 1, "created_at": "soon", "message_ids": []}
            )
        with self.assertRaises(ValueError):
            self.store.set_boundary(
                CHAT_ID, {"seq": 1, "created_at": 0, "message_ids": "om_1"}  # type: ignore[dict-item]
            )


class FailClosedReadTests(GroupLogStoreTestCase):
    """Contract §4: corruption reads as an empty log, never raises."""

    def test_unparseable_state_file_reads_as_empty(self) -> None:
        self._write_state_raw("{ not json")
        self.assertEqual(self.store.boundary(CHAT_ID), {"seq": 0, "created_at": 0, "message_ids": []})

    def test_unsupported_schema_version_reads_as_empty(self) -> None:
        self._write_state_raw(
            {
                "schema_version": GROUP_LOG_STORE_SCHEMA_VERSION + 1,
                "chats": {CHAT_ID: {"last_seq": 9, "boundary": {"seq": 9, "created_at": 1, "message_ids": []}}},
            }
        )
        self.assertEqual(self.store.boundary(CHAT_ID)["seq"], 0)

    def test_corrupt_chat_record_skips_only_that_chat(self) -> None:
        self._write_state_raw(
            {
                "schema_version": GROUP_LOG_STORE_SCHEMA_VERSION,
                "chats": {
                    CHAT_ID: {"last_seq": "many", "boundary": {}},
                    OTHER_CHAT_ID: {
                        "last_seq": 4,
                        "boundary": {"seq": 2, "created_at": 10, "message_ids": ["om_x"]},
                    },
                },
            }
        )
        self.assertEqual(self.store.boundary(CHAT_ID)["seq"], 0)
        self.assertEqual(self.store.boundary(OTHER_CHAT_ID)["seq"], 2)

    def test_corrupt_log_lines_are_skipped(self) -> None:
        self._write_log_raw(
            CHAT_ID,
            [
                "{ not json",
                json.dumps(["not", "an", "object"]),
                json.dumps(make_entry("om_1", seq=1)),
                json.dumps({"seq": "bad", "message_id": "om_2"}),
                json.dumps(make_entry("om_3", seq=3)),
            ],
        )
        entries = self.store.entries_since(CHAT_ID, 0)
        self.assertEqual([e["message_id"] for e in entries], ["om_1", "om_3"])

    def test_append_after_state_corruption_recovers(self) -> None:
        self._write_state_raw("{ not json")
        self.assertEqual(self.store.append(CHAT_ID, make_entry("om_1")), 1)
        reloaded = GroupLogStore(self.data_dir)
        self.assertEqual(reloaded.append(CHAT_ID, make_entry("om_2")), 2)


if __name__ == "__main__":
    unittest.main()
