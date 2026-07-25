import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kite.env_file import ensure_env_template, env_file_path, load_env_file, parse_env_file


class EnvFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env_path = Path(self._tmp.name) / "env"

    def test_env_file_path_defaults_to_platform_default(self) -> None:
        with patch("kite.env_file.default_env_file", return_value=Path("/tmp/default/env")):
            self.assertEqual(env_file_path(), Path("/tmp/default/env"))

    def test_env_file_path_expands_explicit_path(self) -> None:
        self.assertEqual(env_file_path("~/custom.env"), Path("~/custom.env").expanduser())

    def test_parse_missing_file_returns_empty_dict(self) -> None:
        self.assertEqual(parse_env_file(self.env_path), {})

    def test_parse_skips_comments_blanks_and_malformed_lines(self) -> None:
        self.env_path.write_text(
            "\n".join(
                [
                    "# comment",
                    "",
                    "   ",
                    "no-equals-sign",
                    "=empty-key",
                    "KIMI_API_KEY=sk-test",
                    "QUOTED_DOUBLE=\"value with spaces\"",
                    "QUOTED_SINGLE='other value'",
                    "PADDED =  spaced  ",
                    "EXPORTED=token=with=equals",
                    "UNQUOTED_MIXED=\"not-closed",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            parse_env_file(self.env_path),
            {
                "KIMI_API_KEY": "sk-test",
                "QUOTED_DOUBLE": "value with spaces",
                "QUOTED_SINGLE": "other value",
                "PADDED": "spaced",
                "EXPORTED": "token=with=equals",
                "UNQUOTED_MIXED": '"not-closed',
            },
        )

    def test_load_env_file_does_not_override_existing_by_default(self) -> None:
        self.env_path.write_text("KITE_TEST_KEY=from-file\n", encoding="utf-8")
        with patch.dict(os.environ, {"KITE_TEST_KEY": "from-env"}):
            values = load_env_file(self.env_path)
            self.assertEqual(values, {"KITE_TEST_KEY": "from-file"})
            self.assertEqual(os.environ["KITE_TEST_KEY"], "from-env")

    def test_load_env_file_sets_missing_and_honors_override(self) -> None:
        self.env_path.write_text("KITE_TEST_KEY=from-file\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            load_env_file(self.env_path)
            self.assertEqual(os.environ["KITE_TEST_KEY"], "from-file")
        with patch.dict(os.environ, {"KITE_TEST_KEY": "from-env"}):
            load_env_file(self.env_path, override=True)
            self.assertEqual(os.environ["KITE_TEST_KEY"], "from-file")

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are Unix-specific")
    def test_ensure_env_template_creates_private_file_once(self) -> None:
        created = ensure_env_template(self.env_path)
        self.assertEqual(created, self.env_path)
        content = self.env_path.read_text(encoding="utf-8")
        self.assertIn("KIMI_API_KEY", content)
        mode = stat.S_IMODE(self.env_path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        # A second call must not clobber user edits.
        self.env_path.write_text("EDITED=1\n", encoding="utf-8")
        ensure_env_template(self.env_path)
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), "EDITED=1\n")


if __name__ == "__main__":
    unittest.main()
