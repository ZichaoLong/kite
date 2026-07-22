import json
import pathlib
import tempfile
import unittest

from kite.stores.terminal_result_store import TerminalResultRecord, TerminalResultStore


def _record(
    message_id: str,
    text: str,
    *,
    recorded_at: float = 1.0,
    execution_message_id: str = "",
    terminal_result_id: str = "",
    session_id: str = "",
    checksum: str = "",
) -> TerminalResultRecord:
    return TerminalResultRecord(
        message_id=message_id,
        execution_message_id=execution_message_id,
        final_reply_text=text,
        recorded_at=recorded_at,
        terminal_result_id=terminal_result_id,
        session_id=session_id,
        checksum=checksum,
    )


class TerminalResultStoreTests(unittest.TestCase):
    def _make_store(self) -> tuple[tempfile.TemporaryDirectory[str], TerminalResultStore, pathlib.Path]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        return tempdir, TerminalResultStore(data_dir), data_dir / "terminal_results.json"

    def test_upsert_and_get_round_trip(self) -> None:
        _, store, state_path = self._make_store()

        store.upsert(_record("om_1", "final reply", recorded_at=10.0, session_id="session-1"))

        self.assertEqual(store.get("om_1"), "final reply")
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], 1)
        self.assertEqual(raw["results"][0]["session_id"], "session-1")

    def test_upsert_dedupes_by_message_id(self) -> None:
        _, store, _ = self._make_store()

        store.upsert(_record("om_1", "first", recorded_at=1.0))
        store.upsert(_record("om_1", "second", recorded_at=2.0))

        self.assertEqual(store.get("om_1"), "second")
        self.assertEqual(len(store.list_all()), 1)

    def test_upsert_ignores_incomplete_records(self) -> None:
        _, store, state_path = self._make_store()

        store.upsert(_record("", "text"))
        store.upsert(_record("om_1", ""))

        self.assertEqual(store.list_all(), ())
        self.assertFalse(state_path.exists())

    def test_get_missing_returns_empty_string(self) -> None:
        _, store, _ = self._make_store()
        self.assertEqual(store.get(""), "")
        self.assertEqual(store.get("om_missing"), "")

    def test_upsert_dedupes_by_terminal_result_id(self) -> None:
        _, store, _ = self._make_store()

        store.upsert(_record("om_1", "first", terminal_result_id="trid-abc"))
        store.upsert(_record("om_2", "second", terminal_result_id="trid-abc"))

        self.assertEqual(store.get("om_1"), "")
        self.assertEqual(store.get_by_terminal_result_id("trid-abc"), "second")
        self.assertEqual(len(store.list_all()), 1)

    def test_get_by_terminal_result_id_matches_checksum_prefix_and_session(self) -> None:
        _, store, _ = self._make_store()
        store.upsert(
            _record(
                "om_1",
                "kept",
                terminal_result_id="TRID-ABC",
                checksum="DEADBEEF1234",
                session_id="session-1",
            )
        )

        self.assertEqual(
            store.get_by_terminal_result_id("trid-abc", checksum="deadbeef", session_id="session-1"),
            "kept",
        )
        self.assertEqual(store.get_by_terminal_result_id("trid-abc"), "kept")
        self.assertEqual(store.get_by_terminal_result_id("trid-abc", session_id="session-2"), "")
        self.assertEqual(store.get_by_terminal_result_id("trid-abc", checksum="beef"), "")
        self.assertEqual(store.get_by_terminal_result_id(""), "")

    def test_latest_for_session_orders_by_recorded_at(self) -> None:
        _, store, _ = self._make_store()
        store.upsert(_record("om_old", "old", recorded_at=1.0, session_id="session-1"))
        store.upsert(_record("om_new", "new", recorded_at=2.0, session_id="session-1"))
        store.upsert(_record("om_other", "other", recorded_at=3.0, session_id="session-2"))

        self.assertEqual(store.latest_for_session("session-1"), "new")
        self.assertEqual(store.latest_for_session("session-2"), "other")
        self.assertEqual(store.latest_for_session("session-missing"), "")

    def test_has_execution_result(self) -> None:
        _, store, _ = self._make_store()
        store.upsert(_record("om_1", "done", execution_message_id="om_exec"))

        self.assertTrue(
            store.has_execution_result(execution_message_id="om_exec", final_reply_text="done")
        )
        self.assertFalse(
            store.has_execution_result(execution_message_id="om_exec", final_reply_text="changed")
        )
        self.assertFalse(store.has_execution_result(execution_message_id="", final_reply_text="done"))

    def test_list_all_sorts_by_recorded_at(self) -> None:
        _, store, _ = self._make_store()
        store.upsert(_record("om_b", "b", recorded_at=2.0))
        store.upsert(_record("om_a", "a", recorded_at=1.0))

        self.assertEqual([item.message_id for item in store.list_all()], ["om_a", "om_b"])

    def test_corrupt_file_is_fail_soft(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text("{not json", encoding="utf-8")

        # Reads and writes degrade to logged no-ops instead of raising.
        self.assertEqual(store.get("om_1"), "")
        self.assertEqual(store.list_all(), ())
        store.upsert(_record("om_1", "dropped"))
        self.assertEqual(store.get("om_1"), "")

        # Once the corrupt file is gone, the store recovers cleanly.
        state_path.unlink()
        store.upsert(_record("om_1", "recovered"))
        self.assertEqual(store.get("om_1"), "recovered")

    def test_stale_schema_version_is_fail_soft(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps({"schema_version": 99, "results": []}),
            encoding="utf-8",
        )

        self.assertEqual(store.list_all(), ())

    def test_write_is_atomic_without_leftover_tmp(self) -> None:
        _, store, state_path = self._make_store()

        store.upsert(_record("om_1", "text"))

        self.assertFalse(state_path.with_suffix(".json.tmp").exists())
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
