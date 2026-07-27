"""kite/install_templates.py: the two template copies stay in sync and the
loader prefers the repo copy with the installed package data as fallback."""

from __future__ import annotations

import pathlib
import unittest
from unittest.mock import patch

from kite import install_templates

REPO_EXAMPLE = (
    pathlib.Path(__file__).resolve().parents[1] / "config" / "system.yaml.example"
)
TEMPLATE_NAME = "system.yaml.example"


class InstallTemplateTests(unittest.TestCase):
    def test_repo_and_packaged_copies_are_identical(self) -> None:
        packaged = install_templates._packaged_template_dir() / TEMPLATE_NAME
        self.assertTrue(packaged.is_file())
        self.assertEqual(
            REPO_EXAMPLE.read_text(encoding="utf-8"),
            packaged.read_text(encoding="utf-8"),
        )

    def test_load_template_prefers_the_repo_copy(self) -> None:
        self.assertEqual(
            install_templates.load_template(TEMPLATE_NAME),
            REPO_EXAMPLE.read_text(encoding="utf-8"),
        )

    def test_load_template_falls_back_to_the_packaged_copy(self) -> None:
        with patch.object(
            install_templates,
            "_repo_root",
            return_value=pathlib.Path("/nonexistent-kite-repo"),
        ):
            text = install_templates.load_template(TEMPLATE_NAME)
        self.assertIn("app_id", text)
        self.assertEqual(text, REPO_EXAMPLE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
