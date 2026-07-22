"""
User-service management across supported desktop platforms.

Single instance is the premise (docs/architecture/kite-design.md §9): one
config/data directory pair per machine, one `kited` daemon, and one OS-level
service unit named `kite`. The service layer only renders and drives that
unit; the managed `kimi web --no-open` child process is supervised by kited
itself, never by the OS service definitions here.
"""

from __future__ import annotations

import os
import pathlib
import plistlib
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from kite.platform_paths import (
    default_config_root,
    default_data_root,
    default_launch_agent_dir,
    default_systemd_user_dir,
    is_linux,
    is_macos,
    is_windows,
)

SERVICE_NAME = "kite"
LAUNCHD_LABEL = "io.kite"
MANAGED_VENV_DIR_NAME = ".venv"


class ServiceManagerError(RuntimeError):
    """Raised when local service management fails."""


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    identifier: str
    config_dir: pathlib.Path
    data_dir: pathlib.Path
    daemon_command: tuple[str, ...]
    stdout_log_path: pathlib.Path
    stderr_log_path: pathlib.Path


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    installed: bool
    running: bool
    source: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AutostartStatus:
    enabled: bool
    source: str = ""
    detail: str = ""


def managed_venv_dir() -> pathlib.Path:
    """The managed virtualenv created by install.sh / install.py."""
    return default_data_root() / MANAGED_VENV_DIR_NAME


def default_daemon_command() -> tuple[str, ...]:
    """Single source of truth for the command the OS service runs.

    The service runs the `kited` daemon entrypoint installed into the
    managed virtualenv; kited in turn spawns and supervises the managed
    `kimi web --no-open` child process.
    """
    if is_windows():
        return (str(managed_venv_dir() / "Scripts" / "kited.exe"),)
    return (str(managed_venv_dir() / "bin" / "kited"),)


def build_service_definition(
    *,
    daemon_command: list[str] | tuple[str, ...] | None = None,
    config_dir: pathlib.Path | str | None = None,
    data_dir: pathlib.Path | str | None = None,
) -> ServiceDefinition:
    resolved_config_dir = (
        pathlib.Path(config_dir).expanduser() if config_dir is not None else default_config_root()
    )
    resolved_data_dir = (
        pathlib.Path(data_dir).expanduser() if data_dir is not None else default_data_root()
    )
    command = (
        tuple(str(item) for item in daemon_command)
        if daemon_command is not None
        else default_daemon_command()
    )
    return ServiceDefinition(
        identifier=SERVICE_NAME,
        config_dir=resolved_config_dir,
        data_dir=resolved_data_dir,
        daemon_command=command,
        stdout_log_path=resolved_data_dir / "service.stdout.log",
        stderr_log_path=resolved_data_dir / "service.stderr.log",
    )


def _missing_service_definition_message(path: pathlib.Path) -> str:
    return (
        f"service definition 缺失：{path}。"
        " 请重新运行仓库根目录下的 `install.sh`。"
    )


class ServiceManager:
    def display_name(self, definition: ServiceDefinition) -> str:
        return definition.identifier

    def autostart_enable(self, definition: ServiceDefinition) -> None:
        raise NotImplementedError

    def autostart_disable(self, definition: ServiceDefinition) -> None:
        raise NotImplementedError

    def autostart_status(self, definition: ServiceDefinition) -> AutostartStatus:
        raise NotImplementedError

    def ensure_service(self, definition: ServiceDefinition) -> None:
        raise NotImplementedError

    def start(self, definition: ServiceDefinition) -> None:
        raise NotImplementedError

    def stop(self, definition: ServiceDefinition) -> None:
        raise NotImplementedError

    def restart(self, definition: ServiceDefinition) -> None:
        self.stop(definition)
        self.start(definition)

    def status(self, definition: ServiceDefinition) -> ServiceStatus:
        raise NotImplementedError

    def uninstall(self, definition: ServiceDefinition) -> None:
        raise NotImplementedError


class SystemdUserServiceManager(ServiceManager):
    """Linux-only systemd --user service manager."""

    def _unit_name(self, definition: ServiceDefinition) -> str:
        return definition.identifier

    def display_name(self, definition: ServiceDefinition) -> str:
        return self._unit_name(definition)

    def _autostart_status_source(self, definition: ServiceDefinition) -> str:
        return f"systemctl --user is-enabled {self._unit_name(definition)}"

    def _status_source(self, definition: ServiceDefinition) -> str:
        return f"systemctl --user is-active {self._unit_name(definition)}"

    def _unit_path(self, definition: ServiceDefinition) -> pathlib.Path:
        return default_systemd_user_dir() / f"{self._unit_name(definition)}.service"

    def _require_installed(self, definition: ServiceDefinition) -> pathlib.Path:
        unit_path = self._unit_path(definition)
        if not unit_path.exists():
            raise ServiceManagerError(_missing_service_definition_message(unit_path))
        return unit_path

    @staticmethod
    def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(args),
                check=check,
                text=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise ServiceManagerError("systemctl 不可用。") from exc
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            raise ServiceManagerError(message) from exc

    @staticmethod
    def _quote_unit_arg(arg: str) -> str:
        escaped = str(arg).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _render_unit(self, definition: ServiceDefinition) -> str:
        exec_start = " ".join(self._quote_unit_arg(item) for item in definition.daemon_command)
        return "\n".join(
            [
                "[Unit]",
                "Description=KITE",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"WorkingDirectory={definition.data_dir}",
                f"ExecStart={exec_start}",
                "Restart=on-failure",
                "RestartSec=10",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )

    def ensure_service(self, definition: ServiceDefinition) -> None:
        unit_path = self._unit_path(definition)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        definition.data_dir.mkdir(parents=True, exist_ok=True)
        definition.config_dir.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(self._render_unit(definition), encoding="utf-8")
        self._run("systemctl", "--user", "daemon-reload")

    def autostart_enable(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._run("systemctl", "--user", "enable", self._unit_name(definition))

    def autostart_disable(self, definition: ServiceDefinition) -> None:
        self._run("systemctl", "--user", "disable", self._unit_name(definition), check=False)

    def autostart_status(self, definition: ServiceDefinition) -> AutostartStatus:
        if not self._unit_path(definition).exists():
            return AutostartStatus(
                enabled=False,
                source=self._autostart_status_source(definition),
                detail="unit file missing",
            )
        result = self._run("systemctl", "--user", "is-enabled", self._unit_name(definition), check=False)
        detail = result.stdout.strip() or result.stderr.strip()
        return AutostartStatus(
            enabled=result.returncode == 0,
            source=self._autostart_status_source(definition),
            detail=detail,
        )

    def start(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._run("systemctl", "--user", "start", self._unit_name(definition))

    def stop(self, definition: ServiceDefinition) -> None:
        self._run("systemctl", "--user", "stop", self._unit_name(definition), check=False)

    def restart(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._run("systemctl", "--user", "restart", self._unit_name(definition))

    def status(self, definition: ServiceDefinition) -> ServiceStatus:
        unit_path = self._unit_path(definition)
        if not unit_path.exists():
            return ServiceStatus(installed=False, running=False, source=self._status_source(definition), detail="unit file missing")
        result = self._run("systemctl", "--user", "is-active", self._unit_name(definition), check=False)
        running = result.returncode == 0 and result.stdout.strip() == "active"
        detail = result.stdout.strip() or result.stderr.strip()
        return ServiceStatus(installed=True, running=running, source=self._status_source(definition), detail=detail)

    def uninstall(self, definition: ServiceDefinition) -> None:
        self.autostart_disable(definition)
        self._run("systemctl", "--user", "stop", self._unit_name(definition), check=False)
        try:
            self._unit_path(definition).unlink()
        except FileNotFoundError:
            pass
        self._run("systemctl", "--user", "daemon-reload", check=False)


class LaunchdUserServiceManager(ServiceManager):
    """macOS-only launchd user service manager."""

    def _uid_domain(self) -> str:
        return f"gui/{os.getuid()}"

    def _label(self, definition: ServiceDefinition) -> str:
        return LAUNCHD_LABEL

    def _autostart_status_source(self, definition: ServiceDefinition) -> str:
        return f"LaunchAgent {self._label(definition)}"

    def _status_source(self, definition: ServiceDefinition) -> str:
        return f"launchctl print {self._uid_domain()}/{self._label(definition)}"

    def _definition_path(self, definition: ServiceDefinition) -> pathlib.Path:
        return definition.data_dir / "service.plist"

    def _plist_path(self, definition: ServiceDefinition) -> pathlib.Path:
        return default_launch_agent_dir() / f"{self._label(definition)}.plist"

    def _require_installed(self, definition: ServiceDefinition) -> pathlib.Path:
        plist_path = self._definition_path(definition)
        if not plist_path.exists():
            raise ServiceManagerError(_missing_service_definition_message(plist_path))
        return plist_path

    @staticmethod
    def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(args),
                check=check,
                text=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise ServiceManagerError("launchctl 不可用。") from exc
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            raise ServiceManagerError(message) from exc

    def ensure_service(self, definition: ServiceDefinition) -> None:
        plist_path = self._definition_path(definition)
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        definition.data_dir.mkdir(parents=True, exist_ok=True)
        definition.config_dir.mkdir(parents=True, exist_ok=True)
        definition.stdout_log_path.parent.mkdir(parents=True, exist_ok=True)
        definition.stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": self._label(definition),
            "ProgramArguments": list(definition.daemon_command),
            "WorkingDirectory": str(definition.data_dir),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(definition.stdout_log_path),
            "StandardErrorPath": str(definition.stderr_log_path),
        }
        plist_path.write_bytes(plistlib.dumps(payload))

    def start(self, definition: ServiceDefinition) -> None:
        plist_path = self._require_installed(definition)
        domain = self._uid_domain()
        label = self._label(definition)
        self._run("launchctl", "bootout", domain, label, check=False)
        self._run("launchctl", "bootstrap", domain, str(plist_path))
        self._run("launchctl", "kickstart", "-k", f"{domain}/{label}", check=False)

    def stop(self, definition: ServiceDefinition) -> None:
        domain = self._uid_domain()
        label = self._label(definition)
        self._run("launchctl", "bootout", domain, label, check=False)

    def restart(self, definition: ServiceDefinition) -> None:
        self.start(definition)

    def status(self, definition: ServiceDefinition) -> ServiceStatus:
        plist_path = self._definition_path(definition)
        if not plist_path.exists():
            return ServiceStatus(installed=False, running=False, source=self._status_source(definition), detail="plist missing")
        domain = self._uid_domain()
        label = self._label(definition)
        result = self._run("launchctl", "print", f"{domain}/{label}", check=False)
        running = result.returncode == 0 and "state = running" in result.stdout
        detail = result.stdout.strip() or result.stderr.strip()
        return ServiceStatus(installed=True, running=running, source=self._status_source(definition), detail=detail)

    def uninstall(self, definition: ServiceDefinition) -> None:
        self.stop(definition)
        self.autostart_disable(definition)
        try:
            self._definition_path(definition).unlink()
        except FileNotFoundError:
            pass

    def autostart_enable(self, definition: ServiceDefinition) -> None:
        definition_path = self._require_installed(definition)
        autostart_path = self._plist_path(definition)
        autostart_path.parent.mkdir(parents=True, exist_ok=True)
        if autostart_path.exists() or autostart_path.is_symlink():
            autostart_path.unlink()
        autostart_path.symlink_to(definition_path)

    def autostart_disable(self, definition: ServiceDefinition) -> None:
        try:
            self._plist_path(definition).unlink()
        except FileNotFoundError:
            pass

    def autostart_status(self, definition: ServiceDefinition) -> AutostartStatus:
        autostart_path = self._plist_path(definition)
        if autostart_path.is_symlink() and not autostart_path.exists():
            return AutostartStatus(
                enabled=False,
                source=self._autostart_status_source(definition),
                detail="launch agent symlink is dangling",
            )
        enabled = autostart_path.exists()
        detail = str(autostart_path) if enabled else "launch agent disabled"
        return AutostartStatus(
            enabled=enabled,
            source=self._autostart_status_source(definition),
            detail=detail,
        )


class WindowsTaskSchedulerServiceManager(ServiceManager):
    _TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"

    def _task_name(self, definition: ServiceDefinition) -> str:
        return definition.identifier

    def _autostart_status_source(self, definition: ServiceDefinition) -> str:
        return f"schtasks /Query /TN {self._task_name(definition)} /XML"

    def _status_source(self, definition: ServiceDefinition) -> str:
        return f"schtasks /Query /TN {self._task_name(definition)} /FO LIST /V"

    def _launcher_path(self, definition: ServiceDefinition) -> pathlib.Path:
        return definition.data_dir / "service-launch.cmd"

    def _task_xml_path(self, definition: ServiceDefinition) -> pathlib.Path:
        return definition.data_dir / "service-task.xml"

    @staticmethod
    def _is_access_denied_error(message: str) -> bool:
        normalized = str(message or "").strip().lower()
        if not normalized:
            return False
        # Localized Windows builds report the same failure in Chinese.
        return "access is denied" in normalized or "拒绝访问" in normalized

    def _rewrite_existing_task_access_denied_message(self, definition: ServiceDefinition, original_message: str) -> str:
        task_name = self._task_name(definition)
        return (
            "Task Scheduler 拒绝改写现有任务；这通常表示该任务是由不同权限上下文创建的旧任务。\n"
            "请先在当前 PowerShell 中删除旧任务；如果仍然提示拒绝访问，再改用管理员 PowerShell：\n"
            f"  schtasks /Delete /TN {task_name} /F\n"
            "  kitectl service autostart enable\n"
            f"原始错误：{original_message}"
        )

    def _require_installed(self, definition: ServiceDefinition) -> pathlib.Path:
        launcher_path = self._launcher_path(definition)
        if not launcher_path.exists():
            raise ServiceManagerError(_missing_service_definition_message(launcher_path))
        return launcher_path

    @staticmethod
    def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(args),
                check=check,
                text=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise ServiceManagerError("schtasks.exe 不可用。") from exc
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            raise ServiceManagerError(message) from exc

    def _query_task_xml(self, definition: ServiceDefinition) -> ET.Element | None:
        result = self._run("schtasks", "/Query", "/TN", self._task_name(definition), "/XML", check=False)
        if result.returncode != 0:
            return None
        try:
            return ET.fromstring(result.stdout)
        except ET.ParseError as exc:
            raise ServiceManagerError("Task Scheduler 返回了无法解析的 XML。") from exc

    def _task_autostart_enabled(self, definition: ServiceDefinition) -> bool:
        root = self._query_task_xml(definition)
        if root is None:
            return False
        return root.find(f".//{{{self._TASK_XML_NAMESPACE}}}LogonTrigger") is not None

    def _task_xml_bytes(self, definition: ServiceDefinition, *, autostart_enabled: bool) -> bytes:
        ET.register_namespace("", self._TASK_XML_NAMESPACE)

        def tag(name: str) -> str:
            return f"{{{self._TASK_XML_NAMESPACE}}}{name}"

        task = ET.Element(tag("Task"), {"version": "1.3"})
        registration = ET.SubElement(task, tag("RegistrationInfo"))
        ET.SubElement(registration, tag("Description")).text = "KITE"
        if autostart_enabled:
            triggers = ET.SubElement(task, tag("Triggers"))
            logon = ET.SubElement(triggers, tag("LogonTrigger"))
            ET.SubElement(logon, tag("Enabled")).text = "true"
        principals = ET.SubElement(task, tag("Principals"))
        principal = ET.SubElement(principals, tag("Principal"), {"id": "Author"})
        ET.SubElement(principal, tag("LogonType")).text = "InteractiveToken"
        ET.SubElement(principal, tag("RunLevel")).text = "LeastPrivilege"
        settings = ET.SubElement(task, tag("Settings"))
        for key, value in (
            ("AllowStartOnDemand", "true"),
            ("MultipleInstancesPolicy", "IgnoreNew"),
            ("DisallowStartIfOnBatteries", "false"),
            ("StopIfGoingOnBatteries", "false"),
            ("AllowHardTerminate", "true"),
            ("StartWhenAvailable", "false"),
            ("RunOnlyIfNetworkAvailable", "false"),
            ("Enabled", "true"),
            ("Hidden", "false"),
            ("ExecutionTimeLimit", "PT0S"),
            ("Priority", "7"),
            ("RunOnlyIfIdle", "false"),
            ("WakeToRun", "false"),
        ):
            ET.SubElement(settings, tag(key)).text = value
        actions = ET.SubElement(task, tag("Actions"), {"Context": "Author"})
        exec_action = ET.SubElement(actions, tag("Exec"))
        ET.SubElement(exec_action, tag("Command")).text = str(self._launcher_path(definition))
        return ET.tostring(task, encoding="utf-16", xml_declaration=True)

    def _register_task(self, definition: ServiceDefinition, *, autostart_enabled: bool) -> None:
        xml_path = self._task_xml_path(definition)
        xml_path.write_bytes(self._task_xml_bytes(definition, autostart_enabled=autostart_enabled))
        try:
            self._run(
                "schtasks",
                "/Create",
                "/TN",
                self._task_name(definition),
                "/XML",
                str(xml_path),
                "/F",
            )
        except ServiceManagerError as exc:
            if self._is_access_denied_error(str(exc)) and self._query_task_xml(definition) is not None:
                raise ServiceManagerError(
                    self._rewrite_existing_task_access_denied_message(definition, str(exc))
                ) from exc
            raise

    def ensure_service(self, definition: ServiceDefinition) -> None:
        definition.data_dir.mkdir(parents=True, exist_ok=True)
        definition.config_dir.mkdir(parents=True, exist_ok=True)
        launcher_path = self._launcher_path(definition)
        launcher_path.write_text(
            "\r\n".join(
                [
                    "@echo off",
                    f'cd /d "{definition.data_dir}"',
                    " ".join(f'"{item}"' for item in definition.daemon_command),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._register_task(definition, autostart_enabled=self._task_autostart_enabled(definition))

    def start(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._run("schtasks", "/Run", "/TN", self._task_name(definition))

    def stop(self, definition: ServiceDefinition) -> None:
        self._run("schtasks", "/End", "/TN", self._task_name(definition), check=False)

    def status(self, definition: ServiceDefinition) -> ServiceStatus:
        result = self._run("schtasks", "/Query", "/TN", self._task_name(definition), "/FO", "LIST", "/V", check=False)
        if result.returncode != 0:
            return ServiceStatus(
                installed=False,
                running=False,
                source=self._status_source(definition),
                detail=result.stderr.strip() or result.stdout.strip(),
            )
        status_line = next((line for line in result.stdout.splitlines() if line.startswith("Status:")), "")
        running = "Running" in status_line
        return ServiceStatus(
            installed=True,
            running=running,
            source=self._status_source(definition),
            detail=status_line.strip(),
        )

    def uninstall(self, definition: ServiceDefinition) -> None:
        self.stop(definition)
        self._run("schtasks", "/Delete", "/TN", self._task_name(definition), "/F", check=False)
        try:
            self._launcher_path(definition).unlink()
        except FileNotFoundError:
            pass
        try:
            self._task_xml_path(definition).unlink()
        except FileNotFoundError:
            pass

    def autostart_enable(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._register_task(definition, autostart_enabled=True)

    def autostart_disable(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._register_task(definition, autostart_enabled=False)

    def autostart_status(self, definition: ServiceDefinition) -> AutostartStatus:
        if self._query_task_xml(definition) is None:
            return AutostartStatus(
                enabled=False,
                source=self._autostart_status_source(definition),
                detail="scheduled task missing",
            )
        enabled = self._task_autostart_enabled(definition)
        detail = "logon trigger enabled" if enabled else "logon trigger disabled"
        return AutostartStatus(
            enabled=enabled,
            source=self._autostart_status_source(definition),
            detail=detail,
        )


def current_service_manager() -> ServiceManager:
    if is_windows():
        return WindowsTaskSchedulerServiceManager()
    if is_macos():
        return LaunchdUserServiceManager()
    if is_linux():
        return SystemdUserServiceManager()
    raise ServiceManagerError("当前平台不支持后台 service 管理。")
