import json
import pathlib
import tempfile
import unittest

from kite.stores.binding_store import (
    BINDING_STORE_SCHEMA_VERSION,
    BindingStore,
)


def _binding(
    session_id: str = "session-1",
    *,
    attached: bool = True,
    permission_mode: str = "auto",
    plan_mode: bool = False,
    effort: str = "",
) -> dict:
    return {
        "session_id": session_id,
        "attached": attached,
        "permission_mode": permission_mode,
        "plan_mode": plan_mode,
        "effort": effort,
    }


class BindingStoreTests(unittest.TestCase):
    def _make_store(self) -> tuple[tempfile.TemporaryDirectory[str], BindingStore, pathlib.Path]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        return tempdir, BindingStore(data_dir), data_dir / "bindings.json"

    def test_load_missing_returns_none(self) -> None:
        _, store, _ = self._make_store()
        self.assertIsNone(store.load("oc_missing"))

    def test_save_and_load_round_trip(self) -> None:
        _, store, state_path = self._make_store()

        saved = store.save(
            "oc_chat",
            _binding("session-1", attached=False, permission_mode="yolo", plan_mode=True),
        )

        self.assertEqual(saved["session_id"], "session-1")
        loaded = store.load("oc_chat")
        self.assertEqual(loaded, saved)
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], BINDING_STORE_SCHEMA_VERSION)
        self.assertEqual(raw["bindings"]["oc_chat"]["session_id"], "session-1")
        self.assertEqual(raw["bindings"]["oc_chat"]["attached"], False)

    def test_permission_mode_and_plan_mode_persist_round_trip(self) -> None:
        _, store, state_path = self._make_store()

        store.save("oc_chat", _binding("session-1", permission_mode="manual", plan_mode=True))

        loaded = store.load("oc_chat")
        assert loaded is not None
        self.assertEqual(loaded["permission_mode"], "manual")
        self.assertEqual(loaded["plan_mode"], True)
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["bindings"]["oc_chat"]["permission_mode"], "manual")
        self.assertEqual(raw["bindings"]["oc_chat"]["plan_mode"], True)

        # Rewriting only the mode fields keeps the persisted session binding.
        store.save("oc_chat", _binding("session-1", permission_mode="yolo", plan_mode=False))
        reloaded = store.load("oc_chat")
        assert reloaded is not None
        self.assertEqual(reloaded["permission_mode"], "yolo")
        self.assertEqual(reloaded["plan_mode"], False)

    def test_effort_persists_round_trip(self) -> None:
        _, store, state_path = self._make_store()

        store.save(
            "oc_chat",
            _binding("session-1", effort="xhigh"),
        )

        loaded = store.load("oc_chat")
        assert loaded is not None
        self.assertEqual(loaded["effort"], "xhigh")
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["bindings"]["oc_chat"]["effort"], "xhigh")

        # Rewriting only the effort field keeps the rest of the binding.
        store.save("oc_chat", _binding("session-1", effort="off"))
        reloaded = store.load("oc_chat")
        assert reloaded is not None
        self.assertEqual(reloaded["effort"], "off")

    def test_defaults_applied_when_optional_fields_missing(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": BINDING_STORE_SCHEMA_VERSION,
                    "bindings": {"oc_chat": {"session_id": "session-1"}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        loaded = store.load("oc_chat")

        assert loaded is not None
        self.assertEqual(loaded["attached"], True)
        self.assertEqual(loaded["permission_mode"], "auto")
        self.assertEqual(loaded["plan_mode"], False)
        self.assertEqual(loaded["effort"], "")

    def test_save_applies_default_permission_mode_and_normalizes(self) -> None:
        _, store, _ = self._make_store()

        saved = store.save(
            " oc_chat ",
            {"session_id": " session-1 ", "attached": True, "permission_mode": "", "plan_mode": False},
        )

        self.assertEqual(saved["permission_mode"], "auto")
        self.assertEqual(saved["session_id"], "session-1")
        self.assertIsNotNone(store.load("oc_chat"))

    def test_load_all_returns_all_bindings(self) -> None:
        _, store, _ = self._make_store()
        store.save("oc_a", _binding("session-a"))
        store.save("oc_b", _binding("session-b", attached=False, permission_mode="yolo", plan_mode=True))

        loaded = store.load_all()

        self.assertEqual(set(loaded), {"oc_a", "oc_b"})
        self.assertEqual(loaded["oc_b"]["permission_mode"], "yolo")
        self.assertEqual(loaded["oc_b"]["plan_mode"], True)

    def test_clear_removes_single_binding(self) -> None:
        _, store, _ = self._make_store()
        store.save("oc_a", _binding("session-a"))
        store.save("oc_b", _binding("session-b"))

        store.clear("oc_a")

        self.assertIsNone(store.load("oc_a"))
        self.assertIsNotNone(store.load("oc_b"))
        store.clear("oc_missing")
        self.assertIsNotNone(store.load("oc_b"))

    def test_clear_all_removes_state_file(self) -> None:
        _, store, state_path = self._make_store()
        store.save("oc_a", _binding("session-a"))
        self.assertTrue(state_path.exists())

        store.clear_all()

        self.assertFalse(state_path.exists())

    def test_save_writes_atomically_without_leftover_tmp(self) -> None:
        _, store, state_path = self._make_store()

        store.save("oc_a", _binding("session-a"))

        tmp_path = state_path.with_suffix(".json.tmp")
        self.assertFalse(tmp_path.exists())
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], BINDING_STORE_SCHEMA_VERSION)

    def test_save_rejects_invalid_values(self) -> None:
        _, store, _ = self._make_store()

        with self.assertRaisesRegex(ValueError, "chat_id must not be empty"):
            store.save("", _binding("session-1"))
        with self.assertRaisesRegex(ValueError, "session_id cannot be empty"):
            store.save("oc_a", _binding(""))
        with self.assertRaisesRegex(ValueError, "permission_mode must be one of"):
            store.save("oc_a", _binding("session-1", permission_mode="plan"))
        with self.assertRaisesRegex(ValueError, "attached must be a boolean"):
            store.save("oc_a", _binding("session-1", attached="yes"))
        with self.assertRaisesRegex(ValueError, "plan_mode must be a boolean"):
            store.save("oc_a", _binding("session-1", plan_mode=1))
        with self.assertRaisesRegex(ValueError, "effort must be one of"):
            store.save("oc_a", _binding("session-1", effort="turbo"))
        with self.assertRaisesRegex(ValueError, "effort must be a string"):
            store.save("oc_a", _binding("session-1", effort=3))

    def test_load_rejects_corrupt_json(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text("{not json", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "invalid bindings.json"):
            store.load("oc_a")

    def test_load_rejects_missing_schema_version(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(json.dumps({"bindings": {}}), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "schema_version"):
            store.load("oc_a")

    def test_load_rejects_stale_schema_version(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps({"schema_version": 0, "bindings": {}}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "schema_version must be one of"):
            store.load("oc_a")

    def test_load_rejects_invalid_binding_state(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": BINDING_STORE_SCHEMA_VERSION,
                    "bindings": {"oc_chat": {"session_id": "session-1", "permission_mode": "on-request"}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "permission_mode must be one of"):
            store.load("oc_chat")

    def test_load_drops_unknown_fields(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": BINDING_STORE_SCHEMA_VERSION,
                    "bindings": {
                        "oc_chat": {
                            "session_id": "session-1",
                            "future_field": "ignored",
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        loaded = store.load("oc_chat")

        assert loaded is not None
        self.assertNotIn("future_field", loaded)
        self.assertEqual(loaded["session_id"], "session-1")


if __name__ == "__main__":
    unittest.main()
