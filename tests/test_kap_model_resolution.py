"""Model resolution helpers (the per-prompt model contract)."""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from kite.adapters.kap_server import read_kimi_default_model, resolve_prompt_model


class ReadKimiDefaultModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = pathlib.Path(self._tmp.name)

    def _write_config(self, text: str) -> None:
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def test_reads_default_model(self) -> None:
        self._write_config('default_model = "kimi-code/k3"\n')
        self.assertEqual(read_kimi_default_model(self.home), "kimi-code/k3")

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(read_kimi_default_model(self.home))

    def test_invalid_toml_returns_none(self) -> None:
        self._write_config("not = [valid\n")
        self.assertIsNone(read_kimi_default_model(self.home))

    def test_non_string_returns_none(self) -> None:
        self._write_config("default_model = 42\n")
        self.assertIsNone(read_kimi_default_model(self.home))


class ResolvePromptModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = pathlib.Path(self._tmp.name)

    def test_config_model_wins(self) -> None:
        (self.home / "config.toml").write_text(
            'default_model = "model-a"\n', encoding="utf-8"
        )
        self.assertEqual(resolve_prompt_model("model-b", self.home), "model-b")

    def test_falls_back_to_config_toml(self) -> None:
        (self.home / "config.toml").write_text(
            'default_model = "model-a"\n', encoding="utf-8"
        )
        self.assertEqual(resolve_prompt_model(None, self.home), "model-a")

    def test_none_when_unresolvable(self) -> None:
        self.assertIsNone(resolve_prompt_model(None, self.home))


if __name__ == "__main__":
    unittest.main()
