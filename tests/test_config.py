import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kite import config as config_module
from kite.config import (
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    DEFAULT_GROUP_HISTORY_FETCH_LIMIT,
    DEFAULT_GROUP_HISTORY_FETCH_LOOKBACK_SECONDS,
    admin_open_ids,
    approval_timeout_seconds,
    config_dir,
    default_working_dir,
    ensure_init_token,
    group_history_fetch_limit,
    group_history_fetch_lookback_seconds,
    init_token_path,
    kap_settings,
    load_config,
    load_config_file,
    load_system_config_raw,
    save_config_file,
    save_system_config,
    save_system_config_updates,
    system_config_path,
)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_root = Path(self._tmp.name) / "config"
        self._env_patch = patch.dict(os.environ, {"KITE_CONFIG_DIR": str(self.config_root)})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_config_dir_honors_env_override(self) -> None:
        self.assertEqual(config_dir(), self.config_root)
        self.assertEqual(system_config_path(), self.config_root / "system.yaml")
        self.assertEqual(init_token_path(), self.config_root / "init.token")

    def test_config_dir_falls_back_to_platform_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "kite.config.default_config_root",
                return_value=Path("/tmp/kite-default-config"),
            ):
                self.assertEqual(config_dir(), Path("/tmp/kite-default-config"))

    def test_load_config_raises_when_missing(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_config()

    def test_load_config_rejects_missing_app_credentials(self) -> None:
        save_system_config({"app_id": "", "app_secret": "secret"})
        with self.assertRaisesRegex(ValueError, "app_id"):
            load_config()
        save_system_config({"app_id": "cli_xxx"})
        with self.assertRaisesRegex(ValueError, "app_id"):
            load_config()

    def test_load_config_round_trips_system_yaml(self) -> None:
        payload = {
            "app_id": "cli_xxx",
            "app_secret": "secret",
            "admin_open_ids": ["ou_admin"],
        }
        save_system_config(payload)
        self.assertEqual(load_config(), payload)
        self.assertEqual(load_system_config_raw(), payload)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are Unix-specific")
    def test_save_system_config_writes_private_file_atomically(self) -> None:
        path = save_system_config({"app_id": "cli_xxx", "app_secret": "secret"})
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertFalse(path.with_name(f"{path.name}.tmp").exists())

    def test_save_system_config_updates_merges(self) -> None:
        save_system_config({"app_id": "cli_xxx", "app_secret": "secret"})
        merged, _ = save_system_config_updates({"approval_timeout_seconds": 60})
        self.assertEqual(merged["app_id"], "cli_xxx")
        self.assertEqual(merged["approval_timeout_seconds"], 60)
        self.assertEqual(load_system_config_raw()["approval_timeout_seconds"], 60)

    def test_ensure_init_token_is_stable_and_private(self) -> None:
        token = ensure_init_token()
        self.assertTrue(token)
        self.assertEqual(ensure_init_token(), token)
        self.assertEqual(init_token_path().read_text(encoding="utf-8").strip(), token)
        if os.name != "nt":
            mode = stat.S_IMODE(init_token_path().stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_ensure_init_token_regenerates_empty_file(self) -> None:
        init_token_path().parent.mkdir(parents=True, exist_ok=True)
        init_token_path().write_text("\n", encoding="utf-8")
        token = ensure_init_token()
        self.assertTrue(token)
        self.assertEqual(init_token_path().read_text(encoding="utf-8").strip(), token)

    def test_load_config_file_missing_returns_empty_dict(self) -> None:
        self.assertEqual(load_config_file("kimi"), {})

    def test_save_and_load_config_file_round_trip(self) -> None:
        path = save_config_file("kimi", {"model": "kimi-for-coding"})
        self.assertEqual(path, self.config_root / "kimi.yaml")
        self.assertEqual(load_config_file("kimi"), {"model": "kimi-for-coding"})
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        (other / "kimi.yaml").write_text("model: other-model\n", encoding="utf-8")
        self.assertEqual(load_config_file("kimi", directory=other), {"model": "other-model"})

    def test_admin_open_ids_defaults_to_empty_set(self) -> None:
        self.assertEqual(admin_open_ids({}), set())

    def test_admin_open_ids_normalizes_entries(self) -> None:
        config = {"admin_open_ids": [" ou_admin ", "", "ou_second"]}
        self.assertEqual(admin_open_ids(config), {"ou_admin", "ou_second"})

    def test_admin_open_ids_rejects_non_list(self) -> None:
        with self.assertRaisesRegex(ValueError, "admin_open_ids must be a list"):
            admin_open_ids({"admin_open_ids": "ou_admin"})

    def test_default_working_dir_explicit_value_is_expanded(self) -> None:
        self.assertEqual(default_working_dir({"default_working_dir": " ~/work "}), str(Path("~/work").expanduser()))

    def test_default_working_dir_falls_back_to_platform_default(self) -> None:
        with patch.object(
            config_module,
            "_platform_default_working_dir",
            return_value=Path("/home/tester"),
        ):
            self.assertEqual(default_working_dir({}), "/home/tester")

    def test_approval_timeout_seconds_defaults_to_300(self) -> None:
        self.assertEqual(DEFAULT_APPROVAL_TIMEOUT_SECONDS, 300)
        self.assertEqual(approval_timeout_seconds({}), 300)

    def test_approval_timeout_seconds_accepts_positive_integer(self) -> None:
        self.assertEqual(approval_timeout_seconds({"approval_timeout_seconds": 60}), 60)

    def test_approval_timeout_seconds_rejects_invalid_values(self) -> None:
        for bad in (0, -5, "abc", True, None):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "approval_timeout_seconds"):
                    approval_timeout_seconds({"approval_timeout_seconds": bad})

    def test_group_history_fetch_settings_default(self) -> None:
        self.assertEqual(group_history_fetch_limit({}), DEFAULT_GROUP_HISTORY_FETCH_LIMIT)
        self.assertEqual(
            group_history_fetch_lookback_seconds({}),
            DEFAULT_GROUP_HISTORY_FETCH_LOOKBACK_SECONDS,
        )

    def test_group_history_fetch_settings_accept_overrides(self) -> None:
        config = {
            "group_history_fetch_limit": 20,
            "group_history_fetch_lookback_seconds": 3600,
        }
        self.assertEqual(group_history_fetch_limit(config), 20)
        self.assertEqual(group_history_fetch_lookback_seconds(config), 3600)

    def test_group_history_fetch_settings_accept_zero_as_disable(self) -> None:
        config = {
            "group_history_fetch_limit": 0,
            "group_history_fetch_lookback_seconds": 0,
        }
        self.assertEqual(group_history_fetch_limit(config), 0)
        self.assertEqual(group_history_fetch_lookback_seconds(config), 0)

    def test_group_history_fetch_settings_reject_invalid_values(self) -> None:
        for bad in (-1, "abc", True, None):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "group_history_fetch_limit"):
                    group_history_fetch_limit({"group_history_fetch_limit": bad})
                with self.assertRaisesRegex(ValueError, "group_history_fetch_lookback_seconds"):
                    group_history_fetch_lookback_seconds(
                        {"group_history_fetch_lookback_seconds": bad}
                    )

    def test_kap_settings_defaults_when_section_absent(self) -> None:
        settings = kap_settings({})
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertIsNone(settings.port)
        self.assertIsNone(settings.home)
        self.assertIsNone(settings.kimi_bin)
        self.assertIsNone(settings.model)
        self.assertIsNone(settings.stale_seconds)

    def test_kap_settings_parses_full_section(self) -> None:
        settings = kap_settings(
            {
                "kap": {
                    "host": "127.0.0.1",
                    "port": 59000,
                    "home": "~/kap-home",
                    "kimi_bin": "/opt/kimi/bin/kimi",
                    "model": "kimi-code/k3",
                    "stale_seconds": 30,
                    "reconnect_delay_seconds": 1.5,
                    "backoff_base_seconds": 2,
                    "backoff_cap_seconds": 60,
                }
            }
        )
        self.assertEqual(settings.port, 59000)
        self.assertEqual(settings.home, "~/kap-home")
        self.assertEqual(settings.kimi_bin, "/opt/kimi/bin/kimi")
        self.assertEqual(settings.model, "kimi-code/k3")
        self.assertEqual(settings.stale_seconds, 30.0)
        self.assertEqual(settings.reconnect_delay_seconds, 1.5)
        self.assertEqual(settings.backoff_base_seconds, 2.0)
        self.assertEqual(settings.backoff_cap_seconds, 60.0)

    def test_kap_settings_rejects_invalid_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "kap.port"):
            kap_settings({"kap": {"port": 0}})
        with self.assertRaisesRegex(ValueError, "kap.port"):
            kap_settings({"kap": {"port": "58627"}})
        with self.assertRaisesRegex(ValueError, "kap.host"):
            kap_settings({"kap": {"host": "  "}})
        with self.assertRaisesRegex(ValueError, "kap.model"):
            kap_settings({"kap": {"model": "  "}})
        with self.assertRaisesRegex(ValueError, "kap.model"):
            kap_settings({"kap": {"model": 7}})
        with self.assertRaisesRegex(ValueError, "kap.stale_seconds"):
            kap_settings({"kap": {"stale_seconds": -1}})
        with self.assertRaisesRegex(ValueError, "kap must be a mapping"):
            kap_settings({"kap": "nope"})

    def test_kap_settings_rejects_non_loopback_host(self) -> None:
        # Audit L29: the managed child is never passed --host and binds
        # loopback only, so a non-loopback kap.host could never connect —
        # reject it at config validation with a clear error.
        for bad in ("0.0.0.0", "192.168.1.10", "example.internal", "::"):
            with self.subTest(host=bad):
                with self.assertRaisesRegex(ValueError, "kap.host must be a loopback"):
                    kap_settings({"kap": {"host": bad}})

    def test_kap_settings_accepts_loopback_host_spellings(self) -> None:
        for good in ("127.0.0.1", "::1", "localhost", " LOCALHOST "):
            with self.subTest(host=good):
                settings = kap_settings({"kap": {"host": good}})
                self.assertTrue(settings.host)


if __name__ == "__main__":
    unittest.main()
