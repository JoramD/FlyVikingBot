import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(name)s %(levelname)s: %(message)s"


def setup_logging(data_dir: Path, level: int = logging.INFO) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        data_dir / "discordBot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    logging.basicConfig(level=level, format=FORMAT, handlers=[file_handler, stream_handler], force=True)
    # discord.py's gateway logs are noisy at INFO
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
