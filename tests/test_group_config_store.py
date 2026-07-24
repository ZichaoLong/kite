"""GroupConfigStore contract tests (group-chat contract §4.3).

Reads are fail-closed: a corrupt file or record reads as non-activated,
never as open. Writes are strict and atomic (BindingStore conventions).
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from kite.stores.group_config_store import (
    GROUP_CONFIG_STORE_SCHEMA_VERSION,
    GROUP_MODE_MENTION_ONLY,
    GroupConfigStore,
)

CHAT_ID = "oc_group"
OTHER_CHAT_ID = "oc_group_2"


def _activated_config(**overrides) -> dict:
    config = {
        "activated": True,
        "activated_by": "ou_admin",
        "activated_at": 1720000000.5,
        "mode": GROUP_MODE_MENTION_ONLY,
    }
    config.update(overrides)
    return config


class GroupConfigStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = pathlib.Path(self._tmp.name)
        self.store = GroupConfigStore(self.data_dir)

    def _write_raw(self, payload: object) -> None:
        (self.data_dir / "group_configs.json").write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )


class LoadSaveTests(GroupConfigStoreTestCase):
    def test_missing_file_reads_as_non_activated(self) -> None:
        self.assertIsNone(self.store.load(CHAT_ID))
        self.assertFalse(self.store.is_activated(CHAT_ID))

    def test_save_load_roundtrip(self) -> None:
        saved = self.store.save(CHAT_ID, _activated_config())
        self.assertEqual(saved["activated_by"], "ou_admin")
        loaded = self.store.load(CHAT_ID)
        assert loaded is not None
        self.assertEqual(loaded, _activated_config())
        self.assertTrue(self.store.is_activated(CHAT_ID))

    def test_persists_across_instances(self) -> None:
        self.store.save(CHAT_ID, _activated_config())
        reloaded = GroupConfigStore(self.data_dir)
        self.assertTrue(reloaded.is_activated(CHAT_ID))
        loaded = reloaded.load(CHAT_ID)
        assert loaded is not None
        self.assertEqual(loaded["activated_at"], 1720000000.5)

    def test_load_does_not_leak_mutable_state(self) -> None:
        self.store.save(CHAT_ID, _activated_config())
        loaded = self.store.load(CHAT_ID)
        assert loaded is not None
        loaded["activated"] = False
        self.assertTrue(self.store.is_activated(CHAT_ID))

    def test_activate_sets_fields_and_mode(self) -> None:
        config = self.store.activate(CHAT_ID, activated_by="ou_admin", activated_at=123.0)
        self.assertEqual(config["activated_by"], "ou_admin")
        self.assertEqual(config["activated_at"], 123.0)
        self.assertEqual(config["mode"], GROUP_MODE_MENTION_ONLY)
        self.assertTrue(self.store.is_activated(CHAT_ID))

    def test_activate_defaults_timestamp_to_now(self) -> None:
        config = self.store.activate(CHAT_ID, activated_by="ou_admin")
        self.assertGreater(config["activated_at"], 0.0)

    def test_deactivate_clears_actor_but_keeps_record(self) -> None:
        self.store.activate(CHAT_ID, activated_by="ou_admin", activated_at=123.0)
        config = self.store.deactivate(CHAT_ID)
        self.assertFalse(config["activated"])
        self.assertEqual(config["activated_by"], "")
        self.assertEqual(config["activated_at"], 0.0)
        self.assertEqual(config["mode"], GROUP_MODE_MENTION_ONLY)
        self.assertFalse(self.store.is_activated(CHAT_ID))
        # The record itself persists (mode preference kept).
        self.assertIsNotNone(self.store.load(CHAT_ID))

    def test_empty_chat_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.load("  ")
        with self.assertRaises(ValueError):
            self.store.save("", _activated_config())


class StrictWriteValidationTests(GroupConfigStoreTestCase):
    def _assert_save_rejected(self, config: dict) -> None:
        with self.assertRaises(ValueError):
            self.store.save(CHAT_ID, config)

    def test_invalid_mode_is_rejected(self) -> None:
        self._assert_save_rejected(_activated_config(mode="all"))
        self._assert_save_rejected(_activated_config(mode="assistant"))

    def test_activated_must_be_bool(self) -> None:
        self._assert_save_rejected(_activated_config(activated="yes"))

    def test_activated_by_required_when_activated(self) -> None:
        self._assert_save_rejected(_activated_config(activated_by=""))

    def test_activated_at_must_be_a_number(self) -> None:
        self._assert_save_rejected(_activated_config(activated_at="now"))
        self._assert_save_rejected(_activated_config(activated_at=True))

    def test_activate_requires_activated_by(self) -> None:
        with self.assertRaises(ValueError):
            self.store.activate(CHAT_ID, activated_by=" ")


class FailClosedReadTests(GroupConfigStoreTestCase):
    """Contract §4.3: corruption reads as non-activated, never as open."""

    def test_unparseable_file_reads_as_non_activated(self) -> None:
        self._write_raw("{ not json")
        self.assertIsNone(self.store.load(CHAT_ID))
        self.assertFalse(self.store.is_activated(CHAT_ID))

    def test_non_object_root_reads_as_non_activated(self) -> None:
        self._write_raw([1, 2, 3])
        self.assertFalse(self.store.is_activated(CHAT_ID))

    def test_unsupported_schema_version_reads_as_non_activated(self) -> None:
        self._write_raw(
            {
                "schema_version": GROUP_CONFIG_STORE_SCHEMA_VERSION + 1,
                "groups": {CHAT_ID: _activated_config()},
            }
        )
        self.assertFalse(self.store.is_activated(CHAT_ID))

    def test_non_object_groups_reads_as_non_activated(self) -> None:
        self._write_raw({"schema_version": GROUP_CONFIG_STORE_SCHEMA_VERSION, "groups": []})
        self.assertFalse(self.store.is_activated(CHAT_ID))

    def test_corrupt_record_skips_only_that_chat(self) -> None:
        self._write_raw(
            {
                "schema_version": GROUP_CONFIG_STORE_SCHEMA_VERSION,
                "groups": {
                    CHAT_ID: {"activated": True, "activated_by": "", "mode": "mention_only"},
                    OTHER_CHAT_ID: _activated_config(),
                },
            }
        )
        # The corrupt record fails closed...
        self.assertFalse(self.store.is_activated(CHAT_ID))
        self.assertIsNone(self.store.load(CHAT_ID))
        # ...without taking the healthy record down with it.
        self.assertTrue(self.store.is_activated(OTHER_CHAT_ID))

    def test_record_with_invalid_mode_reads_as_non_activated(self) -> None:
        self._write_raw(
            {
                "schema_version": GROUP_CONFIG_STORE_SCHEMA_VERSION,
                "groups": {CHAT_ID: _activated_config(mode="all")},
            }
        )
        self.assertFalse(self.store.is_activated(CHAT_ID))

    def test_save_after_corruption_recovers_the_store(self) -> None:
        self._write_raw("{ not json")
        self.store.save(CHAT_ID, _activated_config())
        reloaded = GroupConfigStore(self.data_dir)
        self.assertTrue(reloaded.is_activated(CHAT_ID))


if __name__ == "__main__":
    unittest.main()
