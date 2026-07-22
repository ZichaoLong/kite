"""
Shared provider environment file handling.

The runtime loads one explicit env file regardless of the surrounding service
manager so installation stays platform-neutral.
"""

from __future__ import annotations

import os
import pathlib

from kite.file_permissions import ensure_private_file_permissions
from kite.platform_paths import default_env_file


def env_file_path(path: pathlib.Path | str | None = None) -> pathlib.Path:
    if path is None:
        return default_env_file()
    return pathlib.Path(path).expanduser()


def parse_env_file(path: pathlib.Path | str | None = None) -> dict[str, str]:
    resolved = env_file_path(path)
    if not resolved.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key:
            continue
        normalized_value = value.strip()
        if (
            len(normalized_value) >= 2
            and normalized_value[0] == normalized_value[-1]
            and normalized_value[0] in {"'", '"'}
        ):
            normalized_value = normalized_value[1:-1]
        values[normalized_key] = normalized_value
    return values


def load_env_file(path: pathlib.Path | str | None = None, *, override: bool = False) -> dict[str, str]:
    values = parse_env_file(path)
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def ensure_env_template(path: pathlib.Path | str | None = None) -> pathlib.Path:
    resolved = env_file_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.exists():
        return resolved
    resolved.write_text(
        "\n".join(
            [
                "# kimi-code provider environment variables.",
                "# Restart KITE after edits when the background service is running.",
                "#",
                "# Example:",
                "# KIMI_API_KEY=your-api-key",
                "",
            ]
        ),
        encoding="utf-8",
    )
    ensure_private_file_permissions(resolved)
    return resolved
