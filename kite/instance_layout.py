"""
Multi-instance filesystem layout (docs/decisions/multi-instance.md §1).

The **default instance keeps the original single-instance paths
byte-identically** (``<config root>`` + ``<data root>``) — the current
deployment becomes the default instance with zero migration. Named instances
live under ``<root>/instances/<name>/`` and get an isolated kap home at
``<data>/kap-home`` (decision §2, the cross-process session-sharing killer).

Path precedence, per axis (several entrypoints rely on this, so it is
documented once here): **explicit directories win over the instance layout**
— ``KITE_CONFIG_DIR`` / ``KITE_DATA_ROOT`` mean "use exactly these
directories" whether or not an instance name is in play; the layout only
supplies the directories that were not given explicitly.
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass

from kite.adapters.kap_server import resolve_kap_home
from kite.platform_paths import default_config_root, default_data_root

DEFAULT_INSTANCE_NAME = "default"
INSTANCES_SEGMENT = "instances"
KAP_HOME_DIR_NAME = "kap-home"

# Decision §1: a name is [a-z0-9][a-z0-9._-]*; `default` (the default
# instance is spelled None, never named), `instances` (the layout segment)
# and `..` are reserved; anything else fails closed.
_INSTANCE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_RESERVED_NAMES = frozenset({DEFAULT_INSTANCE_NAME, INSTANCES_SEGMENT})


def validate_instance_name(name: str) -> str:
    """Fail-closed instance-name validation (decision §1)."""
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("instance name must not be empty")
    if normalized in _RESERVED_NAMES:
        raise ValueError(f"instance name is reserved: {normalized!r}")
    if normalized == ".." or not _INSTANCE_NAME_RE.match(normalized):
        raise ValueError(
            f"instance name must match [a-z0-9][a-z0-9._-]* (got {normalized!r})"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class InstancePaths:
    """One instance's directories; ``instance_name`` None = the default."""

    instance_name: str | None
    config_dir: pathlib.Path
    data_dir: pathlib.Path
    kap_home: pathlib.Path


def resolve(name: str | None = None) -> InstancePaths:
    """Resolve the filesystem layout of one instance.

    ``name=None`` (or empty) resolves the default instance: today's paths,
    byte-identical. A named instance lives under ``instances/<name>/`` with
    its isolated kap home at ``<data>/kap-home``. Explicit ``KITE_CONFIG_DIR``
    / ``KITE_DATA_ROOT`` overrides win per axis over the layout (see the
    module docstring).
    """
    instance_name = (
        validate_instance_name(name) if name and str(name).strip() else None
    )
    raw_config = os.environ.get("KITE_CONFIG_DIR", "").strip()
    raw_data = os.environ.get("KITE_DATA_ROOT", "").strip()
    if instance_name is None:
        config_dir = (
            pathlib.Path(raw_config).expanduser() if raw_config else default_config_root()
        )
        data_dir = pathlib.Path(raw_data).expanduser() if raw_data else default_data_root()
    else:
        config_dir = (
            pathlib.Path(raw_config).expanduser()
            if raw_config
            else default_config_root() / INSTANCES_SEGMENT / instance_name
        )
        data_dir = (
            pathlib.Path(raw_data).expanduser()
            if raw_data
            else default_data_root() / INSTANCES_SEGMENT / instance_name
        )
    return InstancePaths(
        instance_name=instance_name,
        config_dir=config_dir,
        data_dir=data_dir,
        kap_home=data_dir / KAP_HOME_DIR_NAME,
    )


def resolve_effective_kap_home(
    configured: str | None, instance_name: str | None
) -> pathlib.Path:
    """The KIMI_CODE_HOME the instance's kap child runs with (decision §2).

    Precedence: an explicit ``kap.home`` config value always wins; a named
    instance gets its isolated ``<data>/kap-home`` (never the shared
    ``~/.kimi-code``, so no two kap-server processes can write the same
    session directory); the default instance keeps the adapter's resolution
    (``$KIMI_CODE_HOME``, then ``~/.kimi-code``) — its live state is there.
    """
    if configured and str(configured).strip():
        return pathlib.Path(str(configured)).expanduser()
    if instance_name is not None:
        return resolve(instance_name).kap_home
    return resolve_kap_home(None)
