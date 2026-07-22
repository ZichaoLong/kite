import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import kite.file_permissions as file_permissions
from kite.file_permissions import ensure_private_file_permissions


class FilePermissionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "secret.env"
        self.target.write_text("KEY=value\n", encoding="utf-8")
        os.chmod(self.target, 0o644)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are Unix-specific")
    def test_unix_tightens_file_to_0600(self) -> None:
        with patch("kite.file_permissions.is_windows", return_value=False):
            ensure_private_file_permissions(self.target)
        mode = stat.S_IMODE(self.target.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_windows_falls_back_with_one_time_warning(self) -> None:
        with patch("kite.file_permissions.is_windows", return_value=True):
            with patch.object(file_permissions, "_warned_windows_acl_fallback", False):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    ensure_private_file_permissions(self.target)
                    ensure_private_file_permissions(self.target)
        # The file mode is left untouched on the Windows fallback path.
        mode = stat.S_IMODE(self.target.stat().st_mode)
        self.assertEqual(mode, 0o644)
        self.assertEqual(stderr.getvalue().count("警告"), 1)


if __name__ == "__main__":
    unittest.main()
