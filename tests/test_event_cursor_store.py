import json
import os
import pathlib
import stat
import tempfile
import unittest

from kite.stores.event_cursor_store import EventCursor, EventCursorStore


class EventCursorStoreTests(unittest.TestCase):
    def _make_store(self) -> tuple[tempfile.TemporaryDirectory[str], EventCursorStore, pathlib.Path]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        return tempdir, EventCursorStore(data_dir), data_dir / "event_cursors.json"

    def test_get_missing_returns_none(self) -> None:
        _, store, _ = self._make_store()
        self.assertIsNone(store.get("session-1"))

    def test_set_and_get_round_trip(self) -> None:
        _, store, state_path = self._make_store()

        store.set("session-1", EventCursor(seq=42, epoch="epoch-a"))

        self.assertEqual(store.get("session-1"), EventCursor(seq=42, epoch="epoch-a"))
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], 1)
        self.assertEqual(raw["cursors"]["session-1"], {"seq": 42, "epoch": "epoch-a"})

    def test_set_overwrites_existing_cursor(self) -> None:
        _, store, _ = self._make_store()
        store.set("session-1", EventCursor(seq=1, epoch="epoch-a"))
        store.set("session-1", EventCursor(seq=2, epoch="epoch-b"))

        self.assertEqual(store.get("session-1"), EventCursor(seq=2, epoch="epoch-b"))

    def test_cursors_are_scoped_per_session(self) -> None:
        _, store, _ = self._make_store()
        store.set("session-1", EventCursor(seq=1, epoch="epoch-a"))
        store.set("session-2", EventCursor(seq=9, epoch="epoch-c"))

        self.assertEqual(store.get("session-1"), EventCursor(seq=1, epoch="epoch-a"))
        self.assertEqual(store.get("session-2"), EventCursor(seq=9, epoch="epoch-c"))

    def test_clear_removes_only_that_session(self) -> None:
        _, store, _ = self._make_store()
        store.set("session-1", EventCursor(seq=1, epoch="epoch-a"))
        store.set("session-2", EventCursor(seq=2, epoch="epoch-b"))

        store.clear("session-1")

        self.assertIsNone(store.get("session-1"))
        self.assertEqual(store.get("session-2"), EventCursor(seq=2, epoch="epoch-b"))
        store.clear("session-missing")
        self.assertEqual(store.get("session-2"), EventCursor(seq=2, epoch="epoch-b"))

    def test_set_rejects_invalid_arguments(self) -> None:
        _, store, _ = self._make_store()

        with self.assertRaisesRegex(ValueError, "session_id must not be empty"):
            store.set("  ", EventCursor(seq=1, epoch="epoch-a"))
        with self.assertRaisesRegex(ValueError, "seq must be a non-negative integer"):
            store.set("session-1", EventCursor(seq=-1, epoch="epoch-a"))
        with self.assertRaisesRegex(ValueError, "seq must be a non-negative integer"):
            store.set("session-1", EventCursor(seq=True, epoch="epoch-a"))
        with self.assertRaisesRegex(ValueError, "epoch must not be empty"):
            store.set("session-1", EventCursor(seq=1, epoch=" "))
        with self.assertRaisesRegex(ValueError, "session_id must not be empty"):
            store.get("")
        with self.assertRaisesRegex(ValueError, "session_id must not be empty"):
            store.clear("")

    def test_set_normalizes_epoch_whitespace(self) -> None:
        _, store, _ = self._make_store()

        store.set(" session-1 ", EventCursor(seq=0, epoch=" epoch-a "))

        self.assertEqual(store.get("session-1"), EventCursor(seq=0, epoch="epoch-a"))

    def test_corrupt_file_is_treated_as_empty(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text("{not json", encoding="utf-8")

        self.assertIsNone(store.get("session-1"))
        store.set("session-1", EventCursor(seq=7, epoch="epoch-a"))
        self.assertEqual(store.get("session-1"), EventCursor(seq=7, epoch="epoch-a"))

    def test_stale_schema_version_is_treated_as_empty(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps({"schema_version": 99, "cursors": {"session-1": {"seq": 1, "epoch": "e"}}}),
            encoding="utf-8",
        )

        self.assertIsNone(store.get("session-1"))

    def test_malformed_cursor_entries_are_ignored(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "cursors": {
                        "session-bad-seq": {"seq": -1, "epoch": "e"},
                        "session-bad-epoch": {"seq": 1, "epoch": ""},
                        "session-bool-seq": {"seq": True, "epoch": "e"},
                        "session-not-dict": "nope",
                        "session-ok": {"seq": 3, "epoch": "e"},
                    },
                }
            ),
            encoding="utf-8",
        )

        self.assertIsNone(store.get("session-bad-seq"))
        self.assertIsNone(store.get("session-bad-epoch"))
        self.assertIsNone(store.get("session-bool-seq"))
        self.assertIsNone(store.get("session-not-dict"))
        self.assertEqual(store.get("session-ok"), EventCursor(seq=3, epoch="e"))

    def test_write_is_atomic_private_and_locked(self) -> None:
        _, store, state_path = self._make_store()

        store.set("session-1", EventCursor(seq=1, epoch="epoch-a"))

        self.assertFalse(state_path.with_suffix(".json.tmp").exists())
        self.assertTrue((state_path.parent / "event_cursors.lock").exists())
        if os.name != "nt":
            mode = stat.S_IMODE(state_path.stat().st_mode)
            self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
