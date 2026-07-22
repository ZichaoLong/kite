"""
Shared logging configuration.
"""

from __future__ import annotations

import logging
import logging.handlers
import pathlib

from kite.platform_paths import default_log_file


def configure_logging(*, data_dir: pathlib.Path | str | None = None) -> pathlib.Path:
    log_path = default_log_file(data_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    return log_path
