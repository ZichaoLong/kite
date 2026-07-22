import tempfile
import unittest

from kite.runtime_status import RuntimeStatusWriter, read_runtime_status


class RuntimeStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_update_merges_sections_and_round_trips(self) -> None:
        writer = RuntimeStatusWriter(self._tmp.name)
        writer.update(kap={"pid": 123, "port": 58627})
        writer.update(ws={"connected_at": 1000.0})
        writer.update(ws={"last_resync_at": 2000.0})

        status = read_runtime_status(self._tmp.name)
        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["schema_version"], 1)
        self.assertEqual(status["kap"], {"pid": 123, "port": 58627})
        self.assertEqual(status["ws"], {"connected_at": 1000.0, "last_resync_at": 2000.0})
        self.assertIn("updated_at", status)

    def test_read_missing_or_invalid_returns_none(self) -> None:
        self.assertIsNone(read_runtime_status(self._tmp.name))
        writer = RuntimeStatusWriter(self._tmp.name)
        writer.update(kap={"pid": 1})
        writer.path.write_text("not json", encoding="utf-8")
        self.assertIsNone(read_runtime_status(self._tmp.name))

    def test_clear_removes_the_file(self) -> None:
        writer = RuntimeStatusWriter(self._tmp.name)
        writer.update(kap={"pid": 1})
        writer.clear()
        self.assertIsNone(read_runtime_status(self._tmp.name))


if __name__ == "__main__":
    unittest.main()
