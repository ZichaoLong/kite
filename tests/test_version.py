import unittest

from kite.version import __version__


class VersionTests(unittest.TestCase):
    def test_version_is_a_non_empty_string(self) -> None:
        self.assertIsInstance(__version__, str)
        self.assertTrue(__version__)


if __name__ == "__main__":
    unittest.main()
