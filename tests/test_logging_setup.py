import logging
from logging.handlers import RotatingFileHandler

from flyvikingbot.logging_setup import setup_logging


def test_file_logging(tmp_path):
    setup_logging(tmp_path)
    assert any(isinstance(h, RotatingFileHandler) for h in logging.getLogger().handlers)


def test_unwritable_data_dir_falls_back_to_stdout(tmp_path):
    blocker = tmp_path / "data"
    blocker.write_text("not a directory")  # mkdir/open will fail
    setup_logging(blocker)
    handlers = logging.getLogger().handlers
    assert handlers and not any(isinstance(h, RotatingFileHandler) for h in handlers)
