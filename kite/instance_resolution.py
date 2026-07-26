"""
Instance resolution for the entrypoints (docs/decisions/multi-instance.md §3).

kitectl resolution ladder:

1. explicit ``--instance <name>`` (or the ``KITE_INSTANCE`` env var),
2. the single running instance — discovered via per-instance
   ``control_plane.json`` metadata with stale pids filtered; ambiguity (more
   than one live instance, none explicit) fails closed with the candidate
   list,
3. the default instance.

``kitectl service`` commands skip rung 2 (explicit-or-default only: no
"single running" convenience for destructive ops). kited never uses rung 2
either — the daemon IS an instance, spelled via ``--instance`` /
``KITE_INSTANCE`` or the default.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from kite.control_plane import ControlPlaneMetadata, discover_live_control_metadata
from kite.instance_layout import (
    DEFAULT_INSTANCE_NAME,
    INSTANCES_SEGMENT,
    validate_instance_name,
)
from kite.platform_paths import default_data_root

INSTANCE_ENV_VAR = "KITE_INSTANCE"


@dataclass(frozen=True, slots=True)
class RunningInstance:
    """One live daemon: instance name (None = default) + its endpoint facts."""

    instance_name: str | None
    data_dir: pathlib.Path
    metadata: ControlPlaneMetadata

    @property
    def display_name(self) -> str:
        return self.instance_name or DEFAULT_INSTANCE_NAME


def list_running_instances() -> list[RunningInstance]:
    """All live instances (default root + ``instances/*``), stale pids filtered."""
    running: list[RunningInstance] = []
    default_root = default_data_root()
    metadata = discover_live_control_metadata(default_root)
    if metadata is not None:
        running.append(RunningInstance(None, default_root, metadata))
    instances_root = default_root / INSTANCES_SEGMENT
    if instances_root.is_dir():
        for child in sorted(instances_root.iterdir()):
            if not child.is_dir():
                continue
            try:
                name = validate_instance_name(child.name)
            except ValueError:
                continue  # not an instance dir; never fail discovery on it
            metadata = discover_live_control_metadata(child)
            if metadata is not None:
                running.append(RunningInstance(name, child, metadata))
    return running


def explicit_instance_name() -> str | None:
    """The KITE_INSTANCE env value, validated fail-closed (None when unset)."""
    raw = os.environ.get(INSTANCE_ENV_VAR, "").strip()
    return validate_instance_name(raw) if raw else None


def resolve_instance_name(
    flag_value: str | None = None,
    *,
    allow_single_running: bool = True,
) -> str | None:
    """The instance kitectl targets; None means the default instance.

    Raises ValueError on a bad explicit name or on rung-2 ambiguity (the
    message lists the candidates) — kitectl maps both onto exit code 2.
    """
    if flag_value and str(flag_value).strip():
        return validate_instance_name(flag_value)
    env_name = explicit_instance_name()
    if env_name is not None:
        return env_name
    if allow_single_running:
        running = list_running_instances()
        if len(running) == 1:
            return running[0].instance_name
        if len(running) > 1:
            candidates = ", ".join(item.display_name for item in running)
            raise ValueError(
                f"multiple instances are running ({candidates}); "
                f"pass --instance <name> or set {INSTANCE_ENV_VAR} explicitly"
            )
    return None


def daemon_instance_name(flag_value: str | None = None) -> str | None:
    """kited's own instance: ``--instance`` > KITE_INSTANCE > default.

    The daemon never uses the single-running rung — it would be the running
    instance itself.
    """
    if flag_value and str(flag_value).strip():
        return validate_instance_name(flag_value)
    return explicit_instance_name()
