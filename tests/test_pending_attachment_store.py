"""Pending attachment store contract tests (docs/contracts/images.md §1, §4).

A real store over a temp dir: consume-once ``take`` semantics, the cross-key
expired purge, strict write validation, corruption-reads-as-empty, and
restart survival (a second store instance over the same dir).
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from kite.stores.pending_attachment_store import (
    PendingAttachmentRecord,
    PendingAttachmentStore,
)


def make_record(
    *,
    sender: str = "ou_a",
    chat_id: str = "oc_a",
    message_id: str = "om_1",
    display_name: str = "photo.png",
    media_type: str = "image/png",
    local_path: str = "/work/_feishu_attachments/photo.png",
    created_at: float = 1000.0,
    expires_at: float = 1600.0,
) -> PendingAttachmentRecord:
    return PendingAttachmentRecord(
        sender_open_id=sender,
        chat_id=chat_id,
        message_id=message_id,
        attachment_type="image",
        display_name=display_name,
        media_type=media_type,
        local_path=local_path,
        created_at=created_at,
        expires_at=expires_at,
    )


class PendingAttachmentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = pathlib.Path(self._tmp.name)
        self.store = PendingAttachmentStore(self.data_dir)

    def test_add_and_list_all_roundtrip(self) -> None:
        self.store.add(make_record(message_id="om_2"))
        self.store.add(make_record(message_id="om_1"))

        records = self.store.list_all()

        self.assertEqual([r.message_id for r in records], ["om_1", "om_2"])
        self.assertEqual(records[0].media_type, "image/png")

    def test_add_rejects_invalid_records(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add(make_record(sender="  "))
        with self.assertRaises(ValueError):
            self.store.add(make_record(chat_id=""))
        with self.assertRaises(ValueError):
            self.store.add(make_record(message_id=""))
        with self.assertRaises(ValueError):
            self.store.add(make_record(local_path=""))
        with self.assertRaises(ValueError):
            self.store.add(make_record(created_at=1000.0, expires_at=1000.0))
        with self.assertRaises(ValueError):
            self.store.add(
                PendingAttachmentRecord(
                    sender_open_id="ou_a",
                    chat_id="oc_a",
                    message_id="om_1",
                    attachment_type="file",  # first cut admits images only
                    display_name="x",
                    media_type="",
                    local_path="/work/x",
                    created_at=1000.0,
                    expires_at=1600.0,
                )
            )
        # Nothing was persisted by the rejected writes.
        self.assertEqual(self.store.list_all(), ())

    def test_take_is_consume_once_for_the_key(self) -> None:
        self.store.add(make_record(message_id="om_1"))
        self.store.add(make_record(message_id="om_2"))

        active, expired = self.store.take(sender_open_id="ou_a", chat_id="oc_a", now=1200.0)

        self.assertEqual([r.message_id for r in active], ["om_1", "om_2"])
        self.assertEqual(expired, ())
        # Second take: the key's records are gone (consume-once).
        active2, expired2 = self.store.take(sender_open_id="ou_a", chat_id="oc_a", now=1200.0)
        self.assertEqual(active2, ())
        self.assertEqual(expired2, ())

    def test_take_scopes_by_sender_and_chat(self) -> None:
        self.store.add(make_record(sender="ou_a", chat_id="oc_a", message_id="om_1"))
        self.store.add(make_record(sender="ou_b", chat_id="oc_a", message_id="om_2"))
        self.store.add(make_record(sender="ou_a", chat_id="oc_b", message_id="om_3"))

        active, _ = self.store.take(sender_open_id="ou_a", chat_id="oc_a", now=1200.0)

        self.assertEqual([r.message_id for r in active], ["om_1"])
        self.assertEqual(len(self.store.list_all()), 2)

    def test_take_splits_expired_and_purges_other_keys_expired(self) -> None:
        # Target key: one active, one expired.
        self.store.add(make_record(message_id="om_old", created_at=100.0, expires_at=200.0))
        self.store.add(make_record(message_id="om_new", created_at=1100.0, expires_at=1700.0))
        # Other key: one expired (purged by the sweep), one active (kept).
        self.store.add(
            make_record(sender="ou_b", message_id="om_b_old", created_at=100.0, expires_at=200.0)
        )
        self.store.add(
            make_record(sender="ou_b", message_id="om_b_new", created_at=1100.0, expires_at=1700.0)
        )

        active, expired = self.store.take(sender_open_id="ou_a", chat_id="oc_a", now=1200.0)

        self.assertEqual([r.message_id for r in active], ["om_new"])
        self.assertEqual(
            sorted(r.message_id for r in expired), ["om_b_old", "om_old"]
        )
        # Only the other key's active record survives in the store.
        self.assertEqual([r.message_id for r in self.store.list_all()], ["om_b_new"])

    def test_take_rejects_empty_key(self) -> None:
        with self.assertRaises(ValueError):
            self.store.take(sender_open_id="", chat_id="oc_a", now=1.0)

    def test_cleanup_expired(self) -> None:
        self.store.add(make_record(message_id="om_old", created_at=100.0, expires_at=200.0))
        self.store.add(make_record(message_id="om_new", created_at=1100.0, expires_at=1700.0))

        expired = self.store.cleanup_expired(now=1200.0)

        self.assertEqual([r.message_id for r in expired], ["om_old"])
        self.assertEqual([r.message_id for r in self.store.list_all()], ["om_new"])

    def test_records_survive_a_store_recreate(self) -> None:
        # Restart survival (contract §4): a fresh store instance over the
        # same data dir sees the persisted records.
        self.store.add(make_record())

        reloaded = PendingAttachmentStore(self.data_dir)

        self.assertEqual(len(reloaded.list_all()), 1)
        self.assertEqual(reloaded.list_all()[0].message_id, "om_1")

    def test_corrupt_file_reads_as_empty_and_self_heals(self) -> None:
        path = self.data_dir / "pending_attachments.json"
        path.write_text("{not json", encoding="utf-8")

        self.assertEqual(self.store.list_all(), ())

        self.store.add(make_record())
        self.assertEqual(len(self.store.list_all()), 1)

    def test_wrong_schema_version_reads_as_empty(self) -> None:
        path = self.data_dir / "pending_attachments.json"
        path.write_text(
            json.dumps({"schema_version": 999, "attachments": []}), encoding="utf-8"
        )

        self.assertEqual(self.store.list_all(), ())


if __name__ == "__main__":
    unittest.main()
