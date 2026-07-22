import os
import pathlib
import plistlib
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from kite.service_manager import (
    LAUNCHD_LABEL,
    SERVICE_NAME,
    LaunchdUserServiceManager,
    ServiceManagerError,
    SystemdUserServiceManager,
    WindowsTaskSchedulerServiceManager,
    build_service_definition,
    current_service_manager,
    default_daemon_command,
    managed_venv_dir,
)

_TASK_XML_NAMESPACE = WindowsTaskSchedulerServiceManager._TASK_XML_NAMESPACE


def _definition(root: pathlib.Path):
    return build_service_definition(
        config_dir=root / "config",
        data_dir=root / "data",
        daemon_command=["/tmp/venv/bin/kited"],
    )


class ServiceDefinitionTests(unittest.TestCase):
    def test_build_service_definition_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            with patch("kite.service_manager.default_config_root", return_value=root / "cfg"):
                with patch("kite.service_manager.default_data_root", return_value=root / "dat"):
                    with patch("kite.service_manager.is_windows", return_value=False):
                        definition = build_service_definition()
            self.assertEqual(definition.identifier, SERVICE_NAME)
            self.assertEqual(definition.config_dir, root / "cfg")
            self.assertEqual(definition.data_dir, root / "dat")
            self.assertEqual(definition.daemon_command, (str(root / "dat" / ".venv" / "bin" / "kited"),))
            self.assertEqual(definition.stdout_log_path, root / "dat" / "service.stdout.log")
            self.assertEqual(definition.stderr_log_path, root / "dat" / "service.stderr.log")

    def test_managed_venv_dir_honors_data_root_override(self) -> None:
        with patch.dict(os.environ, {"KITE_DATA_ROOT": "/tmp/kite-data"}, clear=True):
            self.assertEqual(managed_venv_dir(), pathlib.Path("/tmp/kite-data/.venv"))

    def test_default_daemon_command_posix(self) -> None:
        with patch("kite.service_manager.default_data_root", return_value=pathlib.Path("/tmp/kite-data")):
            with patch("kite.service_manager.is_windows", return_value=False):
                self.assertEqual(default_daemon_command(), ("/tmp/kite-data/.venv/bin/kited",))

    def test_default_daemon_command_windows(self) -> None:
        with patch("kite.service_manager.default_data_root", return_value=pathlib.Path("C:/kite-data")):
            with patch("kite.service_manager.is_windows", return_value=True):
                self.assertEqual(default_daemon_command(), (str(pathlib.Path("C:/kite-data/.venv/Scripts/kited.exe")),))


class SystemdUserServiceManagerTests(unittest.TestCase):
    def test_ensure_service_writes_unit_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            run_calls: list[tuple[str, ...]] = []
            manager = SystemdUserServiceManager()
            with patch("kite.service_manager.default_systemd_user_dir", return_value=root / "systemd"):
                with patch.object(
                    manager,
                    "_run",
                    side_effect=lambda *args, **kwargs: (run_calls.append(args), subprocess.CompletedProcess(args, 0, stdout="", stderr=""))[1],
                ):
                    manager.ensure_service(definition)

            unit_path = root / "systemd" / "kite.service"
            self.assertTrue(unit_path.exists())
            rendered = unit_path.read_text(encoding="utf-8")
            self.assertIn("Description=KITE", rendered)
            self.assertIn(f"WorkingDirectory={root}/data", rendered)
            self.assertIn('ExecStart="/tmp/venv/bin/kited"', rendered)
            self.assertIn("Restart=on-failure", rendered)
            self.assertIn("WantedBy=default.target", rendered)
            self.assertEqual(run_calls, [("systemctl", "--user", "daemon-reload")])

    def test_render_unit_quotes_arguments_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = build_service_definition(
                config_dir=root / "config",
                data_dir=root / "data",
                daemon_command=["/tmp/ven v/bin/kited", 'flag"quote'],
            )
            manager = SystemdUserServiceManager()
            rendered = manager._render_unit(definition)
            self.assertIn('ExecStart="/tmp/ven v/bin/kited" "flag\\"quote"', rendered)

    def test_lifecycle_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = SystemdUserServiceManager()
            calls: list[tuple[tuple[str, ...], dict]] = []

            def _run(*args, **kwargs):
                calls.append((args, kwargs))
                if args[:3] == ("systemctl", "--user", "is-active"):
                    return subprocess.CompletedProcess(args, 0, stdout="active\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("kite.service_manager.default_systemd_user_dir", return_value=root / "systemd"):
                with patch.object(manager, "_run", side_effect=_run):
                    manager.ensure_service(definition)
                    manager.start(definition)
                    status = manager.status(definition)
                    manager.uninstall(definition)

            self.assertTrue(status.installed)
            self.assertTrue(status.running)
            self.assertEqual(status.source, "systemctl --user is-active kite")
            self.assertEqual(status.detail, "active")
            self.assertEqual(calls[0][0], ("systemctl", "--user", "daemon-reload"))
            self.assertEqual(calls[1][0], ("systemctl", "--user", "start", "kite"))
            self.assertEqual(calls[2][0], ("systemctl", "--user", "is-active", "kite"))
            self.assertEqual(calls[3][0], ("systemctl", "--user", "disable", "kite"))
            self.assertEqual(calls[4][0], ("systemctl", "--user", "stop", "kite"))
            self.assertEqual(calls[5][0], ("systemctl", "--user", "daemon-reload"))
            self.assertFalse((root / "systemd" / "kite.service").exists())

    def test_autostart_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = SystemdUserServiceManager()
            calls: list[tuple[tuple[str, ...], dict]] = []

            def _run(*args, **kwargs):
                calls.append((args, kwargs))
                if args[:3] == ("systemctl", "--user", "is-enabled"):
                    return subprocess.CompletedProcess(args, 0, stdout="enabled\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("kite.service_manager.default_systemd_user_dir", return_value=root / "systemd"):
                with patch.object(manager, "_run", side_effect=_run):
                    manager.ensure_service(definition)
                    manager.autostart_enable(definition)
                    status = manager.autostart_status(definition)
                    manager.autostart_disable(definition)

            self.assertTrue(status.enabled)
            self.assertEqual(status.source, "systemctl --user is-enabled kite")
            self.assertEqual(status.detail, "enabled")
            self.assertEqual(calls[1][0], ("systemctl", "--user", "enable", "kite"))
            self.assertEqual(calls[2][0], ("systemctl", "--user", "is-enabled", "kite"))
            self.assertEqual(calls[3][0], ("systemctl", "--user", "disable", "kite"))

    def test_status_parses_inactive_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = SystemdUserServiceManager()

            def _run(*args, **kwargs):
                if args[:3] == ("systemctl", "--user", "is-active"):
                    return subprocess.CompletedProcess(args, 3, stdout="inactive\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("kite.service_manager.default_systemd_user_dir", return_value=root / "systemd"):
                with patch.object(manager, "_run", side_effect=_run):
                    manager.ensure_service(definition)
                    status = manager.status(definition)

            self.assertTrue(status.installed)
            self.assertFalse(status.running)
            self.assertEqual(status.detail, "inactive")

    def test_status_reports_missing_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = SystemdUserServiceManager()
            with patch("kite.service_manager.default_systemd_user_dir", return_value=root / "systemd"):
                status = manager.status(definition)
                autostart = manager.autostart_status(definition)
            self.assertFalse(status.installed)
            self.assertFalse(status.running)
            self.assertEqual(status.detail, "unit file missing")
            self.assertFalse(autostart.enabled)
            self.assertEqual(autostart.detail, "unit file missing")

    def test_start_requires_installed_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = SystemdUserServiceManager()
            with patch("kite.service_manager.default_systemd_user_dir", return_value=root / "systemd"):
                with self.assertRaisesRegex(ServiceManagerError, "install.sh"):
                    manager.start(definition)


class LaunchdUserServiceManagerTests(unittest.TestCase):
    def test_ensure_service_writes_plist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = LaunchdUserServiceManager()
            with patch("kite.service_manager.default_launch_agent_dir", return_value=root / "LaunchAgents"):
                manager.ensure_service(definition)

            plist_path = definition.data_dir / "service.plist"
            self.assertTrue(plist_path.exists())
            payload = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(payload["Label"], LAUNCHD_LABEL)
            self.assertEqual(payload["ProgramArguments"], ["/tmp/venv/bin/kited"])
            self.assertEqual(payload["WorkingDirectory"], str(root / "data"))
            self.assertEqual(payload["StandardOutPath"], str(root / "data" / "service.stdout.log"))
            self.assertEqual(payload["StandardErrorPath"], str(root / "data" / "service.stderr.log"))
            self.assertTrue(payload["RunAtLoad"])
            self.assertTrue(payload["KeepAlive"])

    def test_lifecycle_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = LaunchdUserServiceManager()
            calls: list[tuple[tuple[str, ...], dict]] = []

            def _run(*args, **kwargs):
                calls.append((args, kwargs))
                if args[:2] == ("launchctl", "print"):
                    return subprocess.CompletedProcess(args, 0, stdout="state = running\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("kite.service_manager.default_launch_agent_dir", return_value=root / "LaunchAgents"):
                with patch.object(manager, "_uid_domain", return_value="gui/501"):
                    with patch.object(manager, "_run", side_effect=_run):
                        manager.ensure_service(definition)
                        manager.start(definition)
                        status = manager.status(definition)
                        manager.uninstall(definition)

            self.assertTrue(status.installed)
            self.assertTrue(status.running)
            self.assertEqual(status.source, f"launchctl print gui/501/{LAUNCHD_LABEL}")
            self.assertEqual(calls[0][0], ("launchctl", "bootout", "gui/501", LAUNCHD_LABEL))
            self.assertEqual(calls[1][0], ("launchctl", "bootstrap", "gui/501", str(root / "data" / "service.plist")))
            self.assertEqual(calls[2][0], ("launchctl", "kickstart", "-k", f"gui/501/{LAUNCHD_LABEL}"))
            self.assertEqual(calls[3][0], ("launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"))
            self.assertEqual(calls[4][0], ("launchctl", "bootout", "gui/501", LAUNCHD_LABEL))
            self.assertFalse((root / "data" / "service.plist").exists())

    def test_status_parses_non_running_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = LaunchdUserServiceManager()

            def _run(*args, **kwargs):
                if args[:2] == ("launchctl", "print"):
                    return subprocess.CompletedProcess(args, 0, stdout="state = waiting\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("kite.service_manager.default_launch_agent_dir", return_value=root / "LaunchAgents"):
                with patch.object(manager, "_run", side_effect=_run):
                    manager.ensure_service(definition)
                    status = manager.status(definition)

            self.assertTrue(status.installed)
            self.assertFalse(status.running)

    def test_autostart_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = LaunchdUserServiceManager()
            with patch("kite.service_manager.default_launch_agent_dir", return_value=root / "LaunchAgents"):
                manager.ensure_service(definition)
                manager.autostart_enable(definition)
                status = manager.autostart_status(definition)
                manager.autostart_disable(definition)

            self.assertTrue(status.enabled)
            self.assertEqual(status.source, f"LaunchAgent {LAUNCHD_LABEL}")
            self.assertEqual(status.detail, str(root / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"))
            self.assertFalse((root / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist").exists())

    def test_autostart_status_detects_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = LaunchdUserServiceManager()
            with patch("kite.service_manager.default_launch_agent_dir", return_value=root / "LaunchAgents"):
                manager.ensure_service(definition)
                manager.autostart_enable(definition)
                (root / "data" / "service.plist").unlink()
                status = manager.autostart_status(definition)

            self.assertFalse(status.enabled)
            self.assertEqual(status.source, f"LaunchAgent {LAUNCHD_LABEL}")
            self.assertEqual(status.detail, "launch agent symlink is dangling")


class WindowsTaskSchedulerServiceManagerTests(unittest.TestCase):
    def test_ensure_service_writes_launcher_and_registers_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            run_calls: list[tuple[str, ...]] = []
            manager = WindowsTaskSchedulerServiceManager()
            with patch.object(
                manager,
                "_run",
                side_effect=lambda *args, **kwargs: (run_calls.append(args), subprocess.CompletedProcess(args, 1 if "/Query" in args else 0, stdout="", stderr=""))[1],
            ):
                manager.ensure_service(definition)

            launcher_path = definition.data_dir / "service-launch.cmd"
            xml_path = definition.data_dir / "service-task.xml"
            self.assertTrue(launcher_path.exists())
            self.assertTrue(xml_path.exists())
            rendered = launcher_path.read_text(encoding="utf-8")
            self.assertIn("kited", rendered)
            self.assertIn(f'cd /d "{definition.data_dir}"', rendered)
            self.assertEqual(run_calls[0][0:4], ("schtasks", "/Query", "/TN", "kite"))
            self.assertEqual(run_calls[1][0:4], ("schtasks", "/Create", "/TN", "kite"))

    def test_task_xml_renders_logon_trigger_only_when_autostart_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = WindowsTaskSchedulerServiceManager()

            enabled_root = ET.fromstring(manager._task_xml_bytes(definition, autostart_enabled=True))
            disabled_root = ET.fromstring(manager._task_xml_bytes(definition, autostart_enabled=False))

            def _find(root_element: ET.Element, name: str) -> ET.Element | None:
                return root_element.find(f".//{{{_TASK_XML_NAMESPACE}}}{name}")

            self.assertIsNotNone(_find(enabled_root, "LogonTrigger"))
            self.assertIsNone(_find(disabled_root, "LogonTrigger"))
            self.assertEqual(_find(enabled_root, "Description").text, "KITE")
            self.assertEqual(_find(enabled_root, "Command").text, str(definition.data_dir / "service-launch.cmd"))
            self.assertEqual(_find(enabled_root, "RunLevel").text, "LeastPrivilege")

    def test_lifecycle_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = WindowsTaskSchedulerServiceManager()
            calls: list[tuple[tuple[str, ...], dict]] = []

            def _run(*args, **kwargs):
                calls.append((args, kwargs))
                if args[:2] == ("schtasks", "/Query") and "/XML" in args:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")
                if args[:2] == ("schtasks", "/Query"):
                    return subprocess.CompletedProcess(args, 0, stdout="Status: Running\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch.object(manager, "_run", side_effect=_run):
                manager.ensure_service(definition)
                manager.start(definition)
                status = manager.status(definition)
                manager.uninstall(definition)

            self.assertTrue(status.installed)
            self.assertTrue(status.running)
            self.assertEqual(status.source, "schtasks /Query /TN kite /FO LIST /V")
            self.assertEqual(calls[0][0][:4], ("schtasks", "/Query", "/TN", "kite"))
            self.assertEqual(calls[1][0][:4], ("schtasks", "/Create", "/TN", "kite"))
            self.assertEqual(calls[2][0], ("schtasks", "/Run", "/TN", "kite"))
            self.assertEqual(calls[3][0], ("schtasks", "/Query", "/TN", "kite", "/FO", "LIST", "/V"))
            self.assertEqual(calls[4][0], ("schtasks", "/End", "/TN", "kite"))
            self.assertEqual(calls[5][0], ("schtasks", "/Delete", "/TN", "kite", "/F"))
            self.assertFalse((definition.data_dir / "service-launch.cmd").exists())
            self.assertFalse((definition.data_dir / "service-task.xml").exists())

    def test_status_parses_ready_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = WindowsTaskSchedulerServiceManager()

            def _run(*args, **kwargs):
                if args[:2] == ("schtasks", "/Query"):
                    return subprocess.CompletedProcess(args, 0, stdout="Status: Ready\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch.object(manager, "_run", side_effect=_run):
                status = manager.status(definition)

            self.assertTrue(status.installed)
            self.assertFalse(status.running)
            self.assertEqual(status.detail, "Status: Ready")

    def test_status_reports_missing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = WindowsTaskSchedulerServiceManager()

            def _run(*args, **kwargs):
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="ERROR: not found")

            with patch.object(manager, "_run", side_effect=_run):
                status = manager.status(definition)
                autostart = manager.autostart_status(definition)

            self.assertFalse(status.installed)
            self.assertFalse(status.running)
            self.assertEqual(status.detail, "ERROR: not found")
            self.assertFalse(autostart.enabled)
            self.assertEqual(autostart.detail, "scheduled task missing")

    def test_autostart_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = WindowsTaskSchedulerServiceManager()
            calls: list[tuple[tuple[str, ...], dict]] = []

            enabled_xml = """<?xml version="1.0"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger /></Triggers>
</Task>
"""

            def _run(*args, **kwargs):
                calls.append((args, kwargs))
                if args[:2] == ("schtasks", "/Query") and "/XML" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=enabled_xml, stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch.object(manager, "_run", side_effect=_run):
                manager.ensure_service(definition)
                manager.autostart_enable(definition)
                status = manager.autostart_status(definition)
                manager.autostart_disable(definition)

            self.assertTrue(status.enabled)
            self.assertEqual(status.source, "schtasks /Query /TN kite /XML")
            self.assertEqual(status.detail, "logon trigger enabled")
            create_calls = [call for call, _ in calls if call[:2] == ("schtasks", "/Create")]
            self.assertGreaterEqual(len(create_calls), 3)

    def test_autostart_enable_access_denied_surfaces_admin_delete_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = WindowsTaskSchedulerServiceManager()

            enabled_xml = """<?xml version="1.0"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Settings />
</Task>
"""

            def _ensure_run(*args, **kwargs):
                if args[:2] == ("schtasks", "/Query") and "/XML" in args:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch.object(manager, "_run", side_effect=_ensure_run):
                manager.ensure_service(definition)

            def _autostart_run(*args, **kwargs):
                if args[:2] == ("schtasks", "/Query") and "/XML" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=enabled_xml, stderr="")
                if args[:2] == ("schtasks", "/Create"):
                    raise ServiceManagerError("错误: 拒绝访问。")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch.object(manager, "_run", side_effect=_autostart_run):
                with self.assertRaises(ServiceManagerError) as raised:
                    manager.autostart_enable(definition)

        rendered = str(raised.exception)
        self.assertIn("当前 PowerShell 中删除旧任务", rendered)
        self.assertIn("管理员 PowerShell", rendered)
        self.assertIn("schtasks /Delete /TN kite /F", rendered)
        self.assertIn("kitectl service autostart enable", rendered)


class CurrentServiceManagerTests(unittest.TestCase):
    def test_factory_dispatch(self) -> None:
        with patch("kite.service_manager.is_windows", return_value=True):
            self.assertIsInstance(current_service_manager(), WindowsTaskSchedulerServiceManager)
        with patch("kite.service_manager.is_windows", return_value=False):
            with patch("kite.service_manager.is_macos", return_value=True):
                self.assertIsInstance(current_service_manager(), LaunchdUserServiceManager)
        with patch("kite.service_manager.is_windows", return_value=False):
            with patch("kite.service_manager.is_macos", return_value=False):
                with patch("kite.service_manager.is_linux", return_value=True):
                    self.assertIsInstance(current_service_manager(), SystemdUserServiceManager)

    def test_factory_rejects_unsupported_platform(self) -> None:
        with patch("kite.service_manager.is_windows", return_value=False):
            with patch("kite.service_manager.is_macos", return_value=False):
                with patch("kite.service_manager.is_linux", return_value=False):
                    with self.assertRaises(ServiceManagerError):
                        current_service_manager()


if __name__ == "__main__":
    unittest.main()
