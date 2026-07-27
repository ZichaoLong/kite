"""Install-time template loading (FOCUS's install_templates.py shape).

Templates ship in two synced places:

- the repo's ``config/`` directory (what users browsing the source tree
  see, and what the README/install output references), and
- the installed package as package data (``kite.install_template_data``),
  so an installed deployment never needs the source tree.

Loading prefers the repo copy when it exists (developers edit the repo
file and see the effect immediately) and falls back to the packaged copy;
``tests/test_install_templates.py`` asserts the two copies stay identical.
"""

from __future__ import annotations

import importlib.resources
import pathlib

_TEMPLATE_PACKAGE = "kite.install_template_data"


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def _packaged_template_dir() -> pathlib.Path:
    return pathlib.Path(str(importlib.resources.files(_TEMPLATE_PACKAGE)))


def load_template(filename: str) -> str:
    """Return a template's text: repo ``config/`` first, packaged fallback."""
    repo_path = _repo_root() / "config" / filename
    if repo_path.exists():
        return repo_path.read_text(encoding="utf-8")
    packaged_path = _packaged_template_dir() / filename
    return packaged_path.read_text(encoding="utf-8")
