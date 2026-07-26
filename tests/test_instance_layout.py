"""Multi-instance layout tests (docs/decisions/multi-instance.md §1/§2)."""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from kite import instance_layout
from kite.adapters.kap_server import resolve_kap_home
from kite.platform_paths import default_config_root, default_data_root


def _home_env(home: pathlib.Path) -> dict[str, str]:
    """Env patch relocating the platform roots without explicit overrides.

    KITE_CONFIG_DIR / KITE_DATA_ROOT are explicit-directory overrides (they
    win over the instance layout), so tests relocate the roots via HOME and
    clear the override vars instead.
    """
    return {
        "HOME": str(home),
        "KITE_CONFIG_ROOT": "",
        "KITE_DATA_ROOT": "",
        "KITE_CONFIG_DIR": "",
        "KIMI_CODE_HOME": "",
    }


class ValidateInstanceNameTests(unittest.TestCase):
    def test_accepts_documented_shape(self) -> None:
        for name in ("acme", "a", "0", "acme-corp", "acme_corp", "acme.corp", "a1-b_c.d"):
            self.assertEqual(instance_layout.validate_instance_name(name), name)

    def test_strips_surrounding_whitespace(self) -> None:
        self.assertEqual(instance_layout.validate_instance_name("  acme  "), "acme")

    def test_rejects_fail_closed(self) -> None:
        for name in ("", "   ", "..", "default", "instances", "Default", "INSTANCES"):
            with self.assertRaises(ValueError, msg=f"name {name!r} must be rejected"):
                instance_layout.validate_instance_name(name)

    def test_rejects_bad_characters_and_shapes(self) -> None:
        for name in ("UPPER", "a b", "/etc", ".hidden", "a/b", "-lead", "_lead", "a..b/"):
            with self.assertRaises(ValueError, msg=f"name {name!r} must be rejected"):
                instance_layout.validate_instance_name(name)


class ResolveLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = pathlib.Path(self._tmp.name) / "home"
        self._env = patch.dict(os.environ, _home_env(self.home))
        self._env.start()
        self.addCleanup(self._env.stop)
        # Roots are derived from the patched HOME (portable across platforms).
        self.config_root = default_config_root()
        self.data_root = default_data_root()

    def test_default_instance_paths_are_byte_identical_to_today(self) -> None:
        for name in (None, "", "   "):
            paths = instance_layout.resolve(name)
            self.assertIsNone(paths.instance_name)
            self.assertEqual(paths.config_dir, default_config_root())
            self.assertEqual(paths.data_dir, default_data_root())
            self.assertEqual(paths.config_dir, self.config_root)
            self.assertEqual(paths.data_dir, self.data_root)

    def test_named_instance_lives_under_instances_segment(self) -> None:
        paths = instance_layout.resolve("acme")
        self.assertEqual(paths.instance_name, "acme")
        self.assertEqual(paths.config_dir, self.config_root / "instances" / "acme")
        self.assertEqual(paths.data_dir, self.data_root / "instances" / "acme")
        self.assertEqual(
            paths.kap_home, self.data_root / "instances" / "acme" / "kap-home"
        )

    def test_named_instance_validates_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            instance_layout.resolve("..")
        with self.assertRaises(ValueError):
            instance_layout.resolve("default")

    def test_explicit_config_dir_wins_over_instance_layout(self) -> None:
        override = self.home / "elsewhere"
        with patch.dict(os.environ, {"KITE_CONFIG_DIR": str(override)}):
            paths = instance_layout.resolve("acme")
        self.assertEqual(paths.config_dir, override)
        # The other axis still comes from the layout.
        self.assertEqual(paths.data_dir, self.data_root / "instances" / "acme")
        self.assertEqual(paths.kap_home, paths.data_dir / "kap-home")

    def test_explicit_data_root_wins_over_instance_layout(self) -> None:
        override = self.home / "elsewhere-data"
        with patch.dict(os.environ, {"KITE_DATA_ROOT": str(override)}):
            paths = instance_layout.resolve("acme")
        self.assertEqual(paths.data_dir, override)
        self.assertEqual(paths.kap_home, override / "kap-home")
        self.assertEqual(paths.config_dir, self.config_root / "instances" / "acme")

    def test_default_instance_honors_explicit_dirs(self) -> None:
        with patch.dict(
            os.environ,
            {"KITE_CONFIG_DIR": str(self.home / "c"), "KITE_DATA_ROOT": str(self.home / "d")},
        ):
            paths = instance_layout.resolve(None)
        self.assertEqual(paths.config_dir, self.home / "c")
        self.assertEqual(paths.data_dir, self.home / "d")


class EffectiveKapHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = pathlib.Path(self._tmp.name) / "home"
        self._env = patch.dict(os.environ, _home_env(self.home))
        self._env.start()
        self.addCleanup(self._env.stop)
        self.data_root = default_data_root()

    def test_named_instance_gets_isolated_kap_home(self) -> None:
        home = instance_layout.resolve_effective_kap_home(None, "acme")
        self.assertEqual(home, self.data_root / "instances" / "acme" / "kap-home")

    def test_default_instance_keeps_shared_kimi_home(self) -> None:
        home = instance_layout.resolve_effective_kap_home(None, None)
        self.assertEqual(home, self.home / ".kimi-code")
        self.assertEqual(home, resolve_kap_home(None))

    def test_default_instance_honors_kimi_code_home_env(self) -> None:
        custom = self.home / "custom-kimi"
        with patch.dict(os.environ, {"KIMI_CODE_HOME": str(custom)}):
            self.assertEqual(instance_layout.resolve_effective_kap_home(None, None), custom)
            # A named instance ignores it: isolation by construction.
            named = instance_layout.resolve_effective_kap_home(None, "acme")
            self.assertEqual(named, self.data_root / "instances" / "acme" / "kap-home")

    def test_explicit_kap_home_config_always_wins(self) -> None:
        configured = self.home / "tenant-kimi"
        self.assertEqual(
            instance_layout.resolve_effective_kap_home(str(configured), "acme"), configured
        )
        self.assertEqual(
            instance_layout.resolve_effective_kap_home(str(configured), None), configured
        )


if __name__ == "__main__":
    unittest.main()
