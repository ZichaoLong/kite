"""
Cross-platform local filesystem layout helpers.

The repository keeps an explicit separation between:

- per-machine config root
- per-machine data root
- user-facing launcher directory
"""

from __future__ import annotations

import os
import pathlib
import sys

APP_NAME = "kite"
ENV_FILE_NAME = "env"
LOG_FILE_NAME = "kite.log"


def current_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def is_windows() -> bool:
    return current_platform() == "windows"


def is_macos() -> bool:
    return current_platform() == "macos"


def is_linux() -> bool:
    return current_platform() == "linux"


def default_config_root() -> pathlib.Path:
    raw = os.environ.get("KITE_CONFIG_ROOT", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    home = pathlib.Path.home()
    if is_windows():
        appdata = pathlib.Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return appdata / APP_NAME / "config"
    if is_macos():
        return home / "Library" / "Application Support" / APP_NAME / "config"
    return home / ".config" / APP_NAME


def default_data_root() -> pathlib.Path:
    raw = os.environ.get("KITE_DATA_ROOT", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    home = pathlib.Path.home()
    if is_windows():
        local_appdata = pathlib.Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return local_appdata / APP_NAME / "data"
    if is_macos():
        return home / "Library" / "Application Support" / APP_NAME / "data"
    return home / ".local" / "share" / APP_NAME


def default_working_dir() -> pathlib.Path:
    return pathlib.Path.home()


def default_user_bin_dir() -> pathlib.Path:
    raw = os.environ.get("KITE_BIN_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    home = pathlib.Path.home()
    if is_windows():
        local_appdata = pathlib.Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return local_appdata / APP_NAME / "bin"
    return home / ".local" / "bin"


def default_user_bash_completion_dir() -> pathlib.Path | None:
    raw = os.environ.get("KITE_BASH_COMPLETION_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    if is_windows():
        return None
    raw_user_dir = os.environ.get("BASH_COMPLETION_USER_DIR", "").strip()
    if raw_user_dir:
        return pathlib.Path(raw_user_dir).expanduser() / "completions"
    return pathlib.Path.home() / ".local" / "share" / "bash-completion" / "completions"


def default_user_zsh_completion_path() -> pathlib.Path | None:
    raw = os.environ.get("KITE_ZSH_COMPLETION_PATH", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    if is_windows():
        return None
    return default_config_root() / "shell-completion" / "kite.zsh"


def default_user_zsh_rc_path() -> pathlib.Path | None:
    raw = os.environ.get("KITE_ZSH_RC_PATH", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    if is_windows():
        return None
    return pathlib.Path.home() / ".zshrc"


def default_user_powershell_profile_path() -> pathlib.Path | None:
    raw = os.environ.get("KITE_POWERSHELL_PROFILE_PATH", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    if not is_windows():
        return None
    return pathlib.Path.home() / "Documents" / "PowerShell" / "profile.ps1"


def default_user_powershell_completion_path() -> pathlib.Path | None:
    raw = os.environ.get("KITE_POWERSHELL_COMPLETION_PATH", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    if default_user_powershell_profile_path() is None:
        return None
    return default_config_root() / "shell-completion" / "kite.ps1"


def default_env_file() -> pathlib.Path:
    raw = os.environ.get("KITE_ENV_FILE", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    return default_config_root() / ENV_FILE_NAME


def default_log_file(data_dir: pathlib.Path | str | None = None) -> pathlib.Path:
    root = pathlib.Path(data_dir).expanduser() if data_dir is not None else default_data_root()
    return root / LOG_FILE_NAME


def default_systemd_user_dir() -> pathlib.Path:
    return pathlib.Path.home() / ".config" / "systemd" / "user"


def default_launch_agent_dir() -> pathlib.Path:
    return pathlib.Path.home() / "Library" / "LaunchAgents"
