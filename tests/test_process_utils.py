import os
import unittest
from unittest.mock import patch

from kite.process_utils import process_exists


@unittest.skipIf(os.name == "nt", "POSIX process probing is Unix-specific")
class ProcessUtilsTests(unittest.TestCase):
    def test_non_positive_pid_does_not_exist(self) -> None:
        self.assertFalse(process_exists(0))
        self.assertFalse(process_exists(-1))
        self.assertFalse(process_exists(None))

    def test_current_process_exists(self) -> None:
        self.assertTrue(process_exists(os.getpid()))

    def test_missing_process_does_not_exist(self) -> None:
        with patch("kite.process_utils.os.kill", side_effect=ProcessLookupError):
            self.assertFalse(process_exists(1234))

    def test_permission_error_means_process_exists(self) -> None:
        with patch("kite.process_utils.os.kill", side_effect=PermissionError):
            self.assertTrue(process_exists(1234))

    def test_linux_zombie_is_treated_as_not_running(self) -> None:
        with patch("kite.process_utils.os.kill", return_value=None):
            with patch("kite.process_utils._linux_process_state", return_value="Z"):
                self.assertFalse(process_exists(1234))

    def test_linux_sleeping_process_exists(self) -> None:
        with patch("kite.process_utils.os.kill", return_value=None):
            with patch("kite.process_utils._linux_process_state", return_value="S"):
                self.assertTrue(process_exists(1234))


if __name__ == "__main__":
    unittest.main()
