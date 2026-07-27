"""Instance scaffolding: the shared body of ``kitectl instance create`` and
``install.sh --instance`` (docs/decisions/multi-instance.md §1).

Both entry points delegate here so they always produce identical layouts.
The system.yaml template comes from kite.install_templates — repo ``config/``
when present, the installed package data otherwise — so an installed
deployment never reaches back into the source tree.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from kite import env_file, install_templates
from kite.file_permissions import ensure_private_file_permissions
from kite.instance_layout import (
    DEFAULT_INSTANCE_NAME,
    resolve,
    validate_instance_name,
)
from kite.platform_paths import ENV_FILE_NAME

SYSTEM_YAML_NAME = "system.yaml"
SYSTEM_YAML_EXAMPLE_NAME = "system.yaml.example"


@dataclass(frozen=True, slots=True)
class ScaffoldReport:
    """What a scaffold run produced (display name "default" = root instance)."""

    instance_name: str
    config_dir: pathlib.Path
    data_dir: pathlib.Path
    kap_home: pathlib.Path
    system_yaml: pathlib.Path
    system_yaml_created: bool  # False = an existing file was kept untouched
    example_path: pathlib.Path
    env_path: pathlib.Path
    env_created: bool


def scaffold_instance(name: str) -> ScaffoldReport:
    """Lay out one instance's directories and template files (idempotent).

    ``default`` scaffolds the root instance. User-edited files are never
    overwritten: an existing system.yaml / env is kept; the ``*.example``
    reference copy is refreshed on every run so it cannot go stale (FOCUS's
    ``_ensure_instance_scaffold`` semantics).
    """
    normalized = str(name or "").strip()
    instance_name = (
        None
        if normalized == DEFAULT_INSTANCE_NAME
        else validate_instance_name(normalized)
    )
    paths = resolve(instance_name)
    directories = [paths.config_dir, paths.data_dir]
    if instance_name is not None:
        # The default instance's kap home is ~/.kimi-code (decision §2);
        # <data>/kap-home is only ever used by NAMED instances (audit R5).
        directories.append(paths.kap_home)
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    template = install_templates.load_template(SYSTEM_YAML_EXAMPLE_NAME)
    example_path = paths.config_dir / SYSTEM_YAML_EXAMPLE_NAME
    example_path.write_text(template, encoding="utf-8")

    system_yaml = paths.config_dir / SYSTEM_YAML_NAME
    system_yaml_created = False
    if not system_yaml.exists():
        system_yaml.write_text(template, encoding="utf-8")
        ensure_private_file_permissions(system_yaml)
        system_yaml_created = True

    env_path = paths.config_dir / ENV_FILE_NAME
    env_created = not env_path.exists()
    env_file.ensure_env_template(env_path)

    return ScaffoldReport(
        instance_name=instance_name or DEFAULT_INSTANCE_NAME,
        config_dir=paths.config_dir,
        data_dir=paths.data_dir,
        kap_home=paths.kap_home,
        system_yaml=system_yaml,
        system_yaml_created=system_yaml_created,
        example_path=example_path,
        env_path=env_path,
        env_created=env_created,
    )
