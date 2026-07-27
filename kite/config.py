"""
Instance configuration loading.

The config directory is resolved dynamically per call so importing this module
never freezes directory state too early.
"""

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from kite.file_permissions import ensure_private_file_permissions
from kite.platform_paths import default_config_root
from kite.platform_paths import default_working_dir as _platform_default_working_dir

_INIT_TOKEN_FILENAME = "init.token"
_CONTROL_TOKEN_FILENAME = "control.token"

# Approval cards that receive no response within this window are resolved to
# upstream as rejected (never auto-approved). See docs/contracts/mvp-scope.md.
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300

# Inbound attachment staging (docs/contracts/images.md §2): pending records
# expire after this TTL and staged bytes are capped post-download.
DEFAULT_ATTACHMENT_TTL_SECONDS = 600
DEFAULT_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024

# Assistant-mode group context (docs/contracts/group-chat.md §3.3): the
# Feishu REST history backfill merged into the local log is capped at this
# many messages over this lookback window. 0 disables the backfill (context
# is then local-log only — the fetch-failure block never fires).
DEFAULT_GROUP_HISTORY_FETCH_LIMIT = 50
DEFAULT_GROUP_HISTORY_FETCH_LOOKBACK_SECONDS = 24 * 3600


def config_dir() -> Path:
    raw = os.environ.get("KITE_CONFIG_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return default_config_root()


def system_config_path() -> Path:
    return config_dir() / "system.yaml"


def init_token_path() -> Path:
    return config_dir() / _INIT_TOKEN_FILENAME


def control_token_path() -> Path:
    return config_dir() / _CONTROL_TOKEN_FILENAME


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    if mode is not None:
        if mode == 0o600:
            ensure_private_file_permissions(tmp_path)
        else:
            os.chmod(tmp_path, mode)
    os.replace(tmp_path, path)


def load_system_config_raw() -> dict[str, Any]:
    return _load_yaml_file(system_config_path())


def save_system_config(config: dict[str, Any]) -> Path:
    path = system_config_path()
    rendered = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    _atomic_write_text(path, rendered, mode=0o600)
    return path


def save_system_config_updates(updates: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    config = load_system_config_raw()
    config.update(updates)
    return config, save_system_config(config)


def ensure_init_token() -> str:
    path = init_token_path()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    _atomic_write_text(path, f"{token}\n", mode=0o600)
    return token


def ensure_control_token() -> str:
    """The daemon-issued control-plane token (docs/decisions/control-plane.md).

    Created 0600 at daemon start when absent, alongside the other instance
    secrets; kitectl reads it to authenticate control-plane requests.
    """
    path = control_token_path()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    _atomic_write_text(path, f"{token}\n", mode=0o600)
    return token


def load_config() -> dict:
    """Load the global instance config (system.yaml)."""
    path = system_config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"instance config file does not exist: {path}\n"
            "Run `kitectl instance create` to scaffold the instance (it "
            "copies system.yaml.example next to this file), then fill in "
            "real values."
        )

    config = _load_yaml_file(path)

    if not config.get("app_id") or not config.get("app_secret"):
        raise ValueError(f"app_id and app_secret must not be empty in {path}")

    return config


def load_config_file(name: str, *, directory: Path | str | None = None) -> dict:
    """Load a component config file ({name}.yaml).

    `directory` lets a local CLI explicitly read another instance's config
    directory. A missing file returns an empty dict; components then fall
    back to their own defaults.
    """
    root = Path(directory).expanduser() if directory is not None else config_dir()
    path = root / f"{name}.yaml"
    return _load_yaml_file(path)


def save_config_file(name: str, config: dict[str, Any]) -> Path:
    """Save a component config file ({name}.yaml)."""
    path = config_dir() / f"{name}.yaml"
    rendered = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    _atomic_write_text(path, rendered, mode=0o600)
    return path


def admin_open_ids(config: Mapping[str, Any]) -> set[str]:
    """The instance admin set (Feishu open_ids) from the loaded config."""
    raw = config.get("admin_open_ids") or []
    if not isinstance(raw, list):
        raise ValueError("admin_open_ids must be a list of open_ids")
    return {str(item).strip() for item in raw if str(item).strip()}


def default_working_dir(config: Mapping[str, Any]) -> str:
    """cwd used when first-use session creation binds an unbound chat."""
    raw = str(config.get("default_working_dir") or "").strip()
    if not raw:
        return str(_platform_default_working_dir())
    return str(Path(raw).expanduser())


def approval_timeout_seconds(config: Mapping[str, Any]) -> int:
    """Seconds before an unanswered approval is resolved as rejected."""
    raw = config.get("approval_timeout_seconds", DEFAULT_APPROVAL_TIMEOUT_SECONDS)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("approval_timeout_seconds must be a positive integer") from exc
    if isinstance(raw, bool) or value <= 0:
        raise ValueError("approval_timeout_seconds must be a positive integer")
    return value


def attachment_ttl_seconds(config: Mapping[str, Any]) -> int:
    """Seconds a staged inbound attachment stays pending consumption."""
    raw = config.get("attachment_ttl_seconds", DEFAULT_ATTACHMENT_TTL_SECONDS)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("attachment_ttl_seconds must be a positive integer") from exc
    if isinstance(raw, bool) or value <= 0:
        raise ValueError("attachment_ttl_seconds must be a positive integer")
    return value


def attachment_max_bytes(config: Mapping[str, Any]) -> int:
    """Post-download byte cap for one inbound attachment (fail-closed)."""
    raw = config.get("attachment_max_bytes", DEFAULT_ATTACHMENT_MAX_BYTES)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("attachment_max_bytes must be a positive integer") from exc
    if isinstance(raw, bool) or value <= 0:
        raise ValueError("attachment_max_bytes must be a positive integer")
    return value


def _non_negative_int(config: Mapping[str, Any], key: str, default: int) -> int:
    raw = config.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a non-negative integer") from exc
    if isinstance(raw, bool) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def group_history_fetch_limit(config: Mapping[str, Any]) -> int:
    """Max messages the assistant-mode REST history backfill returns."""
    return _non_negative_int(
        config, "group_history_fetch_limit", DEFAULT_GROUP_HISTORY_FETCH_LIMIT
    )


def group_history_fetch_lookback_seconds(config: Mapping[str, Any]) -> int:
    """Lookback window (seconds) for the assistant-mode history backfill."""
    return _non_negative_int(
        config,
        "group_history_fetch_lookback_seconds",
        DEFAULT_GROUP_HISTORY_FETCH_LOOKBACK_SECONDS,
    )


# ---------------------------------------------------------------------------
# kap-server connection/supervision settings (the `kap:` config section)
# ---------------------------------------------------------------------------


# kap.host is validated loopback-only (audit L29): the managed kap-server
# child is spawned without --host and binds loopback, and kap-server has no
# TLS — a non-loopback host could never connect and only misleads.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True, slots=True)
class KapSettings:
    """The `kap:` section of system.yaml.

    All fields except host are Optional: None means "use the adapter's
    default". `home`/`kimi_bin` additionally fall back to the KIMI_CODE_HOME /
    KIMI_BIN environment variables (resolution lives in the adapter).

    Example system.yaml:

        kap:
          host: 127.0.0.1        # loopback only; kap-server has no TLS
          port: 58627            # requested port; the server may bump +1
          home: ~/.kimi-code     # KIMI_CODE_HOME for the managed child
          kimi_bin: null         # default: $KIMI_BIN or `kimi` from PATH
          model: null            # model carried per prompt; default: config.toml default_model
          stale_seconds: 45      # WS probe (ping) when no frame for this long
          reconnect_delay_seconds: 2
          backoff_base_seconds: 1    # crash-restart backoff: base * 2^n
          backoff_cap_seconds: 30    # ... capped here
    """

    host: str
    port: int | None
    home: str | None
    kimi_bin: str | None
    model: str | None
    stale_seconds: float | None
    reconnect_delay_seconds: float | None
    backoff_base_seconds: float | None
    backoff_cap_seconds: float | None


def kap_settings(config: Mapping[str, Any]) -> KapSettings:
    """Parse and validate the `kap:` section (absent section → all defaults)."""
    raw = config.get("kap")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("kap must be a mapping")

    host = raw.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("kap.host must be a non-empty string")
    if host.strip().lower() not in _LOOPBACK_HOSTS:
        raise ValueError(
            "kap.host must be a loopback address (127.0.0.1, ::1 or localhost): "
            "the managed kap-server child is never passed --host and binds "
            "loopback only, so any other value can never connect"
        )

    port = raw.get("port")
    if port is not None:
        if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
            raise ValueError("kap.port must be an integer in 1..65535")

    home = raw.get("home")
    if home is not None and (not isinstance(home, str) or not home.strip()):
        raise ValueError("kap.home must be a non-empty string")

    kimi_bin = raw.get("kimi_bin")
    if kimi_bin is not None and (not isinstance(kimi_bin, str) or not kimi_bin.strip()):
        raise ValueError("kap.kimi_bin must be a non-empty string")

    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("kap.model must be a non-empty string")

    def _positive_seconds(key: str) -> float | None:
        value = raw.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"kap.{key} must be a positive number")
        return float(value)

    return KapSettings(
        host=host.strip(),
        port=port,
        home=home.strip() if isinstance(home, str) else None,
        kimi_bin=kimi_bin.strip() if isinstance(kimi_bin, str) else None,
        model=model.strip() if isinstance(model, str) else None,
        stale_seconds=_positive_seconds("stale_seconds"),
        reconnect_delay_seconds=_positive_seconds("reconnect_delay_seconds"),
        backoff_base_seconds=_positive_seconds("backoff_base_seconds"),
        backoff_cap_seconds=_positive_seconds("backoff_cap_seconds"),
    )
