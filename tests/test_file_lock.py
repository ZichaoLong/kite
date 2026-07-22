import os
import tempfile
import unittest
from pathlib import Path

from kite.file_lock import FileLockBusyError, acquire_file_lock, release_file_lock


@unittest.skipIf(os.name == "nt", "flock contention semantics are POSIX-specific")
class FileLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock_path = Path(self._tmp.name) / "test.lock"

    def _open(self):
        return self.lock_path.open("a+")

    def test_acquire_and_release_roundtrip(self) -> None:
        with self._open() as handle:
            acquire_file_lock(handle, blocking=False)
            release_file_lock(handle)
            # Re-acquiring after release must succeed.
            acquire_file_lock(handle, blocking=False)
            release_file_lock(handle)

    def test_acquire_initializes_empty_lock_file(self) -> None:
        with self._open() as handle:
            acquire_file_lock(handle, blocking=False)
            release_file_lock(handle)
        self.assertGreater(self.lock_path.stat().st_size, 0)

    def test_non_blocking_acquire_while_held_raises_busy_error(self) -> None:
        with self._open() as holder:
            acquire_file_lock(holder, blocking=False)
            with self._open() as contender:
                with self.assertRaises(FileLockBusyError):
                    acquire_file_lock(contender, blocking=False)
            release_file_lock(holder)

    def test_contender_can_acquire_after_release(self) -> None:
        with self._open() as holder:
            acquire_file_lock(holder, blocking=False)
            release_file_lock(holder)
            with self._open() as contender:
                acquire_file_lock(contender, blocking=False)
                release_file_lock(contender)


if __name__ == "__main__":
    unittest.main()
