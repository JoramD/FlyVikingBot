"""Logging to stdout (docker logs) and a rotating file in the data folder."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(name)s %(levelname)s: %(message)s"


def setup_logging(data_dir: Path, level: int = logging.INFO) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    file_error: OSError | None = None
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(data_dir / "discordBot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        )
    except OSError as exc:
        # Don't crash-loop over a log file; stdout logging still works (docker logs)
        file_error = exc

    logging.basicConfig(level=level, format=FORMAT, handlers=handlers, force=True)
    # discord.py's gateway logs are noisy at INFO
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    if file_error:
        logging.getLogger(__name__).warning("File logging disabled, can't write to %s: %s", data_dir, file_error)
