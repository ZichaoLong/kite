"""install.py helper tests (the installer itself is never executed here)."""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install


class WriteWrappersTests(unittest.TestCase):
    def test_writes_executable_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            venv_dir = pathlib.Path(tmp) / "venv"
            bin_dir = pathlib.Path(tmp) / "bin"

            written = install._write_wrappers(venv_dir, bin_dir)

            self.assertEqual([path.name for path in written], ["kitectl"])
            target = bin_dir / "kitectl"
            self.assertTrue(os.access(target, os.X_OK))
            content = target.read_text(encoding="utf-8")
            self.assertIn(str(venv_dir), content)
            self.assertIn('"$@"', content)


class RegisterServiceTests(unittest.TestCase):
    def test_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            venv_dir = pathlib.Path(tmp)
            with patch.object(install.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "", "")
                self.assertTrue(install._register_service(venv_dir))
            argv = run.call_args[0][0]
            self.assertIn("kitectl", argv[0])
            self.assertEqual(argv[1:], ["service", "install"])

    def test_failure_warns_and_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            venv_dir = pathlib.Path(tmp)
            with patch.object(install.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 3, "", "boom")
                self.assertFalse(install._register_service(venv_dir))


if __name__ == "__main__":
    unittest.main()
