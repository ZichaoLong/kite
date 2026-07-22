import logging
import tempfile
import unittest
from pathlib import Path

from kite.logging_setup import configure_logging


class LoggingSetupTests(unittest.TestCase):
    def tearDown(self) -> None:
        root_logger = logging.getLogger()
        for handler in root_logger.handlers:
            handler.close()
        root_logger.handlers.clear()

    def test_configure_logging_writes_to_data_dir_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = configure_logging(data_dir=tmp)
            self.assertEqual(log_path, Path(tmp) / "kite.log")
            root_logger = logging.getLogger()
            self.assertEqual(root_logger.level, logging.INFO)
            self.assertEqual(len(root_logger.handlers), 2)
            logging.getLogger("kite.test").info("hello kite")
            for handler in root_logger.handlers:
                handler.flush()
            self.assertIn("hello kite", log_path.read_text(encoding="utf-8"))

    def test_configure_logging_replaces_existing_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_logger = logging.getLogger()
            root_logger.addHandler(logging.NullHandler())
            configure_logging(data_dir=tmp)
            self.assertEqual(len(root_logger.handlers), 2)
            self.assertFalse(any(isinstance(h, logging.NullHandler) for h in root_logger.handlers))


if __name__ == "__main__":
    unittest.main()
