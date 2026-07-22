import os
import unittest
from pathlib import Path
from unittest.mock import patch

from kite.platform_paths import default_config_root, default_data_root, default_working_dir


class PlatformPathsTests(unittest.TestCase):
    def test_default_roots_use_linux_machine_paths_even_inside_repo_checkout(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("kite.platform_paths.pathlib.Path.home", return_value=Path("/home/tester")):
                with patch("kite.platform_paths.is_windows", return_value=False):
                    with patch("kite.platform_paths.is_macos", return_value=False):
                        self.assertEqual(default_config_root(), Path("/home/tester/.config/kite"))
                        self.assertEqual(default_data_root(), Path("/home/tester/.local/share/kite"))

    def test_default_roots_use_macos_machine_paths(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("kite.platform_paths.pathlib.Path.home", return_value=Path("/Users/tester")):
                with patch("kite.platform_paths.is_windows", return_value=False):
                    with patch("kite.platform_paths.is_macos", return_value=True):
                        self.assertEqual(
                            default_config_root(),
                            Path("/Users/tester/Library/Application Support/kite/config"),
                        )
                        self.assertEqual(
                            default_data_root(),
                            Path("/Users/tester/Library/Application Support/kite/data"),
                        )

    def test_default_roots_use_windows_machine_paths(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APPDATA": r"C:\Users\tester\AppData\Roaming",
                "LOCALAPPDATA": r"C:\Users\tester\AppData\Local",
            },
            clear=True,
        ):
            with patch("kite.platform_paths.pathlib.Path.home", return_value=Path(r"C:\Users\tester")):
                with patch("kite.platform_paths.is_windows", return_value=True):
                    with patch("kite.platform_paths.is_macos", return_value=False):
                        self.assertEqual(
                            default_config_root(),
                            Path(r"C:\Users\tester\AppData\Roaming/kite/config"),
                        )
                        self.assertEqual(
                            default_data_root(),
                            Path(r"C:\Users\tester\AppData\Local/kite/data"),
                        )

    def test_default_roots_honor_explicit_env_overrides(self) -> None:
        with patch.dict(
            os.environ,
            {
                "KITE_CONFIG_ROOT": "/tmp/custom-config-root",
                "KITE_DATA_ROOT": "/tmp/custom-data-root",
            },
            clear=True,
        ):
            self.assertEqual(default_config_root(), Path("/tmp/custom-config-root"))
            self.assertEqual(default_data_root(), Path("/tmp/custom-data-root"))

    def test_default_working_dir_uses_home_directory(self) -> None:
        with patch("kite.platform_paths.pathlib.Path.home", return_value=Path("/home/tester")):
            self.assertEqual(default_working_dir(), Path("/home/tester"))


if __name__ == "__main__":
    unittest.main()
