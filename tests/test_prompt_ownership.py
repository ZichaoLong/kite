import threading
import unittest

from kite.prompt_ownership import (
    CERTAINTY_BEST_EFFORT,
    CERTAINTY_CERTAIN,
    PromptOwnership,
    PromptOwnershipEntry,
)


def _entry(prompt_id: str, chat_id: str, certainty: str = CERTAINTY_BEST_EFFORT) -> PromptOwnershipEntry:
    return PromptOwnershipEntry(prompt_id=prompt_id, chat_id=chat_id, certainty=certainty)


class PromptOwnershipTests(unittest.TestCase):
    def test_record_and_owner_lookup(self) -> None:
        ownership = PromptOwnership()
        ownership.record("p-1", "oc_chat")
        self.assertEqual(ownership.owner_of("p-1"), "oc_chat")
        self.assertEqual(ownership.certainty_of("p-1"), CERTAINTY_CERTAIN)
        self.assertEqual(len(ownership), 1)

    def test_record_best_effort_marks_certainty(self) -> None:
        ownership = PromptOwnership()
        ownership.record_best_effort("p-1", "oc_chat")
        self.assertEqual(ownership.certainty_of("p-1"), CERTAINTY_BEST_EFFORT)

    def test_unknown_prompt_returns_none(self) -> None:
        ownership = PromptOwnership()
        self.assertIsNone(ownership.owner_of("p-missing"))
        self.assertIsNone(ownership.certainty_of("p-missing"))
        self.assertIsNone(ownership.entry_of("p-missing"))
        self.assertIsNone(ownership.owner_of(""))

    def test_record_upgrades_best_effort_to_certain(self) -> None:
        ownership = PromptOwnership()
        ownership.record_best_effort("p-1", "oc_chat")
        ownership.record("p-1", "oc_chat")
        self.assertEqual(ownership.certainty_of("p-1"), CERTAINTY_CERTAIN)

    def test_forget_removes_entry(self) -> None:
        ownership = PromptOwnership()
        ownership.record("p-1", "oc_chat")
        ownership.forget("p-1")
        self.assertIsNone(ownership.owner_of("p-1"))
        ownership.forget("p-1")  # absent: no-op

    def test_rebuild_replaces_everything(self) -> None:
        ownership = PromptOwnership()
        ownership.record("p-old", "oc_old")
        ownership.rebuild([_entry("p-1", "oc_a"), _entry("p-2", "oc_b")])
        self.assertIsNone(ownership.owner_of("p-old"))
        self.assertEqual(ownership.owner_of("p-1"), "oc_a")
        self.assertEqual(ownership.owner_of("p-2"), "oc_b")
        self.assertEqual(ownership.certainty_of("p-1"), CERTAINTY_BEST_EFFORT)
        self.assertEqual(len(ownership), 2)

    def test_rebuild_with_empty_list_clears(self) -> None:
        ownership = PromptOwnership()
        ownership.record("p-1", "oc_chat")
        ownership.rebuild([])
        self.assertEqual(len(ownership), 0)

    def test_rebuild_validates_entries(self) -> None:
        ownership = PromptOwnership()
        with self.assertRaises(ValueError):
            ownership.rebuild([_entry("", "oc_chat")])
        with self.assertRaises(ValueError):
            ownership.rebuild([_entry("p-1", "oc_chat", "wild guess")])

    def test_record_rejects_empty_ids(self) -> None:
        ownership = PromptOwnership()
        with self.assertRaises(ValueError):
            ownership.record("", "oc_chat")
        with self.assertRaises(ValueError):
            ownership.record("p-1", "  ")

    def test_snapshot_returns_all_entries(self) -> None:
        ownership = PromptOwnership()
        ownership.record("p-1", "oc_a")
        ownership.record_best_effort("p-2", "oc_b")
        snapshot = ownership.snapshot()
        self.assertEqual(len(snapshot), 2)
        by_id = {entry.prompt_id: entry for entry in snapshot}
        self.assertEqual(by_id["p-1"].certainty, CERTAINTY_CERTAIN)
        self.assertEqual(by_id["p-2"].chat_id, "oc_b")

    def test_entry_of_returns_full_entry(self) -> None:
        ownership = PromptOwnership()
        ownership.record("p-1", "oc_a")
        entry = ownership.entry_of("p-1")
        assert entry is not None
        self.assertEqual(entry.prompt_id, "p-1")
        self.assertEqual(entry.chat_id, "oc_a")
        self.assertEqual(entry.certainty, CERTAINTY_CERTAIN)

    def test_concurrent_records_do_not_corrupt_the_map(self) -> None:
        ownership = PromptOwnership()

        def record_range(start: int) -> None:
            for index in range(start, start + 50):
                ownership.record(f"p-{index}", "oc_chat")

        threads = [threading.Thread(target=record_range, args=(n,)) for n in (0, 50, 100)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(ownership), 150)
        self.assertEqual(ownership.owner_of("p-42"), "oc_chat")


if __name__ == "__main__":
    unittest.main()
