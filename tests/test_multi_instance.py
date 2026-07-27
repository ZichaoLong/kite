"""End-to-end multi-instance wiring tests (docs/decisions/multi-instance.md).

Covers: kitectl --instance targeting (incl. the single-running rung and the
ambiguity error), per-instance service definitions, the kited daemon lease,
kited's named-instance kap-home isolation, install.py --instance, and
`kitectl instance create` scaffolding.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import stat
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import install
from kite import instance_layout
from kite import kitectl
from kite import kited
from kite.platform_paths import ENV_FILE_NAME
from kite.stores.binding_store import BindingStore

_MUTATED_ENV_VARS = (
    "HOME",
    "KITE_CONFIG_ROOT",
    "KITE_DATA_ROOT",
    "KITE_CONFIG_DIR",
    "KITE_INSTANCE",
    "KITE_ENV_FILE",
    "KIMI_CODE_HOME",
)


@contextlib.contextmanager
def _saved_env(keys=_MUTATED_ENV_VARS):
    """kitectl/kited main() publish the resolved instance via env; restore."""
    saved = {key: os.environ.get(key) for key in keys}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class MultiInstanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = pathlib.Path(self._tmp.name) / "home"
        # Roots are derived from the patched HOME (portable across platforms).
        with patch.dict(os.environ, self._roots_env()):
            from kite.platform_paths import default_config_root, default_data_root

            self.config_root = default_config_root()
            self.data_root = default_data_root()
        self.config_root.mkdir(parents=True)
        self.data_root.mkdir(parents=True)

    def _roots_env(self) -> dict[str, str]:
        # Relocate the roots via HOME: KITE_CONFIG_DIR / KITE_DATA_ROOT are
        # explicit-directory overrides that win over the instance layout, so
        # they must stay clear for the layout to apply.
        return {
            "HOME": str(self.home),
            "KITE_CONFIG_ROOT": "",
            "KITE_DATA_ROOT": "",
            "KITE_CONFIG_DIR": "",
            "KITE_INSTANCE": "",
            "KITE_ENV_FILE": "",
            "KIMI_CODE_HOME": "",
        }

    def _instance_dirs(self, name: str) -> tuple[pathlib.Path, pathlib.Path]:
        config_dir = self.config_root / "instances" / name
        data_dir = self.data_root / "instances" / name
        config_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        return config_dir, data_dir

    def _bind(self, data_dir: pathlib.Path, chat_id: str, session_id: str) -> None:
        BindingStore(data_dir).save(
            chat_id,
            {
                "session_id": session_id,
                "attached": True,
                "permission_mode": "auto",
                "plan_mode": False,
            },
        )

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = kitectl.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def _publish_live_control_plane(self, data_dir: pathlib.Path) -> None:
        (data_dir / "control_plane.json").write_text(
            json.dumps({"port": 43210, "pid": os.getpid(), "started_at": time.time()}),
            encoding="utf-8",
        )


class KitectlInstanceFlagTests(MultiInstanceTestCase):
    def test_binding_list_targets_named_instance(self) -> None:
        _, acme_data = self._instance_dirs("acme")
        self._bind(acme_data, "chat-acme", "s-acme")
        self._bind(self.data_root, "chat-default", "s-default")

        code, out, err = self._run_cli("--instance", "acme", "binding", "list")

        self.assertEqual(code, 0, err)
        self.assertIn("chat-acme", out)
        self.assertNotIn("chat-default", out)

    def test_env_var_targets_named_instance(self) -> None:
        _, acme_data = self._instance_dirs("acme")
        self._bind(acme_data, "chat-acme", "s-acme")
        stdout = io.StringIO()
        with _saved_env(), patch.dict(
            os.environ, {**self._roots_env(), "KITE_INSTANCE": "acme"}
        ):
            with contextlib.redirect_stdout(stdout):
                code = kitectl.main(["binding", "list"])
        self.assertEqual(code, 0)
        self.assertIn("chat-acme", stdout.getvalue())

    def test_flag_wins_over_env_var(self) -> None:
        _, acme_data = self._instance_dirs("acme")
        _, bravo_data = self._instance_dirs("bravo")
        self._bind(acme_data, "chat-acme", "s-acme")
        self._bind(bravo_data, "chat-bravo", "s-bravo")
        stdout = io.StringIO()
        with _saved_env(), patch.dict(
            os.environ, {**self._roots_env(), "KITE_INSTANCE": "bravo"}
        ):
            with contextlib.redirect_stdout(stdout):
                code = kitectl.main(["--instance", "acme", "binding", "list"])
        self.assertEqual(code, 0)
        self.assertIn("chat-acme", stdout.getvalue())

    def test_bad_instance_name_exits_2(self) -> None:
        code, _, err = self._run_cli("--instance", "..", "binding", "list")
        self.assertEqual(code, 2)
        self.assertIn("instance name", err)

    def test_single_running_instance_is_used_without_flag(self) -> None:
        _, acme_data = self._instance_dirs("acme")
        self._bind(acme_data, "chat-acme", "s-acme")
        self._publish_live_control_plane(acme_data)

        code, out, err = self._run_cli("binding", "list")

        self.assertEqual(code, 0, err)
        self.assertIn("chat-acme", out)

    def test_ambiguous_running_instances_exit_2_with_candidates(self) -> None:
        _, acme_data = self._instance_dirs("acme")
        _, bravo_data = self._instance_dirs("bravo")
        self._publish_live_control_plane(acme_data)
        self._publish_live_control_plane(bravo_data)

        code, _, err = self._run_cli("binding", "list")

        self.assertEqual(code, 2)
        self.assertIn("acme", err)
        self.assertIn("bravo", err)

    def test_service_commands_skip_single_running_rung(self) -> None:
        # A live named instance must NOT leak into a service command's
        # resolution: the definition below stays the default instance's.
        _, acme_data = self._instance_dirs("acme")
        self._publish_live_control_plane(acme_data)
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            kitectl._apply_instance_environment(
                SimpleNamespace(instance=None, config_dir=None, data_dir=None, command="service")
            )
            self.assertEqual(os.environ.get("KITE_INSTANCE", ""), "")
            self.assertNotEqual(
                os.environ.get("KITE_CONFIG_DIR", ""), str(acme_data)
            )

    def test_explicit_dirs_skip_single_running_rung(self) -> None:
        # Audit N1: an explicit --config-dir/--data-dir already names the
        # directories; rung 2 must not mix in a running instance's name.
        _, acme_data = self._instance_dirs("acme")
        self._publish_live_control_plane(acme_data)
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            kitectl._apply_instance_environment(
                SimpleNamespace(
                    instance=None,
                    config_dir=str(self.home / "explicit-cfg"),
                    data_dir=str(self.home / "explicit-data"),
                    command="binding",
                )
            )
            self.assertEqual(os.environ.get("KITE_INSTANCE", ""), "")

    def test_preset_dir_env_vars_skip_single_running_rung(self) -> None:
        # Same for pre-set KITE_CONFIG_DIR / KITE_DATA_ROOT (audit N1).
        _, acme_data = self._instance_dirs("acme")
        self._publish_live_control_plane(acme_data)
        env = {**self._roots_env(), "KITE_DATA_ROOT": str(self.home / "explicit-data")}
        with _saved_env(), patch.dict(os.environ, env):
            kitectl._apply_instance_environment(
                SimpleNamespace(instance=None, config_dir=None, data_dir=None, command="binding")
            )
            self.assertEqual(os.environ.get("KITE_INSTANCE", ""), "")

    def test_explicit_dirs_do_not_hide_ambiguity_behind_rung2(self) -> None:
        # Two live instances + explicit dirs: rung 2 skipped means NO
        # ambiguity error — the explicit dirs ARE the target.
        _, acme_data = self._instance_dirs("acme")
        _, bravo_data = self._instance_dirs("bravo")
        self._publish_live_control_plane(acme_data)
        self._publish_live_control_plane(bravo_data)
        explicit_data = self.home / "explicit-data"
        explicit_data.mkdir()
        self._bind(explicit_data, "chat-explicit", "s-explicit")

        code, out, err = self._run_cli(
            "--data-dir", str(explicit_data), "binding", "list"
        )

        self.assertEqual(code, 0, err)
        self.assertIn("chat-explicit", out)

    def test_completion_ignores_running_instance_ambiguity(self) -> None:
        # Audit N1: `completion` is instance-agnostic; the ambiguity guard
        # must not reject it.
        _, acme_data = self._instance_dirs("acme")
        _, bravo_data = self._instance_dirs("bravo")
        self._publish_live_control_plane(acme_data)
        self._publish_live_control_plane(bravo_data)

        code, out, err = self._run_cli("completion", "bash")

        self.assertEqual(code, 0, err)
        self.assertIn("_kitectl_complete", out)


class ServiceDefinitionInstanceTests(MultiInstanceTestCase):
    def _definition(self, instance: str | None):
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            kitectl._apply_instance_environment(
                SimpleNamespace(
                    instance=instance, config_dir=None, data_dir=None, command="service"
                )
            )
            return kitectl._service_definition()

    def test_default_instance_definition_is_unchanged(self) -> None:
        definition = self._definition(None)
        self.assertEqual(definition.identifier, "kite")
        self.assertEqual(definition.config_dir, self.config_root)
        self.assertEqual(definition.data_dir, self.data_root)
        self.assertNotIn("--instance", definition.daemon_command)

    def test_named_instance_definition_dirs_and_unit(self) -> None:
        self._instance_dirs("acme")  # created instance: fail-close passes (§3)
        definition = self._definition("acme")
        self.assertEqual(definition.identifier, "kite-acme")
        self.assertEqual(definition.config_dir, self.config_root / "instances" / "acme")
        self.assertEqual(definition.data_dir, self.data_root / "instances" / "acme")
        command = list(definition.daemon_command)
        self.assertEqual(command[command.index("--instance") + 1], "acme")
        self.assertEqual(
            command[command.index("--config-dir") + 1],
            str(self.config_root / "instances" / "acme"),
        )

    def test_service_identifier_shape(self) -> None:
        from kite import service_manager

        self.assertEqual(service_manager.service_identifier(None), "kite")
        self.assertEqual(service_manager.service_identifier(""), "kite")
        self.assertEqual(service_manager.service_identifier("acme"), "kite-acme")


class KitedLeaseTests(MultiInstanceTestCase):
    """The lease locks the instance DATA dir (decision §4, audit N1-MED-1)."""

    def test_second_lease_fails_naming_holder_pid(self) -> None:
        _, data_dir = self._instance_dirs("acme")
        handle = kited.acquire_instance_lease(data_dir, instance_name="acme")
        self.addCleanup(handle.close)
        with self.assertRaises(kited.InstanceLeaseError) as ctx:
            kited.acquire_instance_lease(data_dir, instance_name="acme")
        message = str(ctx.exception)
        self.assertIn(str(os.getpid()), message)
        self.assertIn("acme", message)
        self.assertIn(kited.KITED_LOCK_FILE_NAME, message)

    def test_lease_records_holder_pid(self) -> None:
        _, data_dir = self._instance_dirs("acme")
        handle = kited.acquire_instance_lease(data_dir, instance_name="acme")
        self.addCleanup(handle.close)
        recorded = (data_dir / kited.KITED_LOCK_FILE_NAME).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(recorded, str(os.getpid()))

    def test_stale_holder_lock_is_acquirable(self) -> None:
        _, data_dir = self._instance_dirs("acme")
        handle = kited.acquire_instance_lease(data_dir, instance_name="acme")
        handle.close()  # simulates holder death: the OS releases the flock
        takeover = kited.acquire_instance_lease(data_dir, instance_name="acme")
        self.addCleanup(takeover.close)
        # The new holder overwrote the recorded pid.
        self.assertEqual(kited._lock_holder_pid(data_dir / kited.KITED_LOCK_FILE_NAME), os.getpid())

    def test_two_config_dirs_sharing_one_data_dir_conflict(self) -> None:
        # Audit N1-MED-1: per-axis explicit dirs can break the config:data
        # 1:1 — two kited with DIFFERENT config dirs but the SAME data dir
        # must still conflict (every mutable shared surface is in the data
        # dir). The lease therefore lives in the data dir.
        shared_data = self.home / "shared-data"
        (self.home / "cfg-a").mkdir(parents=True)
        (self.home / "cfg-b").mkdir(parents=True)
        handle = kited.acquire_instance_lease(shared_data)
        self.addCleanup(handle.close)
        with self.assertRaises(kited.InstanceLeaseError):
            kited.acquire_instance_lease(shared_data)
        self.assertTrue((shared_data / kited.KITED_LOCK_FILE_NAME).exists())

    def test_main_exits_2_on_lease_conflict(self) -> None:
        config_dir = self.home / "cfg"
        data_dir = self.home / "data2"
        data_dir.mkdir(parents=True)
        handle = kited.acquire_instance_lease(data_dir)
        self.addCleanup(handle.close)
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            rc = kited.main(
                ["--config-dir", str(config_dir), "--data-dir", str(data_dir)]
            )
        self.assertEqual(rc, 2)


class KitedKapHomeTests(MultiInstanceTestCase):
    """main() wiring: named instance → isolated kap home; default → ~/.kimi-code."""

    def _run_main(self, argv: list[str], config_dir: pathlib.Path) -> dict:
        (config_dir).mkdir(parents=True, exist_ok=True)
        (config_dir / "system.yaml").write_text(
            "app_id: cli_x\napp_secret: sec\n", encoding="utf-8"
        )
        captured: dict = {}

        def _fake_run(**kwargs):
            captured.update(kwargs)
            return 0

        with (
            patch.object(kited.kap_server, "resolve_kimi_bin", return_value="kimi"),
            patch.object(
                kited.kap_server,
                "detect_kimi_version",
                return_value=kited.kap_server.VERIFIED_KIMI_VERSION,
            ),
            patch.object(kited, "build_outbound_runtime", return_value=None),
            patch.object(kited, "run", side_effect=_fake_run),
        ):
            rc = kited.main(argv)
        self.assertEqual(rc, 0)
        return captured

    def test_named_instance_kap_home_isolation(self) -> None:
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            captured = self._run_main(["--instance", "acme"], self.config_root / "instances" / "acme")
        expected = self.data_root / "instances" / "acme" / "kap-home"
        self.assertEqual(captured["home"], expected)
        self.assertEqual(captured["data_dir"], self.data_root / "instances" / "acme")

    def test_default_instance_keeps_shared_kimi_home(self) -> None:
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            captured = self._run_main([], self.config_root)
        self.assertEqual(captured["home"], self.home / ".kimi-code")
        self.assertEqual(captured["data_dir"], self.data_root)

    def test_explicit_kap_home_config_wins_for_named_instance(self) -> None:
        custom = self.home / "tenant-kimi"
        config_dir = self.config_root / "instances" / "acme"
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "system.yaml").write_text(
                f"app_id: cli_x\napp_secret: sec\nkap:\n  home: {custom}\n",
                encoding="utf-8",
            )
            captured: dict = {}
            with (
                patch.object(kited.kap_server, "resolve_kimi_bin", return_value="kimi"),
                patch.object(
                    kited.kap_server,
                    "detect_kimi_version",
                    return_value=kited.kap_server.VERIFIED_KIMI_VERSION,
                ),
                patch.object(kited, "build_outbound_runtime", return_value=None),
                patch.object(kited, "run", side_effect=lambda **kw: captured.update(kw) or 0),
            ):
                rc = kited.main(["--instance", "acme"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["home"], custom)

    def test_named_instance_loads_its_own_env_file(self) -> None:
        config_dir = self.config_root / "instances" / "acme"
        with _saved_env((*_MUTATED_ENV_VARS, "KITE_TEST_PROVIDER_KEY")):
            os.environ.pop("KITE_TEST_PROVIDER_KEY", None)
            with patch.dict(os.environ, self._roots_env()):
                config_dir.mkdir(parents=True, exist_ok=True)
                (config_dir / "env").write_text(
                    "KITE_TEST_PROVIDER_KEY=from-instance-env\n", encoding="utf-8"
                )
                self._run_main(["--instance", "acme"], config_dir)
                self.assertEqual(
                    os.environ.get("KITE_TEST_PROVIDER_KEY"), "from-instance-env"
                )

    def test_main_rejects_bad_instance_name(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = kited.main(["--instance", ".."])
        self.assertEqual(rc, 2)
        self.assertIn("instance name", stderr.getvalue())


class InstallInstanceTests(MultiInstanceTestCase):
    def test_install_instance_creates_directories(self) -> None:
        stdout = io.StringIO()
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            with contextlib.redirect_stdout(stdout):
                install.main(["--instance", "acme"])
            paths = instance_layout.resolve("acme")
        self.assertEqual(paths.config_dir, self.config_root / "instances" / "acme")
        self.assertTrue(paths.config_dir.is_dir())
        self.assertTrue(paths.data_dir.is_dir())
        self.assertTrue(paths.kap_home.is_dir())
        self.assertIn("acme", stdout.getvalue())

    def test_install_instance_writes_env_template(self) -> None:
        # Audit N1: --instance lays out the provider-env template next to the
        # instance's future system.yaml, same as the default install.
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            install.main(["--instance", "acme"])
            paths = instance_layout.resolve("acme")
        env_path = paths.config_dir / ENV_FILE_NAME
        self.assertTrue(env_path.is_file())
        self.assertIn("KIMI_API_KEY", env_path.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

    def test_install_instance_rejects_bad_name(self) -> None:
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            with self.assertRaises(SystemExit):
                install.main(["--instance", ".."])

    def test_install_still_rejects_unknown_args(self) -> None:
        with self.assertRaises(SystemExit):
            install.main(["bogus"])

    def test_install_instance_writes_system_yaml_from_bundled_template(self) -> None:
        # install.sh --instance delegates to the same scaffold as
        # `kitectl instance create` (FOCUS parity): system.yaml comes from
        # the installed template with private permissions.
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            install.main(["--instance", "acme"])
            paths = instance_layout.resolve("acme")
        system_yaml = paths.config_dir / "system.yaml"
        example = paths.config_dir / "system.yaml.example"
        self.assertTrue(system_yaml.is_file())
        self.assertTrue(example.is_file())
        self.assertIn('app_id: "cli_xxx"', system_yaml.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(system_yaml.stat().st_mode), 0o600)
        self.assertEqual(
            example.read_text(encoding="utf-8"), system_yaml.read_text(encoding="utf-8")
        )


class InstanceCreateTests(MultiInstanceTestCase):
    """`kitectl instance create` (FOCUS's `focusctl instance create` parity)."""

    def test_create_named_instance_scaffolds_dirs_and_templates(self) -> None:
        code, out, err = self._run_cli("instance", "create", "acme")

        self.assertEqual(code, 0, err)
        config_dir = self.config_root / "instances" / "acme"
        data_dir = self.data_root / "instances" / "acme"
        self.assertTrue(config_dir.is_dir())
        self.assertTrue(data_dir.is_dir())
        self.assertTrue((data_dir / "kap-home").is_dir())
        system_yaml = config_dir / "system.yaml"
        env_path = config_dir / ENV_FILE_NAME
        self.assertIn('app_id: "cli_xxx"', system_yaml.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(system_yaml.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
        self.assertTrue((config_dir / "system.yaml.example").is_file())
        self.assertIn("created from template", out)
        self.assertIn(f"kitectl --instance acme service install", out)

    def test_create_is_idempotent_and_never_clobbers_user_files(self) -> None:
        code, _, err = self._run_cli("instance", "create", "acme")
        self.assertEqual(code, 0, err)
        config_dir = self.config_root / "instances" / "acme"
        system_yaml = config_dir / "system.yaml"
        system_yaml.write_text('app_id: "cli_real"\napp_secret: "s"\n', encoding="utf-8")
        (config_dir / "system.yaml.example").write_text("stale\n", encoding="utf-8")
        (config_dir / ENV_FILE_NAME).write_text("KIMI_API_KEY=real\n", encoding="utf-8")

        code, out, err = self._run_cli("instance", "create", "acme")

        self.assertEqual(code, 0, err)
        # User files kept; only the *.example reference copy is refreshed.
        self.assertIn("cli_real", system_yaml.read_text(encoding="utf-8"))
        self.assertIn("app_id", (config_dir / "system.yaml.example").read_text(encoding="utf-8"))
        self.assertEqual(
            (config_dir / ENV_FILE_NAME).read_text(encoding="utf-8"), "KIMI_API_KEY=real\n"
        )
        self.assertIn("kept existing", out)

    def test_create_default_scaffolds_the_root_instance(self) -> None:
        code, out, err = self._run_cli("instance", "create", "default")

        self.assertEqual(code, 0, err)
        self.assertTrue((self.config_root / "system.yaml").is_file())
        self.assertTrue((self.config_root / "system.yaml.example").is_file())
        self.assertTrue((self.config_root / ENV_FILE_NAME).is_file())
        # The default instance's kap home is ~/.kimi-code (decision §2):
        # <data>/kap-home is never created for it (audit R5-LOW-1).
        self.assertFalse((self.data_root / "kap-home").exists())
        self.assertNotIn("kap home", out)
        # The default instance's next steps carry no --instance flag.
        self.assertIn("kitectl service install", out)
        self.assertNotIn("--instance", out)

    def test_create_rejects_a_bad_name(self) -> None:
        code, _, err = self._run_cli("instance", "create", "..")

        self.assertEqual(code, 2)
        self.assertIn("instance name", err)

    def test_create_skips_the_single_running_rung(self) -> None:
        # Two live instances would make rung-2 resolution fail ambiguous;
        # `instance create` is instance-agnostic and must not consult it.
        self._publish_live_control_plane(self.data_root)
        _, acme_data = self._instance_dirs("acme")
        self._publish_live_control_plane(acme_data)

        code, out, err = self._run_cli("instance", "create", "corp-b")

        self.assertEqual(code, 0, err)
        self.assertIn("corp-b", out)

    def _run_cli_with_env(self, extra_env: dict[str, str], *argv: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with _saved_env(), patch.dict(os.environ, {**self._roots_env(), **extra_env}):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = kitectl.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_create_ignores_kite_instance_env_and_targets_the_named_dir(self) -> None:
        # Audit R5-HIGH-1: rung-1's env publication used to send the scaffold
        # into the RESOLVED instance's directories while reporting success.
        self._run_cli("instance", "create", "main")
        main_yaml = self.config_root / "instances" / "main" / "system.yaml"
        main_yaml.write_text('app_id: "cli_main"\n', encoding="utf-8")

        code, out, err = self._run_cli_with_env(
            {"KITE_INSTANCE": "main"}, "instance", "create", "new"
        )

        self.assertEqual(code, 0, err)
        # The NEW instance is scaffolded; main is untouched.
        self.assertTrue(
            (self.config_root / "instances" / "new" / "system.yaml").is_file()
        )
        self.assertTrue((self.data_root / "instances" / "new" / "kap-home").is_dir())
        self.assertEqual(main_yaml.read_text(encoding="utf-8"), 'app_id: "cli_main"\n')
        self.assertIn("instances/new", out)
        # And the new instance is immediately usable (fail-close passes).
        code, _, err = self._run_cli("--instance", "new", "binding", "list")
        self.assertEqual(code, 0, err)

    def test_create_ignores_the_instance_flag_too(self) -> None:
        self._run_cli("instance", "create", "main")

        code, out, err = self._run_cli("--instance", "main", "instance", "create", "new")

        self.assertEqual(code, 0, err)
        self.assertTrue(
            (self.config_root / "instances" / "new" / "system.yaml").is_file()
        )

    def test_completion_is_not_blocked_by_a_bad_kite_instance(self) -> None:
        # Audit R5-MED-1: the existence gate collateral-blocked instance-
        # agnostic commands.
        code, out, err = self._run_cli_with_env(
            {"KITE_INSTANCE": "typo-ghost"}, "completion", "bash"
        )

        self.assertEqual(code, 0, err)
        self.assertIn("kitectl", out)


class FailClosedUncreatedInstanceTests(MultiInstanceTestCase):
    """Uncreated named instances are rejected, never implicitly scaffolded
    (FOCUS parity, docs/decisions/multi-instance.md §3)."""

    def _ghost_dirs(self) -> tuple[pathlib.Path, pathlib.Path]:
        return (
            self.config_root / "instances" / "ghost",
            self.data_root / "instances" / "ghost",
        )

    def test_kitectl_flag_rejects_ghost_without_scaffolding(self) -> None:
        code, _, err = self._run_cli("--instance", "ghost", "binding", "list")

        self.assertEqual(code, 2)
        self.assertIn("kitectl instance create ghost", err)
        config_dir, data_dir = self._ghost_dirs()
        self.assertFalse(config_dir.exists())
        self.assertFalse(data_dir.exists())

    def test_kitectl_env_rejects_ghost(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with _saved_env(), patch.dict(
            os.environ, {**self._roots_env(), "KITE_INSTANCE": "ghost"}
        ):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = kitectl.main(["binding", "list"])
        self.assertEqual(code, 2)
        self.assertIn("kitectl instance create ghost", stderr.getvalue())

    def test_service_install_rejects_ghost(self) -> None:
        # Previously this call implicitly mkdir'd the instance dirs
        # (service_manager.install) — the fail-close now fires first.
        code, _, err = self._run_cli("--instance", "ghost", "service", "install")

        self.assertEqual(code, 2)
        self.assertIn("kitectl instance create ghost", err)
        config_dir, data_dir = self._ghost_dirs()
        self.assertFalse(config_dir.exists())
        self.assertFalse(data_dir.exists())

    def test_kited_rejects_ghost_before_lease(self) -> None:
        stderr = io.StringIO()
        with _saved_env(), patch.dict(os.environ, self._roots_env()):
            with contextlib.redirect_stderr(stderr):
                rc = kited.main(["--instance", "ghost"])
        self.assertEqual(rc, 2)
        self.assertIn("kitectl instance create ghost", stderr.getvalue())
        # Rejected before the lease could mkdir the data dir.
        _, data_dir = self._ghost_dirs()
        self.assertFalse(data_dir.exists())

    def test_created_instance_passes(self) -> None:
        code, _, err = self._run_cli("instance", "create", "ghost")
        self.assertEqual(code, 0, err)

        code, out, err = self._run_cli("--instance", "ghost", "binding", "list")

        self.assertEqual(code, 0, err)
        self.assertIn("(no bindings)", out)


if __name__ == "__main__":
    unittest.main()
